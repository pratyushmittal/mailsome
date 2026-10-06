from __future__ import annotations

import base64
import copy
import itertools
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import httplib2
import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client as DjangoClient
from google.auth.exceptions import RefreshError
from google_auth_oauthlib.flow import Flow
from googleapiclient.errors import HttpError
from pytest_bdd import given, scenario, then, when

from accounts import views as oauth
from accounts.models import Account
from classifications import labeling
from classifications import utils as classification_utils
from classifications.utils import DEFAULT_MODEL
from inbox import gmail
from inbox.gmail import service as gmail_service
from inbox.models import Message, Tab
from jobs import pipeline

if TYPE_CHECKING:
    from googleapiclient._apis.gmail.v1.schemas import (
        Message as GmailMessage,
    )

NOW = 1_800_000_000_000
HEADERS = {"X-Mailsome-Request": "1", "Origin": settings.ORIGIN}


def http_error(status: int) -> HttpError:
    return HttpError(
        httplib2.Response({"status": str(status)}),
        b'{"error":{"message":"fake error"}}',
    )


def mail(
    message_id: str, *, days: int = 1, labels: list[str] | None = None
) -> GmailMessage:
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "internalDate": str(NOW - days * 86400 * 1000),
        "labelIds": labels if labels is not None else ["INBOX", "UNREAD"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "A Human <human@example.com>"},
                {"name": "Subject", "value": f"Subject {message_id}"},
            ],
            "body": {"data": base64.urlsafe_b64encode(b"Hello from a human.").decode()},
        },
    }


