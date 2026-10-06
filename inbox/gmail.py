"""Gmail access, on-demand caching, and history synchronization."""

from __future__ import annotations

import base64
import json
import random
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any, cast

import google_auth_httplib2
import httplib2
from django.conf import settings
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Gmail methods are generated dynamically; stubs are only needed by the type checker.
if TYPE_CHECKING:
    from googleapiclient._apis.gmail.v1 import GmailResource
    from googleapiclient._apis.gmail.v1.schemas import (
        Filter,
        Label,
        ListMessagesResponse,
        Profile,
    )
    from googleapiclient._apis.gmail.v1.schemas import Message as GmailMessage

from accounts.models import Account
from inbox import utils
from inbox.models import Message
from mailsome.errors import APIError
from mailsome.utilities import atomic_write

MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
FILTER_SCOPE = "https://www.googleapis.com/auth/gmail.settings.basic"
SCOPES = [MODIFY_SCOPE, FILTER_SCOPE]


@contextmanager
def service(credentials: Credentials | None = None) -> Iterator[GmailResource]:
    """Own the Gmail transport; supplied OAuth credentials are not persisted before account validation."""
    # OAuth callbacks supply fresh credentials before the connected account is accepted.
    if credentials is None:
        credentials = Credentials.from_authorized_user_file(
            settings.DATA_DIR / "token.json"
        )
        # Access tokens expire independently of the Gmail history cursor.
        if not credentials.valid:
            credentials.refresh(Request())
            atomic_write(settings.DATA_DIR / "token.json", credentials.to_json())

    # Each operation owns its HTTP transport; httplib2 clients are not thread-safe.
    client = build(
        "gmail",
        "v1",
        cache_discovery=False,
        # Discovery accepts this duck-typed transport; its stubs only name Http.
        http=cast(
            httplib2.Http,
            google_auth_httplib2.AuthorizedHttp(
                credentials, http=httplib2.Http(timeout=30)
            ),
        ),
    )
    try:
        yield client
    finally:
        client.close()


def get_message_details(client: GmailResource, message_id: str) -> GmailMessage | None:
    """Fetch complete parsed email details, including inline text/HTML and MIME parts.

    Callers persist received data through the shared update function, which fills
    missing body caches. This avoids a second detail request when that message
    is later opened or classified. Existing cached messages still use lightweight
    label-only requests during sync.

    This function does not write to the cache or fetch separate attachment data;
    attachment references still require get_attachment() when the user opens them.
    Return None for a missing/deleted message (404); propagate other API errors.
    """
    try:
        return (
            client.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="full",
            )
            .execute(num_retries=2)
        )
    except HttpError as error:
        # A message can disappear between the list/history and detail requests.
        if error.resp.status == 404:
            return None
        raise


def _retryable_read_error(error: Exception) -> bool:
    """Retry only Gmail server failures and rate-limit responses."""
    if not isinstance(error, HttpError):
        return False
    if error.resp.status in {429, 500, 502, 503, 504}:
        return True
    details = error.error_details if isinstance(error.error_details, list) else []
    return error.resp.status == 403 and any(
        isinstance(item, dict)
        and item.get("reason") in {"rateLimitExceeded", "userRateLimitExceeded"}
        for item in details
    )


def _download_batch(
    client: GmailResource,
    message_ids: list[str],
    known: set[str],
) -> tuple[dict[str, GmailMessage | None], dict[str, Exception]]:
    """Execute one batch without persistence or retries; None marks a missing message."""
    downloaded: dict[str, GmailMessage | None] = {}
    errors: dict[str, Exception] = {}

    def received(message_id: str, response: Any, error: Exception | None) -> None:
        if isinstance(error, HttpError) and error.resp.status == 404:
            downloaded[message_id] = None
        elif error is not None:
            errors[message_id] = error
        else:
            downloaded[message_id] = cast("GmailMessage", response)

    batch = client.new_batch_http_request(callback=received)
    messages = client.users().messages()
    for message_id in message_ids:
        if message_id in known:
            request = messages.get(
                userId="me",
                id=message_id,
                format="minimal",
                fields="id,threadId,labelIds",
            )
        else:
            request = messages.get(userId="me", id=message_id, format="full")
        batch.add(request, request_id=message_id)
    try:
        batch.execute()
    except HttpError as error:
        errors.update(
            (message_id, error)
            for message_id in message_ids
            if message_id not in downloaded
        )
    return downloaded, errors


