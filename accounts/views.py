"""Owner-supplied Gmail OAuth credentials, state and PKCE; tokens never reach the browser."""

import json
import secrets

from django.conf import settings
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseRedirect,
)
from django.views.decorators.http import require_http_methods
from google_auth_oauthlib.flow import Flow

from accounts.forms import CredentialsForm
from accounts.models import Account
from inbox import gmail
from jobs.pipeline import sync_requested
from mailsome.errors import APIError
from mailsome.utilities import atomic_write, page

ORIGIN = settings.ORIGIN
REDIRECT_URI = f"{ORIGIN}/auth/callback"


def oauth_flow(**kwargs: str) -> Flow:
    path = settings.DATA_DIR / "credentials.json"
    # OAuth credentials must be supplied by the owner, not committed to the repo.
    if not path.exists():
        raise APIError(
            503,
            "Open Connect Gmail for setup instructions and upload your Google web OAuth credentials.",
        )
    return Flow.from_client_secrets_file(
        str(path),
        scopes=gmail.SCOPES,
        redirect_uri=REDIRECT_URI,
        **kwargs,
    )


@require_http_methods(["GET"])
def connect(request: HttpRequest) -> HttpResponse:
    # Use one host consistently so the session cookie reaches the OAuth callback.
    if request.get_host().split(":")[0] != "localhost":
        return HttpResponseRedirect(f"{ORIGIN}/auth/connect")
    # First-time setup belongs in the browser, before Google authorization starts.
    if not (settings.DATA_DIR / "credentials.json").exists():
        return page(request, "accounts/connect.html", {"form": CredentialsForm()})
    flow = oauth_flow()
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    request.session["oauth_state"] = state
    request.session["oauth_verifier"] = flow.code_verifier
    return HttpResponseRedirect(url)


@require_http_methods(["POST"])
def upload_credentials(request: HttpRequest) -> HttpResponse:
    form = CredentialsForm(request.POST, request.FILES)
    status = 400
    if form.is_valid():
        path = settings.DATA_DIR / "credentials.json"
        # A stale setup tab must not overwrite a configured OAuth client.
        if path.exists():
            form.add_error(
                None,
                "OAuth credentials are already configured. Return to Connect Gmail; to replace them, remove the existing local credentials.json first.",
            )
            status = 409
        else:
            atomic_write(path, json.dumps({"web": form.cleaned_data["credentials"]}))
            return HttpResponseRedirect("/auth/connect", status=303)
    # File-size validation is distinct from invalid JSON or missing form fields.
    if any(
        error.code == "too_large"
        for errors in form.errors.as_data().values()
        for error in errors
    ):
        status = 413
    return page(request, "accounts/connect.html", {"form": form}, status=status)


@require_http_methods(["GET"])
def callback(request: HttpRequest) -> HttpResponse:
    expected = request.session.pop("oauth_state", None)
    verifier = request.session.pop("oauth_verifier", None)
    # Reject unsolicited callbacks before handling denial or exchanging any code.
    if not expected or not secrets.compare_digest(
        expected, request.GET.get("state", "")
    ):
        raise APIError(
            400, "Invalid or expired sign-in state. Start Connect Gmail again."
        )
    # The user can decline consent without changing the existing account or cache.
    if "error" in request.GET:
        raise APIError(400, "Gmail connection was cancelled. You can try again.")
    # A successful callback must include both the code and our original PKCE verifier.
    if not request.GET.get("code") or not verifier:
        raise APIError(400, "Incomplete sign-in response. Start Connect Gmail again.")

    flow = oauth_flow(state=expected, code_verifier=verifier)
    # Passing the code directly keeps local HTTP out of oauthlib's token URL checks.
    flow.fetch_token(code=request.GET["code"], timeout=30)
    # Offline access is necessary for future refreshes after the access token expires.
    if not flow.credentials.refresh_token:
        raise APIError(400, "No refresh token received. Reconnect Gmail.")
    with gmail.service(flow.credentials) as client:
        profile = gmail.get_profile(client)
    account = Account.objects.filter(pk=1).first()
    # Reconnecting another account must not overwrite this mailbox's token or preferences.
    if account and account.email != profile["emailAddress"]:
        raise APIError(
            409,
            "A different Gmail account is already connected. Reconnect that account.",
        )
    token = json.loads(flow.credentials.to_json())
    # Granular consent can grant fewer scopes; the SDK accepts both OAuth text and parsed lists.
    granted = flow.credentials.granted_scopes
    if granted is not None:
        token["scopes"] = granted.split() if isinstance(granted, str) else list(granted)
    atomic_write(settings.DATA_DIR / "token.json", json.dumps(token))
    Account.objects.get_or_create(
        pk=1,
        defaults={
            "email": profile["emailAddress"],
            "history_id": profile["historyId"],
        },
    )
    # Wake the Gmail worker to sync now rather than wait for its next interval.
    sync_requested.set()
    return HttpResponseRedirect("/", status=303)