@pytest.fixture
def api() -> MagicMock:
    service = MagicMock()
    service.users.return_value = service
    service.__enter__.return_value = service
    service.getProfile.return_value.execute.return_value = {
        "emailAddress": "me@example.com",
        "historyId": "100",
    }

    def list_page(**_):
        kwargs = service.messages.return_value.list.call_args.kwargs
        rows = [row for row in service.mailbox.values() if isinstance(row, dict)]
        rows = [
            row
            for row in rows
            if set(kwargs.get("labelIds", [])).issubset(row["labelIds"])
            and not {"SPAM", "TRASH"}.intersection(row["labelIds"])
        ]
        query = kwargs.get("q", "")
        # This double only models fixtures' simple label/sender predicates; Gmail owns real parsing.
        import re
        from email.utils import parseaddr

        for name in re.findall(r'-label:"([^"\\]+)"', query):
            label_id = next(
                (
                    label["id"]
                    for label in service.labels.return_value.list.return_value.execute.return_value[
                        "labels"
                    ]
                    if label["name"] == name
                ),
                None,
            )
            rows = [row for row in rows if label_id not in row["labelIds"]]
        if query.startswith("from:"):
            sender = query.removeprefix("from:").strip('"').split()[0]
            rows = [
                row
                for row in rows
                if parseaddr(
                    next(
                        header["value"]
                        for header in row["payload"]["headers"]
                        if header["name"] == "From"
                    )
                )[1]
                == sender
            ]
        rows.sort(key=lambda row: (-int(row["internalDate"]), row["id"]))
        offset = int(kwargs.get("pageToken") or 0)
        size = kwargs.get("maxResults", 50)
        result = {
            "messages": [
                {"id": row["id"], "threadId": row["threadId"]}
                for row in rows[offset : offset + size]
            ]
        }
        if offset + size < len(rows):
            result["nextPageToken"] = str(offset + size)
        return result

    service.default_list = list_page
    service.messages.return_value.list.return_value.execute.side_effect = list_page
    service.history.return_value.list.return_value.execute.return_value = {
        "historyId": "110"
    }
    service.labels.return_value.list.return_value.execute.return_value = {
        "labels": [
            {"id": "Label_humans", "name": "Humans", "type": "user"},
            {"id": "INBOX", "name": "INBOX", "type": "system"},
        ]
    }
    service.labels.return_value.create.return_value.execute.return_value = {
        "id": "Label_new",
        "name": "Receipts",
        "type": "user",
    }

    def create_label_result(**_):
        label = service.labels.return_value.create.return_value.execute.return_value
        labels = service.labels.return_value.list.return_value.execute.return_value[
            "labels"
        ]
        if label not in labels:
            labels.append(label)
        return label

    service.labels.return_value.create.return_value.execute.side_effect = (
        create_label_result
    )

    def modify(**kwargs: Any) -> MagicMock:
        message = service.mailbox[kwargs["id"]]
        message["labelIds"] = list(
            (set(message["labelIds"]) | set(kwargs["body"].get("addLabelIds", [])))
            - set(kwargs["body"].get("removeLabelIds", []))
        )
        events = service.history.return_value.list.return_value.execute.return_value.setdefault(
            "history", []
        )
        events.append(
            {
                kind: [
                    {
                        "message": {"id": kwargs["id"]},
                        "labelIds": kwargs["body"][action],
                    }
                ]
                for kind, action in (
                    ("labelsAdded", "addLabelIds"),
                    ("labelsRemoved", "removeLabelIds"),
                )
                if kwargs["body"].get(action)
            }
        )
        return MagicMock(execute=MagicMock(return_value=copy.deepcopy(message)))

    service.messages.return_value.modify.side_effect = modify

    def batch_modify(**kwargs: Any) -> MagicMock:
        for message_id in kwargs["body"]["ids"]:
            modify(id=message_id, body=kwargs["body"])
        return MagicMock(execute=MagicMock(return_value={}))

    service.messages.return_value.batchModify.side_effect = batch_modify
    service.mailbox = {
        "a": mail("a"),
        "b": mail("b"),
        "old": mail("old", days=20, labels=[]),
        "archived": mail("archived", labels=[]),
        "during": mail("during"),
    }

    def get(**kwargs: Any) -> MagicMock:
        request = MagicMock()
        result = service.mailbox.get(kwargs["id"])
        # Tests model messages disappearing between the list and detail requests.
        if result is None:
            request.execute.side_effect = http_error(404)
        elif isinstance(result, Exception):
            request.execute.side_effect = result
        else:
            response = copy.deepcopy(result)
            # Minimal label reads omit content; detail reads return the entire payload.
            if kwargs.get("format") == "minimal":
                assert kwargs["fields"] == "id,threadId,labelIds"
                response = {
                    key: response[key] for key in ("id", "threadId", "labelIds")
                }
            else:
                assert "fields" not in kwargs
            request.execute.return_value = response
        return request

    def get_thread(**kwargs: Any) -> MagicMock:
        messages = [
            copy.deepcopy(message)
            for message in service.mailbox.values()
            if isinstance(message, dict) and message["threadId"] == kwargs["id"]
        ]
        assert "fields" not in kwargs
        return MagicMock(
            execute=MagicMock(return_value={"id": kwargs["id"], "messages": messages})
        )

    def modify_thread(**kwargs: Any) -> MagicMock:
        for message in service.mailbox.values():
            if isinstance(message, dict) and message["threadId"] == kwargs["id"]:
                modify(id=message["id"], body=kwargs["body"])
        return MagicMock(execute=MagicMock(return_value={"id": kwargs["id"]}))

    service.threads.return_value.modify.side_effect = modify_thread
    service.threads.return_value.get.side_effect = get_thread
    service.messages.return_value.get.side_effect = get
    service.batch_sizes = []

    def new_batch(callback):
        requests = []
        batch = MagicMock()
        batch.add.side_effect = lambda request, request_id: requests.append(
            (request_id, request)
        )

        def execute():
            service.batch_sizes.append(len(requests))
            for request_id, request in requests:
                try:
                    response = request.execute()
                except HttpError as error:
                    callback(request_id, None, error)
                else:
                    callback(request_id, response, None)

        batch.execute.side_effect = execute
        return batch

    service.new_batch_http_request.side_effect = new_batch
    service.filter_store = {}
    filters = service.settings.return_value.filters.return_value
    filters.list.return_value.execute.side_effect = lambda **_: {
        "filter": copy.deepcopy(list(service.filter_store.values()))
    }

    filter_ids = itertools.count(1)

    def create_filter(**kwargs):
        def execute(**_):
            identifier = f"managed-{next(filter_ids)}"
            service.filter_store[identifier] = {
                "id": identifier,
                **copy.deepcopy(kwargs["body"]),
            }
            return copy.deepcopy(service.filter_store[identifier])

        return MagicMock(execute=MagicMock(side_effect=execute))

    def delete_filter(**kwargs):
        def execute(**_):
            if kwargs["id"] not in service.filter_store:
                raise http_error(404)
            del service.filter_store[kwargs["id"]]
            return {}

        return MagicMock(execute=MagicMock(side_effect=execute))

    filters.create.side_effect = create_filter
    filters.delete.side_effect = delete_filter
    return service


