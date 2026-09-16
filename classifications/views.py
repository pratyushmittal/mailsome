"""Django-rendered AI settings and paged request/cost history."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.views.decorators.http import require_http_methods

from classifications import labeling, usage
from classifications.forms import AIForm, ReclassifyForm, UserContextForm
from classifications.models import AIRequest
from inbox import gmail
from inbox.models import Message
from jobs.models import Work
from jobs.runtime import label_policy_lock
from jobs.tasks import enqueue
from mailsome.errors import APIError
from mailsome.utilities import page, redirect_next


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
    # Reads need no lock; saves serialize only with policy edits and an authorized label write.
    with label_policy_lock() if request.method == "POST" else nullcontext():
        config = labeling.settings()
        form = AIForm(
            request.POST if request.method == "POST" else None,
            initial={
                key: config[key] for key in ("enabled", "reasoning", "user_context")
            },
        )
        if request.method == "POST" and form.is_valid():
            # Blank password fields preserve the secret; never add it to form initial/context.
            if form.cleaned_data["api_key"]:
                config["api_key"] = form.cleaned_data["api_key"]
            if form.cleaned_data["enabled"] and not config["api_key"]:
                form.add_error("api_key", "Add your OpenAI API key before enabling AI.")
            else:
                config.update(
                    enabled=form.cleaned_data["enabled"],
                    reasoning=form.cleaned_data["reasoning"],
                )
                # Older open Settings forms omit the new field; only submitted context replaces it.
                if "user_context" in request.POST:
                    config["user_context"] = form.cleaned_data["user_context"]
                gmail.atomic_write(settings.DATA_DIR / "ai.json", json.dumps(config))
    if request.method == "POST" and not form.errors:
        enqueue("labeling", explicit=True)
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
    with label_policy_lock() if request.method == "POST" else nullcontext():
        config = labeling.settings()
        form = UserContextForm(
            request.POST if request.method == "POST" else None,
            initial={"user_context": config["user_context"]},
        )
        form.fields["user_context"].widget.attrs["autofocus"] = True
        if request.method == "POST" and form.is_valid():
            config["user_context"] = form.cleaned_data["user_context"]
            gmail.atomic_write(settings.DATA_DIR / "ai.json", json.dumps(config))
            enqueue("labeling", explicit=True)
            return redirect_next(request, default="/settings/")
    return page(
        request,
        "classifications/context.html",
        {"form": form},
        status=400 if form.errors else 200,
    )


@require_http_methods(["GET", "POST"])
def reclassify(request: HttpRequest) -> HttpResponse:
    """Confirm a durable, scoped label reset before reclassifying cached inbox mail."""
    recent = Message.objects.inbox()
    form = ReclassifyForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        # Worker claims and reset share a transaction: old in-flight answers cannot undo a reset.
        with transaction.atomic():
            config = labeling.settings()
            # Refuse resets until the active request finishes or restart recovery records its outcome.
            if (
                Work.objects.filter(
                    kind="labeling", progress__status="running"
                ).exists()
                or AIRequest.objects.filter(status="running").exists()
            ):
                form.add_error(
                    None,
                    "Wait for labeling to finish. Restart the app first if its worker was interrupted.",
                )
            # A second confirmation must not replace an interrupted or already queued selection.
            elif Work.objects.filter(kind="sync").exclude(reclassification={}).exists():
                form.add_error(
                    None,
                    "A reclassification reset is already pending. Refresh to retry it.",
                )
            # Confirmation cannot enable AI implicitly or invent a label configuration.
            elif (
                not config["enabled"]
                or not config["api_key"]
                or not labeling.enabled_labels()
            ):
                form.add_error(
                    None, "Enable AI and at least one label before reclassifying."
                )
            # A reconnected read-only account cannot reset Gmail labels.
            elif not gmail.can_label():
                form.add_error(
                    None, "Reconnect Gmail with modify access before reclassifying."
                )
            # Sync may archive or delete cached inbox mail before confirmation.
            elif not recent.exists():
                form.add_error(
                    None, "There are no cached inbox messages to reclassify."
                )
            else:
                work, _ = Work.objects.get_or_create(kind="sync")
                work.reclassification = {
                    "message_ids": list(recent.values_list("id", flat=True)),
                    "label_ids": [label["id"] for label in labeling.enabled_labels()],
                }
                work.save(update_fields=["reclassification"])
                enqueue("sync", explicit=True)
                return HttpResponseRedirect("/settings/", status=303)
    return page(
        request,
        "classifications/reclassify.html",
        {
            "form": form,
            "message_count": recent.count(),
            "labels": labeling.enabled_labels(),
        },
        status=400 if form.errors else 200,
    )
