"""Explicit form submissions schedule work; the only JSON endpoint reports progress."""

from django import forms
from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods

from jobs.runtime import progress_state
from jobs.tasks import enqueue
from mailsome.errors import APIError
from mailsome.utilities import redirect_next


@require_http_methods(["POST"])
def refresh(request: HttpRequest) -> HttpResponse:
    # This explicit retry requires a connected account, just like scheduled sync.
    if not (settings.DATA_DIR / "token.json").exists():
        raise APIError(401, "Connect Gmail first.")
    retry_ai = forms.BooleanField(required=False).clean(request.POST.get("retry_ai"))
    enqueue("sync", explicit=retry_ai)
    return redirect_next(request)


@require_http_methods(["GET"])
def progress(request: HttpRequest) -> HttpResponse:
    return JsonResponse(progress_state())