def credentials_file(content: dict[str, Any] | bytes) -> SimpleUploadedFile:
    data = json.dumps(content).encode() if isinstance(content, dict) else content
    return SimpleUploadedFile("credentials.json", data, content_type="application/json")


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: MagicMock
) -> Iterator[DjangoClient]:
    monkeypatch.setattr(gmail.time, "time", lambda: NOW / 1000)
    monkeypatch.setattr(gmail.random, "random", lambda: 0)

    @contextmanager
    def fake_service(credentials=None) -> Iterator[MagicMock]:
        yield api

    monkeypatch.setattr(gmail, "service", fake_service)
    browser = DjangoClient(
        enforce_csrf_checks=True,
        headers={**HEADERS, "Host": "localhost:8002"},
        SERVER_NAME="localhost",
        SERVER_PORT="8002",
    )
    browser.get("/")
    browser.defaults["HTTP_X_CSRFTOKEN"] = browser.cookies["csrftoken"].value
    yield browser


def snapshot():
    return list(Message.objects.order_by("id").values()), Account.objects.values().get()


@pytest.fixture
def google_consent(monkeypatch):
    def fetch_token(flow, **kwargs):
        flow.oauth2session.token = {
            "access_token": "private-access",
            "refresh_token": "private-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
            "expires_at": NOW / 1000 + 3600,
            "scope": " ".join(gmail.SCOPES),
        }
        return flow.oauth2session.token

    monkeypatch.setattr(Flow, "fetch_token", fetch_token)


@pytest.fixture
def oauth_ready(tmp_path, web_credentials, google_consent):
    (tmp_path / "credentials.json").write_text(json.dumps(web_credentials))


@scenario(
    "inbox.feature", "Connect Gmail through local OAuth setup and reload its token"
)
def test_gmail_setup_connection_and_token_reload(
    client, tmp_path, api, monkeypatch, web_credentials, google_consent
):
    pass


@given("the local Google OAuth setup page is available")
def local_oauth_setup(client):
    page = client.get("/auth/connect")
    assert page.status_code == 200
    assert 'type="file"' in page.text
    assert oauth.REDIRECT_URI in page.text


@when(
    "I upload web credentials and complete Google consent",
    target_fixture="complete_google_connection",
)
def complete_google_connection(client, tmp_path, api, monkeypatch, web_credentials):
    response = client.post(
        "/auth/credentials", data={"credentials": credentials_file(web_credentials)}
    )
    assert response.status_code == 303
    assert json.loads((tmp_path / "credentials.json").read_text()) == web_credentials

    # Use our real connection boundary; only Google's transport is replaced.
    monkeypatch.setattr(gmail, "service", gmail_service)
    monkeypatch.setattr(gmail, "build", MagicMock(return_value=api))
    response = authorize(client)
    return response


@then(
    "the account is connected without preloading mail and its token is saved",
    target_fixture="verify_connected_account",
)
def verify_connected_account(
    tmp_path, api, web_credentials, complete_google_connection
):
    response = complete_google_connection
    assert response.status_code == 303
    assert Account.objects.get(pk=1).email == "me@example.com"
    assert not Message.objects.exists()
    api.messages.return_value.list.assert_not_called()
    token_path = tmp_path / "token.json"
    saved = json.loads(token_path.read_text())
    assert saved["refresh_token"] == "private-refresh"
    assert saved["client_id"] == web_credentials["web"]["client_id"]
    return saved, token_path


@when("the saved access token expires and Gmail access refreshes it")
def refresh_saved_access_token(monkeypatch, verify_connected_account):
    saved, token_path = verify_connected_account
    # Expire the saved token and exercise reload/refresh persistence, not Google's protocol.
    saved["expiry"] = "2000-01-01T00:00:00Z"
    token_path.write_text(json.dumps(saved))

    def renew(credentials, request):
        assert credentials.refresh_token == "private-refresh"
        credentials.token = "renewed-access"

    monkeypatch.setattr(gmail.Credentials, "refresh", renew)
    with gmail.service() as service:
        assert gmail.get_profile(service)["emailAddress"] == "me@example.com"


@then("the renewed token is persisted for subsequent Gmail access")
def verify_renewed_token(verify_connected_account):
    _saved, token_path = verify_connected_account
    assert json.loads(token_path.read_text())["token"] == "renewed-access"


