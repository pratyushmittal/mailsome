"""Explicit refresh and minute-level sync/classification status."""

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods

from accounts.models import Account
from inbox.models import Message
from jobs.pipeline import sync_requested
from mailsome.errors import APIError
from mailsome.utilities import redirect_next


@require_http_methods(["POST"])
def refresh(request: HttpRequest) -> HttpResponse:
    if not (settings.DATA_DIR / "token.json").exists():
        raise APIError(401, "Connect Gmail first.")
    sync_requested.set()
    return redirect_next(request)


@require_http_methods(["GET"])
def sync_status(request: HttpRequest) -> HttpResponse:
    return JsonResponse(
        {
            "synced_at": Account.objects.filter(pk=1)
            .values_list("synced_at", flat=True)
            .first(),
            "classifications_due": Message.objects.classifiable().count(),
        }
    )
