"""Validate the owner's uploaded Google Web OAuth client before saving it."""

import json
from typing import Any

from django import forms
from django.conf import settings


class CredentialsForm(forms.Form):
    credentials = forms.FileField(
        label="Google OAuth client JSON",
        widget=forms.FileInput(attrs={"accept": ".json,application/json"}),
    )

    def clean_credentials(self) -> dict[str, Any]:
        uploaded = self.cleaned_data["credentials"]
        # Uploaded files bypass DATA_UPLOAD_MAX_MEMORY_SIZE, so check their actual size too.
        if uploaded.size > 64 * 1024:
            raise forms.ValidationError(
                "Choose the Google client JSON file (maximum 64 KB).", code="too_large"
            )
        try:
            config = json.loads(uploaded.read(64 * 1024 + 1))
        except (ValueError, UnicodeError, RecursionError):
            raise forms.ValidationError(
                "This is not a valid JSON file. Upload the file downloaded from Google Cloud.",
            ) from None

        web = config.get("web") if isinstance(config, dict) else None
        # Desktop clients, service-account keys, and token files cannot configure this web app.
        if not isinstance(web, dict) or not all(
            isinstance(web.get(key), str) and web[key].strip()
            for key in ("client_id", "client_secret", "auth_uri", "token_uri")
        ):
            raise forms.ValidationError(
                "Choose a Web application OAuth client JSON with its client ID and secret, not a Desktop client, service-account key, or token file.",
            )
        # Untrusted uploads must never redirect users or send authorization codes to another host.
        if (
            web["auth_uri"]
            not in {
                "https://accounts.google.com/o/oauth2/auth",
                "https://accounts.google.com/o/oauth2/v2/auth",
            }
            or web["token_uri"] != "https://oauth2.googleapis.com/token"
        ):
            raise forms.ValidationError(
                "The file must use Google's official OAuth authorization and token endpoints. Download it again from Google Cloud.",
            )
        # Google requires the callback to be registered on this particular client.
        if (
            not isinstance(web.get("redirect_uris"), list)
            or f"{settings.ORIGIN}/auth/callback" not in web["redirect_uris"]
        ):
            raise forms.ValidationError(
                f"Add {settings.ORIGIN}/auth/callback under Authorized redirect URIs in Google Cloud, save, and download the JSON again.",
            )

        return web