def authorize(client: DjangoClient):
    redirect = client.get("/auth/connect", follow=False)
    parameters = parse_qs(urlparse(redirect.headers["location"]).query)
    return client.get(
        "/auth/callback",
        query_params={"state": parameters["state"][0], "code": "test-code"},
        follow=False,
    )


def seed_account(client: DjangoClient, tmp_path: Path) -> None:
    Account.objects.create(pk=1, email="me@example.com")
    (tmp_path / "token.json").write_text(json.dumps({"scopes": gmail.SCOPES}))


def synced(client: DjangoClient, tmp_path: Path, api: MagicMock) -> None:
    seed_account(client, tmp_path)
    pipeline.synchronize()
    assert client.get("/").status_code == 200
    gmail.sync(api)
    api.reset_mock()
    api.messages.return_value.list.return_value.execute.side_effect = api.default_list
    api.history.return_value.list.return_value.execute.side_effect = None
    api.history.return_value.list.return_value.execute.return_value = {
        "historyId": "110"
    }


def test_oauth_callback_rejects_mismatched_state(client, oauth_ready, monkeypatch):
    client.get("/auth/connect")
    exchange = MagicMock()
    monkeypatch.setattr(Flow, "fetch_token", exchange)
    response = client.get(
        "/auth/callback", query_params={"state": "forged", "code": "test-code"}
    )
    assert response.status_code == 400
    exchange.assert_not_called()


def test_denied_consent(
    client: DjangoClient,
    tmp_path: Path,
    oauth_ready,
    api: MagicMock,
) -> None:
    response = client.get("/auth/connect", follow=False)
    state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    response = client.get(
        "/auth/callback", query_params={"state": state, "error": "access_denied"}
    )
    assert response.status_code == 400
    assert not (tmp_path / "token.json").exists()


def test_different_account_is_not_silently_replaced(
    client: DjangoClient,
    tmp_path: Path,
    oauth_ready,
    api: MagicMock,
) -> None:
    synced(client, tmp_path, api)
    before = snapshot()
    api.getProfile.return_value.execute.return_value["emailAddress"] = (
        "other@example.com"
    )
    assert authorize(client).status_code == 409
    assert snapshot() == before
    assert json.loads((tmp_path / "token.json").read_text()) == {"scopes": gmail.SCOPES}


def test_expired_credentials_keep_cached_inbox(
    client: DjangoClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api: MagicMock,
) -> None:
    synced(client, tmp_path, api)
    before = snapshot()
    monkeypatch.setattr(
        gmail, "service", MagicMock(side_effect=RefreshError("revoked"))
    )
    with pytest.raises(RefreshError):
        pipeline.synchronize()
    response = client.get("/")
    assert response.status_code == 401
    assert "Reconnect" in response.text
    assert snapshot() == before
    assert Message.objects.count() == 3


def test_reconnect_preserves_cache_and_cursor(
    client: DjangoClient,
    tmp_path: Path,
    oauth_ready,
    api: MagicMock,
) -> None:
    synced(client, tmp_path, api)
    before = snapshot()
    assert authorize(client).status_code == 303
    assert snapshot() == before
    assert "private-refresh" in (tmp_path / "token.json").read_text()


@pytest.fixture
def web_credentials() -> dict[str, Any]:
    return {
        "web": {
            "client_id": "upload-client.apps.googleusercontent.com",
            "client_secret": "upload-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [oauth.REDIRECT_URI],
        }
    }


def test_rejects_invalid_credentials_upload(client, tmp_path):
    response = client.post(
        "/auth/credentials", data={"credentials": credentials_file(b"not json")}
    )
    assert response.status_code == 400
    assert not (tmp_path / "credentials.json").exists()


def test_credentials_upload_does_not_overwrite_existing_file(
    client: DjangoClient, tmp_path: Path, web_credentials: dict[str, Any]
) -> None:
    path = tmp_path / "credentials.json"
    path.write_text("existing owner configuration")
    response = client.post(
        "/auth/credentials", data={"credentials": credentials_file(web_credentials)}
    )
    assert response.status_code == 409
    assert path.read_text() == "existing owner configuration"


def add_tab(client: DjangoClient):
    response = client.post("/tabs/new/", data={"name": "Humans"})
    assert response.status_code == 303
    return Tab.objects.get(name="Humans").pk


def create_label(client: DjangoClient):
    return client.post("/tabs/new/", data={"name": "Receipts"})


