"""Server-rendered page context and local-only return navigation."""

from typing import Any
from urllib.parse import unquote, urlencode, urlsplit

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render

from accounts.models import Account
from inbox.models import Tab
from jobs.runtime import progress_state


def safe_next(request: HttpRequest, default: str = "/") -> str:
    value = request.POST.get("next", request.GET.get("next", default))
    decoded = unquote(value)
    # Return targets come from query/form data: never redirect to another host or an unsafe scheme.
    if (
        not value.startswith("/")
        or decoded.startswith("//")
        or "\\" in decoded
        or any(ord(char) < 32 for char in decoded)
        or urlsplit(value).netloc
    ):
        return default
    return value


def redirect_next(request: HttpRequest, default: str = "/") -> HttpResponse:
    return HttpResponseRedirect(safe_next(request, default), status=303)


def page(
    request: HttpRequest,
    template: str,
    context: dict[str, Any] | None = None,
    *,
    status: int = 200,
) -> HttpResponse:
    progress = progress_state()
    # Only the rendered links need presentation dictionaries; ORM callers keep typed models.
    tabs = [
        {
            "id": tab.pk,
            "name": tab.name,
            "tone": tab.pk % 6,
            "url": "/?" + urlencode({"tab": tab.pk}),
        }
        for tab in Tab.objects.all()
    ]
    query = {
        key: request.GET[key]
        for key in ("tab", "q", "sender", "page")
        if request.GET.get(key)
    }
    list_url = "/" + ("?" + urlencode(query) if query else "")
    return render(
        request,
        template,
        {
            "account": Account.objects.filter(pk=1)
            .values("email", "synced_at")
            .first(),
            "tabs": tabs,
            "current_tab": request.GET.get("tab", ""),
            "selected_tab": next(
                (tab for tab in tabs if str(tab["id"]) == request.GET.get("tab")), None
            ),
            "list_url": list_url,
            "next_url": safe_next(request, list_url),
            "return_url": request.get_full_path(),
            "sync_progress": progress["sync"],
            "label_progress": progress["labeling"],
            "progress_revision": progress["revision"],
            "poll_inbox": template == "inbox/list.html"
            and not request.GET.get("sender"),
            **(context or {}),
        },
        status=status,
    )
