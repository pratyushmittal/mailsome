"""Small, locally served UI assets shared by inbox and connection setup."""

from contextlib import closing

from django.conf import settings
from django.http import FileResponse, HttpRequest, HttpResponse
from django.views.decorators.http import require_http_methods
from django.views.static import serve

ROOT = settings.BASE_DIR


@require_http_methods(["GET", "HEAD"])
def static_file(request: HttpRequest, path: str) -> HttpResponse:
    response = serve(request, path, document_root=ROOT / "static")
    # Conditional requests can return a non-streaming 304 response.
    if not isinstance(response, FileResponse):
        return response
    # These small local UI assets need no streaming. Buffer in the sync view, not ASGI's iterator adapter.
    with closing(response):
        return HttpResponse(
            b"".join(response), status=response.status_code, headers=response.headers
        )