def sender_rule(client: DjangoClient) -> None:
    response = client.post(
        "/tabs/new/",
        data={"name": "Humans", "people": "HUMAN@example.com\nhuman@example.com"},
    )
    assert response.status_code == 303
    assert Tab.objects.get().people == ["human@example.com"]


def enable_ai(client: DjangoClient) -> None:
    assert (
        client.post(
            "/tabs/new/",
            data={
                "name": "Humans",
                "description": "Personal messages from people",
                "auto_classify": "on",
            },
        ).status_code
        == 303
    )
    response = client.post(
        "/settings/",
        data={"enabled": "on", "api_key": "secret-test-key"},
    )
    assert response.status_code == 303
    assert "secret-test-key" not in response.text


def jev_response(state, questions):
    from typesafe_sdk import Noul, SystemOneResponse

    answers = {}
    for key, question in questions.items():
        if isinstance(question, Noul):
            answers[key] = {
                "type": "noul",
                "noul": 0.1 if state["email"]["subject"] == "Subject b" else 0.9,
            }
        else:
            highest = len(question.criteria) - 1
            answers[key] = {
                "type": "score",
                "score": highest * 0.75,
                "confidence": 0.8,
                "legend": dict(enumerate(question.criteria)),
                "probabilities": {0: 0.25, highest: 0.75},
            }
    return SystemOneResponse.model_validate(
        {
            "model": DEFAULT_MODEL,
            "usage": {"input_tokens": 1000, "output_tokens": 10},
            "answers": answers,
        }
    )


def measured_ai(monkeypatch: pytest.MonkeyPatch):
    client = MagicMock()
    client.__enter__.return_value = client
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        return jev_response(**kwargs)

    client.system_one.side_effect = respond
    monkeypatch.setattr(labeling, "TypeSafeClient", MagicMock(return_value=client))
    return calls, client


def conversation_ready(client, tmp_path, api) -> None:
    synced(client, tmp_path, api)
    api.mailbox["old"]["threadId"] = "thread-a"
    api.mailbox["old"]["labelIds"] = []
    for identifier, labels, sender, days in (
        ("sent", ["SENT"], "Me <me@example.com>", 1),
        ("reply", ["INBOX", "UNREAD"], "Other <other@example.com>", 0),
        ("draft", ["DRAFT"], "Me <me@example.com>", 0),
    ):
        api.mailbox[identifier] = mail(identifier, days=days, labels=labels)
        api.mailbox[identifier]["threadId"] = "thread-a"
        api.mailbox[identifier]["payload"]["headers"] = [
            {"name": "From", "value": sender},
            {"name": "To", "value": "Human <human@example.com>"},
            {"name": "Subject", "value": "A conversation"},
        ]


# A valid tiny PNG, not just bytes mislabeled as an image.
INLINE_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a4Z8AAAAASUVORK5CYII="


def formatted_message_ready(client, tmp_path, api) -> None:
    seed_account(client, tmp_path)
    html = """<html><head><style>@import url(https://tracker.example/style);</style><base href="http://localhost:8002/"><meta http-equiv="refresh" content="0;url=https://tracker.example/refresh"></head><body onload="alert('XSS')"><table style="background-color: #ffeecc; width: 90%"><tr><td><strong>Welcome</strong><p style="color: red; background-image:url(https://tracker.example/css);position:fixed">Formatted message</p><img src="cid:logo" alt="Logo"><img src="https://tracker.example/pixel" alt="Remote image"><a href="https://example.com/read" onclick="alert('XSS')">Read more</a><a href="/refresh/">Bad relative link</a><a href="javascript:alert('XSS')">Bad script link</a><script>window.top.pwned=1;fetch('/settings/')</script><form action="/refresh/"><button>Submit</button></form><iframe src="/settings/"></iframe><svg onload="alert('XSS')"></svg></td></tr></table></body></html>"""
    api.mailbox["a"]["payload"].update(
        mimeType="multipart/related",
        parts=[
            {
                "mimeType": "text/plain",
                "body": {
                    "data": base64.urlsafe_b64encode(b"Plain message fallback").decode()
                },
            },
            {
                "mimeType": "text/html",
                "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()},
            },
            {
                "mimeType": "image/png",
                "filename": "logo.png",
                "headers": [{"name": "Content-ID", "value": "<logo>"}],
                "body": {
                    "attachmentId": "logo-attachment",
                    "size": len(base64.b64decode(INLINE_PNG)),
                },
            },
            {
                "mimeType": "application/pdf",
                "filename": "invoice.pdf",
                "body": {"attachmentId": "never-fetch", "size": 100},
            },
        ],
    )
    api.mailbox["a"]["payload"].pop("body", None)
    api.messages.return_value.attachments.return_value.get.return_value.execute.return_value = {
        "data": INLINE_PNG
    }