def update_messages(
    client: GmailResource,
    message_ids: list[str],
    *,
    labels_only: bool = False,
) -> None:
    """Persist batches of 50, retrying server and rate-limit failures up to twice."""
    known = (
        set(Message.objects.filter(pk__in=message_ids).values_list("id", flat=True))
        if labels_only
        else set()
    )
    for start in range(0, len(message_ids), 50):
        # Each batched get costs 20 units; 50 per 10s stays within Gmail's 6,000 units/min per user.
        if start:
            time.sleep(10)
        pending = message_ids[start : start + 50]
        failures: list[Exception] = []
        for attempt in range(3):
            downloaded, errors = _download_batch(client, pending, known)
            for message_id, response in downloaded.items():
                utils.apply_message_update(
                    response if response is not None else {"id": message_id},
                    labels_only=message_id in known,
                    deleted=response is None,
                )
            pending = []
            for message_id, error in errors.items():
                if attempt < 2 and _retryable_read_error(error):
                    pending.append(message_id)
                else:
                    failures.append(error)
            if not pending:
                break
            time.sleep(random.random() * 2 ** (attempt + 1))
        if failures:
            raise failures[0]


def get_message_page(
    client: GmailResource,
    *,
    query: str = "",
    labels: list[str] | None = None,
    page_token: str | None = None,
    size: int = 50,
) -> ListMessagesResponse:
    """Let Gmail select one page, preserving its order and opaque forward cursor."""
    return (
        client.users()
        .messages()
        .list(
            userId="me",
            q=query,
            labelIds=labels or [],
            pageToken=page_token,
            maxResults=size,
            includeSpamTrash=False,
        )
        .execute(num_retries=2)
    )


def get_messages_for_list(
    client: GmailResource,
    message_ids: list[str],
) -> list[Message]:
    """Load messages for IDs returned by a Gmail list/search, in that same order.

    Read existing rows without their large bodies. Fetch and save full details
    for each missing unique ID, including labels and bodies. Existing rows are
    reused, not downloaded again. Preserve duplicate IDs and skip deleted mail.
    """
    cached = {
        message.id: message
        for message in Message.objects.defer("body", "rich_body").filter(
            pk__in=message_ids
        )
    }
    missing = list(
        dict.fromkeys(
            message_id for message_id in message_ids if message_id not in cached
        )
    )
    update_messages(client, missing)
    cached.update(
        (message.id, message)
        for message in Message.objects.defer("body", "rich_body").filter(pk__in=missing)
    )
    return [cached[message_id] for message_id in message_ids if message_id in cached]


def sync(
    client: GmailResource,
) -> None:
    """Apply Gmail history changes and recover recent mail after cursor expiry.

    The sync worker calls this on startup, roughly every minute, and on Refresh
    or rule edits. With no cursor, capture one without downloading existing mail.
    On expiry, capture a fresh cursor before fetching mail since the last sync,
    overlapping by one minute. Date-based recovery cannot reconstruct deletions
    or changes to older mail.

    Fetch details for newly encountered messages and only labels for cached ones.
    Persist directly through the shared message-update function. Advance the cursor
    after each history page's updates succeed; failures replay only the unsaved pages.
    The sync timestamp advances only after the whole interval succeeds. AI
    independently scans stored mail.
    """
    account = Account.objects.get(pk=1)
    cursor = account.history_id
    page_token = None
    while cursor is not None:
        try:
            page = (
                client.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=cursor,
                    pageToken=page_token,
                    maxResults=500,
                )
                .execute(num_retries=2)
            )
        except HttpError as error:
            if error.resp.status != 404:
                raise
            cursor = None
            break

        changed: set[str] = set()
        deleted: set[str] = set()
        for event in page.get("history", []):
            for kind in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                changed.update(item["message"]["id"] for item in event.get(kind, []))
            deleted.update(
                item["message"]["id"] for item in event.get("messagesDeleted", [])
            )
        for message_id in sorted(deleted):
            utils.apply_message_update({"id": message_id}, deleted=True)
        update_messages(client, sorted(changed - deleted), labels_only=True)
        page_token = page.get("nextPageToken")
        # Checkpoint saved pages so a failed catch-up resumes here instead of replaying them.
        # A page without records has no record ID to resume from; keep the previous checkpoint.
        if page_token and page.get("history"):
            Account.objects.filter(pk=1).update(history_id=page["history"][-1]["id"])
        if not page_token:
            cursor = page["historyId"]
            break

    if cursor is None:
        # Anchor first so subsequent history includes changes during recovery.
        cursor = get_profile(client)["historyId"]
        if account.history_id is not None and account.synced_at is not None:
            query = f"after:{account.synced_at // 1000 - 60}"
            page_token = None
            has_more_pages = True
            while has_more_pages:
                # Large pages let update_messages pace its batches to the per-user quota.
                messages_page = get_message_page(
                    client, query=query, page_token=page_token, size=500
                )
                update_messages(
                    client,
                    [message["id"] for message in messages_page.get("messages", [])],
                    labels_only=True,
                )
                page_token = messages_page.get("nextPageToken")
                has_more_pages = bool(page_token)
    Account.objects.filter(pk=1).update(
        history_id=cursor, synced_at=int(time.time() * 1000)
    )


