"""Shared file persistence, page context, and local-only return navigation."""

from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import unquote, urlencode, urlsplit

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render

from accounts.models import Account
from inbox.models import Message, Tab


def atomic_write(path: Path, content: str) -> None:
    """Replace a file via a sibling temporary file, keeping owner-only (0600) permissions."""
    # Readers see the old or complete new file, never partially written content.
    # Unique temporary paths prevent overlapping writes from sharing a temporary file.
    with NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=f".{path.name}-", delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            file.write(content)
            file.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


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
    account = Account.objects.filter(pk=1).values("email", "synced_at").first()
    synced_at = account["synced_at"] if account else None
    # Only the rendered links need presentation dictionaries; ORM callers keep typed models.
    tabs = [
        {
            "id": tab.pk,
            "name": tab.name,
            "tone": tab.pk % 6,
            "feed": tab.feed,
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
            "account": account,
            "last_synced": datetime.fromtimestamp(synced_at / 1000, UTC)
            if synced_at
            else None,
            "sync_interval_ms": settings.SYNC_INTERVAL * 1000,
            "classifications_due": Message.objects.classifiable().count(),
            "tabs": tabs,
            "current_tab": request.GET.get("tab", ""),
            "selected_tab": next(
                (tab for tab in tabs if str(tab["id"]) == request.GET.get("tab")), None
            ),
            "list_url": list_url,
            "next_url": safe_next(request, list_url),
            "return_url": request.get_full_path(),
            **(context or {}),
        },
        status=status,
    )
