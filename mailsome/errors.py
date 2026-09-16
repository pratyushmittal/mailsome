"""Safe public errors: never serialize upstream payloads, mail, or credentials."""

import time
from email.utils import parsedate_to_datetime

from django.core.exceptions import RequestDataTooBig
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from oauthlib.oauth2 import OAuth2Error


class APIError(Exception):
    def __init__(self, status: int, detail: str):
        self.status_code = status
        self.detail = detail
        super().__init__(detail)


def gmail_denial(error: HttpError) -> str:
    # Google's client parses both legacy errors[] and modern ErrorInfo details[].
    details = error.error_details if isinstance(error.error_details, list) else []
    for item in details:
        # ErrorInfo may be accompanied by help links or other non-reason details.
        if not isinstance(item, dict):
            continue
        reason = item.get("reason")
        if reason in ("accessNotConfigured", "SERVICE_DISABLED"):
            advice = "Enable the Gmail API in the same Google Cloud project as the uploaded OAuth client. If you just enabled it, wait a few minutes, then start Connect Gmail again."
        elif reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"):
            advice = "Start Connect Gmail again and grant the requested Gmail mail and settings permissions on Google's consent screen. Configuring a scope in Cloud does not itself grant access to your account."
        elif reason == "domainPolicy":
            advice = "Your Google Workspace administrator has blocked this app's Gmail access. Ask them to review the app's API access policy."
        elif reason in (
            "dailyLimitExceeded",
            "rateLimitExceeded",
            "userRateLimitExceeded",
            "RATE_LIMIT_EXCEEDED",
        ):
            advice = "Gmail's quota or rate limit was exceeded. Wait and retry; check the project's Gmail API quotas if it persists."
        else:
            # Only recognized reason codes are reflected; arbitrary diagnostics may contain secrets.
            continue
        return f"Gmail returned 403 ({reason}). {advice}"
    return "Gmail returned 403 without a recognized reason. Check API access, granted consent, Workspace policy, and quota settings; then start Connect Gmail again."


def gmail_retry_delay(error: Exception) -> int | None:
    """Return a minimum quota cooldown, or None for errors unrelated to Gmail quota."""
    # Only Gmail HTTP failures qualify; never turn an AI failure into an automatic paid retry.
    if not isinstance(error, HttpError):
        return None
    details = error.error_details if isinstance(error.error_details, list) else []
    reasons = {
        item.get("reason")
        for item in details
        if isinstance(item, dict) and isinstance(item.get("reason"), str)
    }
    # A 403 may mean disabled API/access, not throttling. Do not conceal that distinction.
    if error.resp.status != 429 and not (
        error.resp.status == 403
        and reasons.intersection(
            {
                "rateLimitExceeded",
                "userRateLimitExceeded",
                "RATE_LIMIT_EXCEEDED",
                "dailyLimitExceeded",
            }
        )
    ):
        return None
    minimum = 3600 if "dailyLimitExceeded" in reasons else 60
    header = error.resp.get("retry-after", "")
    try:
        return max(minimum, int(header))
    except (ValueError, TypeError):
        # Retry-After can also be an HTTP date; absent/invalid hints use our own backoff.
        try:
            return max(
                minimum,
                int(parsedate_to_datetime(header).timestamp() - time.time()) + 1,
            )
        except (ValueError, TypeError, OverflowError):
            return minimum


def error_response(request: HttpRequest | None, error: Exception) -> HttpResponse:
    status, detail = (
        502,
        "Gmail is unavailable. Saved mail and decisions are retained. Please retry.",
    )
    # Expected errors get actionable messages without leaking tokens or email payloads.
    if isinstance(error, APIError):
        status, detail = error.status_code, str(error.detail)
    elif isinstance(error, RefreshError) or (
        isinstance(error, HttpError) and error.resp.status == 401
    ):
        status, detail = (
            401,
            "Gmail access expired or was revoked. Reconnect the same account.",
        )
    elif isinstance(error, OAuth2Error):
        status, detail = 400, "Google sign-in failed. Start Connect Gmail again."
    elif isinstance(error, Http404):
        status, detail = 404, "This page or message is no longer available."
    elif isinstance(error, RequestDataTooBig):
        status, detail = 413, "This upload or form is too large."
    elif isinstance(error, HttpError) and error.resp.status == 429:
        status, detail = 429, "Gmail rate limit reached. Wait before retrying."
    elif isinstance(error, HttpError) and error.resp.status == 403:
        status, detail = 403, gmail_denial(error)
    # Jobs need structured safe errors; browser pages receive escaped HTML, never raw provider details.
    if request is None or request.path.startswith("/api/"):
        return JsonResponse({"error": detail}, status=status)
    return render(
        request, "error.html", {"error": detail, "status": status}, status=status
    )


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    return error_response(
        request,
        APIError(
            403,
            "This form expired or came from another site. Reload the page and try again.",
        ),
    )
