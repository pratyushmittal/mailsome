"""Django-rendered AI settings and paged request/cost history."""

import json
from datetime import UTC, datetime

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.views.decorators.http import require_http_methods

from classifications import usage, utils
from classifications.forms import (
    AIForm,
    ImportanceForm,
    ReclassifyForm,
    UserContextForm,
)
from inbox import gmail
from inbox.models import Message
from mailsome.errors import APIError
from mailsome.utilities import atomic_write, page, redirect_next


@require_http_methods(["GET", "POST"])
def ai_settings(request: HttpRequest) -> HttpResponse:
    before = request.GET.get("before")
    # Keyset cursors must fit SQLite's signed integer IDs; provider diagnostics never reach the page.
    if before is not None and (
        len(before) > 19
        or not before.isascii()
        or not before.isdecimal()
        or not 0 < int(before) <= 2**63 - 1
    ):
        raise APIError(400, "Provide a valid request-history cursor.")
    config = utils.settings()
    form = AIForm(
        request.POST if request.method == "POST" else None,
        initial={key: config[key] for key in ("enabled", "user_context")},
    )
    if request.method == "POST" and form.is_valid():
        # Blank password fields preserve the secret; never add it to form initial/context.
        if form.cleaned_data["api_key"]:
            config["api_key"] = form.cleaned_data["api_key"]
        if form.cleaned_data["enabled"] and not config["api_key"]:
            form.add_error("api_key", "Add your TypeSafe API key before enabling AI.")
        else:
            config["enabled"] = form.cleaned_data["enabled"]
            # Older open Settings forms omit the new field; only submitted context replaces it.
            if "user_context" in request.POST:
                config["user_context"] = form.cleaned_data["user_context"]
            atomic_write(settings.DATA_DIR / "typesafe.json", json.dumps(config))
    if request.method == "POST" and not form.errors:
        return HttpResponseRedirect("/settings/", status=303)
    history = usage.history(int(before) if before else None)
    for record in history["requests"]:
        record["date"] = datetime.fromtimestamp(record["started_at"] / 1000, UTC)
        record["duration"] = (
            (record["finished_at"] - record["started_at"]) / 1000
            if record["finished_at"]
            else None
        )
    return page(
        request,
        "classifications/settings.html",
        {
            "form": form,
            "usage": history,
            "has_key": bool(config["api_key"]),
            "model": config["model"],
            "can_label": gmail.can_label(),
            "can_manage_filters": gmail.can_manage_filters(),
        },
        status=400 if form.errors else 200,
    )


@require_http_methods(["GET", "POST"])
def user_context(request: HttpRequest) -> HttpResponse:
    """Edit only mail context, preserving AI consent, credentials and completed work."""
    config = utils.settings()
    form = UserContextForm(
        request.POST if request.method == "POST" else None,
        initial={"user_context": config["user_context"]},
    )
    form.fields["user_context"].widget.attrs["autofocus"] = True
    if request.method == "POST" and form.is_valid():
        config["user_context"] = form.cleaned_data["user_context"]
        atomic_write(settings.DATA_DIR / "typesafe.json", json.dumps(config))
        return redirect_next(request, default="/settings/")
    return page(
        request,
        "classifications/context.html",
        {"form": form},
        status=400 if form.errors else 200,
    )


@require_http_methods(["GET", "POST"])
def reclassify(request: HttpRequest) -> HttpResponse:
    """Reset selected labels and make cached inbox mail eligible for the classifier."""
    recent = Message.objects.inbox()
    form = ReclassifyForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        config = utils.settings()
        labels = utils.enabled_labels()
        # Confirmation cannot enable AI implicitly or invent a label configuration.
        if not config["enabled"] or not config["api_key"]:
            form.add_error(None, "Enable AI before reclassifying.")
        # A reconnected read-only account cannot reset Gmail labels.
        elif labels and not gmail.can_label():
            form.add_error(
                None, "Reconnect Gmail with modify access before reclassifying."
            )
        # Sync may archive or delete cached inbox mail before confirmation.
        elif not recent.exists():
            form.add_error(None, "There are no cached inbox messages to reclassify.")
        else:
            utils.reset_classifications(
                {
                    "message_ids": list(recent.values_list("id", flat=True)),
                    "label_ids": [label["id"] for label in labels],
                }
            )
            return HttpResponseRedirect("/settings/", status=303)
    return page(
        request,
        "classifications/reclassify.html",
        {
            "form": form,
            "message_count": recent.count(),
            "labels": utils.enabled_labels(),
        },
        status=400 if form.errors else 200,
    )


@require_http_methods(["GET", "POST"])
def importance_settings(request: HttpRequest) -> HttpResponse:
    """Edit the importance rubric and badge threshold without changing stored scores."""
    config = utils.settings()
    form = ImportanceForm(
        request.POST if request.method == "POST" else None,
        initial={
            "importance_levels": "\n".join(config["importance_levels"]),
            "importance_threshold": config["importance_threshold"],
        },
    )
    if request.method == "POST" and form.is_valid():
        config.update(form.cleaned_data)
        atomic_write(settings.DATA_DIR / "typesafe.json", json.dumps(config))
        return HttpResponseRedirect("/settings/importance/", status=303)
    return page(
        request,
        "classifications/importance.html",
        {"form": form},
        status=400 if form.errors else 200,
    )