@scenario(
    "inbox.feature",
    "Prepare email state without exposing private content",
)
def test_email_state_preserves_body_and_exposes_only_allowed_metadata():
    pass


@given(
    "a message contains markup-like text and private rich content",
    target_fixture="mail_state_input",
)
def mail_state_input():
    body = '</content><message id="injected">&\x00नमस्ते\n' * 100
    message = Message(
        id="real-gmail-id",
        received_at=66_600_000,
        body=body,
        sender='Human "A" <human@example.com>',
        subject='A <subject> & "quote"',
        recipients={
            "To": ["Original <me@example.com>"],
            "Cc": [],
            "Delivered-To": ["me@example.com"],
        },
        attachment_count=2,
        rich_body={
            "html": "PRIVATE HTML",
            "images": {"PRIVATE CID": "PRIVATE DATA"},
            "attachments": [{"name": 'invoice & "bill".pdf', "id": "PRIVATE PART"}],
        },
    )
    return body, message


@when(
    "the message state is prepared for classification",
    target_fixture="prepared_mail_state",
)
def prepared_mail_state(mail_state_input):
    _body, message = mail_state_input
    return labeling._message_state(message, {"user_context": "Family & work"})


@then(
    "the state contains the full text and allowed metadata without changing the stored body"
)
def verify_safe_mail_state(mail_state_input, prepared_mail_state):
    body, message = mail_state_input
    state = prepared_mail_state
    email = state["email"]
    assert email["body"] == body
    assert email["received_at"] == "1970-01-02T00:00:00+05:30"
    assert email["sender"] == message.sender
    assert email["subject"] == message.subject
    assert email["attachment_count"] == 2
    assert email["attachments"] == ['invoice & "bill".pdf']
    # Empty headers are omitted; display names are preserved.
    assert email["recipients"] == {
        "To": ["Original <me@example.com>"],
        "Delivered-To": ["me@example.com"],
    }
    assert state["user_context"] == "Family & work"
    assert "PRIVATE" not in json.dumps(state) and message.id not in json.dumps(state)
    assert message.body == body


@pytest.mark.parametrize("attachment_count", [0, None])
def test_email_state_preserves_large_inputs_and_omits_absent_optional_fields(
    attachment_count,
):
    message = Message(
        id="a",
        received_at=NOW,
        body='\x00"ह' * 60_000,
        sender="s" * 1_001,
        subject="t" * 1_001,
        attachment_count=attachment_count,
    )
    context = "Context. " * 30_000
    state = labeling._message_state(message, {"user_context": context})
    assert state["email"]["body"] == message.body
    assert state["email"]["sender"] == message.sender
    assert state["email"]["subject"] == message.subject
    assert state["user_context"] == context
    assert "attachment_count" not in state["email"]
    assert "attachments" not in state["email"]
    assert "recipients" not in state["email"]


def test_configuration_readers_follow_django_data_directory(
    tmp_path, monkeypatch, settings
):
    directory = tmp_path / "alternate"
    directory.mkdir()
    settings.DATA_DIR = directory
    (directory / "typesafe.json").write_text(json.dumps({"api_key": "alternate-key"}))
    (directory / "token.json").write_text(json.dumps({"scopes": gmail.SCOPES}))
    (directory / "credentials.json").write_text("{}")
    load_credentials = MagicMock(return_value=MagicMock(valid=True))
    monkeypatch.setattr(
        gmail.Credentials, "from_authorized_user_file", load_credentials
    )
    monkeypatch.setattr(gmail, "build", MagicMock())
    flow = MagicMock()
    monkeypatch.setattr(oauth.Flow, "from_client_secrets_file", flow)
    assert classification_utils.settings()["api_key"] == "alternate-key"
    assert gmail.can_label()
    oauth.oauth_flow()
    assert flow.call_args.args == (str(directory / "credentials.json"),)
    with gmail.service():
        pass
    load_credentials.assert_called_once_with(directory / "token.json")
