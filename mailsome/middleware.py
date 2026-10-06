"""Apply request checks and browser protections to the local mail application.

Mailsome uses the connected owner's Gmail account without a separate app login.
A local server can still receive requests from other websites opened in that
browser, so binding to localhost alone is not enough. This middleware enforces
ALLOWED_HOSTS, rejects requests marked cross-site (except the OAuth callback),
and requires the app's request header and same-origin checks for API polling.
OAuth state validation and Django's CSRF middleware remain separate protections.

Responses disable caching of private mail and set browser policies restricting
scripts, framing, content-type guessing, and referrer disclosure. Email images
from HTTPS sites are allowed by the default policy only when requested for that
view. View exceptions use the app's shared error response handling.

These checks are not authentication: non-browser clients can forge headers.
The server must remain local and must not be exposed through hosting or tunnels.
"""

from collections.abc import Callable

from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.http import HttpRequest, HttpResponse
from django.utils.log import log_response

from mailsome.errors import APIError, error_response


class LocalSecurity:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        try:
            request.get_host()  # Enforce ALLOWED_HOSTS for HTML pages as well as static files.
        except DisallowedHost:
            return error_response(request, APIError(400, "Use localhost:8002."))
        # The sole JSON endpoint is read-only and reserved for same-origin sync/classification polling.
        if request.path.startswith("/api/") and (
            request.headers.get("X-Mailsome-Request") != "1"
            or request.headers.get("Origin", settings.ORIGIN) != settings.ORIGIN
            or request.headers.get("Sec-Fetch-Site") == "cross-site"
        ):
            response = error_response(
                request, APIError(403, "Use Mailsome in its local browser tab.")
            )
        # OAuth returns through a cross-site navigation; its one-time state is validated in the callback.
        elif (
            request.headers.get("Sec-Fetch-Site") == "cross-site"
            and request.path != "/auth/callback"
        ):
            response = error_response(
                request, APIError(403, "Open Mailsome directly on localhost.")
            )
        else:
            response = self.get_response(request)
        for name, value in {
            "Cache-Control": "no-store",
            # no-referrer makes Chromium HTTP form POSTs send Origin:null, failing CSRF.
            # same-origin preserves native local forms without leaking URLs to external sites.
            "Referrer-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        }.items():
            # Only the isolated email document overrides framing and referrer policy.
            if name in {"X-Frame-Options", "Referrer-Policy"}:
                response.setdefault(name, value)
            else:
                response[name] = value
        # Only our local script executes; email bodies load in same-origin sandboxed frames.
        response.setdefault(
            "Content-Security-Policy",
            (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:"
                + (" https:" if request.GET.get("images") == "1" else "")
                + "; "
                "object-src 'none'; frame-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            ),
        )
        return response

    def process_exception(
        self, request: HttpRequest, exception: Exception
    ) -> HttpResponse:
        response = error_response(request, exception)
        log_response(
            "%s: %s",
            response.reason_phrase,
            request.path,
            response=response,
            request=request,
            exception=exception,
        )
        return response