def get_thread_messages(client: GmailResource, thread_id: str) -> list[Message]:
    """Fetch conversation details and save current labels and missing bodies."""
    thread = (
        client.users()
        .threads()
        .get(
            userId="me",
            id=thread_id,
            format="full",
        )
        .execute(num_retries=2)
    )
    # Draft content can change; it is not part of the immutable-message store.
    received = [
        message
        for message in thread.get("messages", [])
        if "DRAFT" not in message.get("labelIds", [])
    ]
    for message in received:
        utils.apply_message_update(message)
    return list(
        Message.objects.filter(pk__in=[message["id"] for message in received]).order_by(
            "received_at", "id"
        )
    )


def get_sender_history(
    client: GmailResource,
    sender: str,
    page_token: str | None,
    *,
    all_mail: bool = False,
) -> tuple[list[Message], str | None]:
    """Reuse cached messages for one all-date page, retaining full details of cache misses."""
    page = get_message_page(
        client,
        query=f"from:{json.dumps(sender)}",
        page_token=page_token,
        size=20 if all_mail else 6,
    )
    messages = get_messages_for_list(
        client, [item["id"] for item in page.get("messages", [])]
    )
    return (
        [item for item in messages if item.sender_email == sender],
        page.get("nextPageToken"),
    )


def _granted_scopes() -> set[str]:
    path = settings.DATA_DIR / "token.json"
    # Existing read-only tokens remain useful for reading until the owner reconnects.
    if not path.exists():
        return set()
    return set(json.loads(path.read_text()).get("scopes", []))


def can_label() -> bool:
    return bool(
        {MODIFY_SCOPE, "https://mail.google.com/"}.intersection(_granted_scopes())
    )


def can_manage_filters() -> bool:
    return FILTER_SCOPE in _granted_scopes()


def list_labels(client: GmailResource) -> list[Label]:
    return (
        client.users()
        .labels()
        .list(userId="me")
        .execute(num_retries=2)
        .get("labels", [])
    )


def get_profile(client: GmailResource) -> Profile:
    return client.users().getProfile(userId="me").execute(num_retries=2)


def create_label(client: GmailResource, name: str) -> Label:
    # Creation is not retried blindly: a lost response can leave the label installed.
    return client.users().labels().create(userId="me", body={"name": name}).execute()


def get_attachment(client: GmailResource, message_id: str, attachment_id: str) -> str:
    """Return Gmail's base64url data; callers validate MIME membership and download limits."""
    return (
        client.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute(num_retries=2)["data"]
    )


def add_label_to_message(
    client: GmailResource, message_id: str, label_id: str
) -> GmailMessage:
    """Return confirmed message state for single-message actions such as unsubscribe."""
    return (
        client.users()
        .messages()
        .modify(userId="me", id=message_id, body={"addLabelIds": [label_id]})
        .execute(num_retries=2)
    )


def add_label_to_messages(
    client: GmailResource, message_ids: list[str], label_id: str
) -> None:
    """Write up to 1,000 messages; callers acknowledge decisions, history refreshes labels."""
    client.users().messages().batchModify(
        userId="me", body={"ids": message_ids, "addLabelIds": [label_id]}
    ).execute(num_retries=2)


def remove_label_from_messages(
    client: GmailResource, message_ids: list[str], label_id: str
) -> None:
    """Remove one label in chunks of up to 1,000; callers own reset policy and refresh."""
    for start in range(0, len(message_ids), 1_000):
        client.users().messages().batchModify(
            userId="me",
            body={
                "ids": message_ids[start : start + 1_000],
                "removeLabelIds": [label_id],
            },
        ).execute(num_retries=2)


def send_message(
    client: GmailResource, mail: EmailMessage, thread_id: str | None = None
) -> GmailMessage:
    """Send once; a thread ID files a reply in its Gmail conversation."""
    body: GmailMessage = {"raw": base64.urlsafe_b64encode(mail.as_bytes()).decode()}
    # New mail starts its own conversation.
    if thread_id:
        body["threadId"] = thread_id
    # No automatic retries: a retried send can deliver the same email twice.
    return client.users().messages().send(userId="me", body=body).execute()


def archive_thread(client: GmailResource, thread_id: str) -> None:
    client.users().threads().modify(
        userId="me", id=thread_id, body={"removeLabelIds": ["INBOX"]}
    ).execute(num_retries=2)


def list_filters(client: GmailResource) -> list[Filter]:
    return (
        client.users()
        .settings()
        .filters()
        .list(userId="me")
        .execute(num_retries=2)
        .get("filter", [])
    )


def create_filter(client: GmailResource, body: Filter) -> Filter:
    # On an uncertain creation, the next edit lists exact matches instead of creating duplicates.
    return (
        client.users()
        .settings()
        .filters()
        .create(userId="me", body=body)
        .execute(num_retries=0)
    )


def delete_filter(client: GmailResource, filter_id: str) -> None:
    try:
        client.users().settings().filters().delete(userId="me", id=filter_id).execute(
            num_retries=0
        )
    except HttpError as error:
        # A previous delete may have succeeded before acknowledgement, or in another session.
        if error.resp.status != 404:
            raise


def _sender_filter_body(label_id: str, senders: list[str]) -> Filter:
    # Quote addresses as data, not Gmail operators; matching itself belongs to Gmail.
    return {
        "criteria": {
            "query": "{"
            + " ".join(
                f"from:{json.dumps(sender, ensure_ascii=False)}"
                for sender in sorted(set(senders))
            )
            + "}"
        },
        "action": {"addLabelIds": [label_id]},
    }


def _filter_matches(remote: Mapping[str, Any], body: Filter) -> bool:
    # Gmail can return empty optional fields; extra nonempty criteria/actions must match too.
    return {
        key: {
            name: value
            for name, value in remote.get(key, {}).items()
            if value not in (None, "", [], False)
        }
        for key in ("criteria", "action")
    } == body


def replace_sender_filter(
    client: GmailResource, label_id: str, previous: list[str], senders: list[str]
) -> None:
    """Replace exact sender criteria/actions without claiming ownership of other filters."""
    remote = list_filters(client)
    previous_body = _sender_filter_body(label_id, previous)
    desired_body = _sender_filter_body(label_id, senders)
    old = [item for item in remote if previous and _filter_matches(item, previous_body)]
    desired = [
        item for item in remote if senders and _filter_matches(item, desired_body)
    ]
    # Duplicate exact matches are ambiguous, including user-created copies.
    if len(old) > 1 or len(desired) > 1:
        raise APIError(
            409,
            "Multiple matching Gmail filters found. Remove duplicates in Gmail and retry this edit.",
        )
    # After an uncertain creation, reuse the exact match instead of creating duplicates.
    if senders and not desired:
        result = create_filter(client, desired_body)
        # Do not remove the old rule without confirmation that replacement succeeded.
        if not isinstance(result.get("id"), str) or not result["id"]:
            raise APIError(
                502,
                "Gmail did not confirm filter creation. Retry this edit to check the result.",
            )
    for item in old:
        delete_filter(client, item["id"])
