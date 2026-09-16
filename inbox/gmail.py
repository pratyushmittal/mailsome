"""Gmail access, on-demand caching, and history synchronization."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

import google_auth_httplib2
import httplib2
from django.conf import settings
from django.db import transaction
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
from inbox.models import Message
from inbox.utils import message_from_gmail, save_or_create_message
from jobs.runtime import mailbox_lock

MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
FILTER_SCOPE = "https://www.googleapis.com/auth/gmail.settings.basic"
SCOPES = [MODIFY_SCOPE, FILTER_SCOPE]
Progress = Callable[[str, int, int | None], None]


def atomic_write(path: Path, content: str) -> None:
    """Replace a file via a sibling temporary file, keeping owner-only (0600) permissions."""
    # Readers see the old or complete new file, never partially written content.
    temporary = path.with_suffix(".tmp")
    with os.fdopen(
        os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w"
    ) as file:
        file.write(content)
    temporary.chmod(0o600)
    temporary.replace(path)


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

    Callers persist the response with save_or_create_message(), which also fills
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


def update_message(
    client: GmailResource, message_id: str, *, labels_only: bool = False
) -> None:
    """Fetch and save current message state, serializing with other mailbox reads/writes.

    Normally fetch full details and fill missing bodies without overwriting saved
    content or AI completion. History sync uses labels_only=True for cached mail
    to avoid downloading content again. A Gmail 404 removes the cached message.
    Callers must not already hold mailbox_lock; this function owns fetch/save locking.
    """
    with mailbox_lock():
        message = (
            _get_message_labels(client, message_id)
            if labels_only
            else get_message_details(client, message_id)
        )
        with transaction.atomic():
            # A message can be deleted between its listing/history event and this fetch.
            if message is None:
                Message.objects.filter(pk=message_id).delete()
            elif labels_only:
                Message.objects.filter(pk=message_id).update(
                    labels=message.get("labelIds", [])
                )
            else:
                save_or_create_message(message)


def _get_message_labels(client: GmailResource, message_id: str) -> GmailMessage | None:
    """Read current labels for a cached message touched by a history event.

    Full details also include labels, but history sync does not need to download
    saved bodies again. Return None on deletion; other provider errors propagate.
    Browsing saves labels whenever full details arrive; AI uses the local snapshot.
    """
    try:
        return (
            client.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="minimal",
                fields="id,threadId,labelIds",
            )
            .execute(num_retries=2)
        )
    except HttpError as error:
        # History or a page can reference mail deleted before its details are read.
        if error.resp.status == 404:
            return None
        raise


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
    *,
    report: Progress = lambda stage, completed, total: None,
) -> list[Message]:
    """Load messages for IDs returned by a Gmail list/search, in that same order.

    Read existing rows without their large bodies. Fetch and save full details
    for each missing unique ID, including labels and bodies. Existing rows are
    reused, not downloaded again. Preserve duplicate IDs and skip deleted mail.
    The report callback counts missing-ID requests, including deletions.
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
    report("headers", 0, len(missing))
    for index, message_id in enumerate(missing):
        update_message(client, message_id)
        report("headers", index + 1, len(missing))
    cached.update(
        (message.id, message)
        for message in Message.objects.defer("body", "rich_body").filter(pk__in=missing)
    )
    return [cached[message_id] for message_id in message_ids if message_id in cached]


def sync(
    client: GmailResource,
    *,
    report: Progress = lambda stage, completed, total: None,
) -> None:
    """Apply Gmail history changes; never list or preload an inbox window.

    The sync worker calls this on startup, roughly every minute, and on Refresh
    or rule edits. With no cursor, capture one without downloading existing mail.
    An expired cursor is replaced too: keep cached mail and AI decisions, accepting
    that changes in the lost interval stay stale until a later read or event.

    Fetch details for newly encountered messages and only labels for cached ones.
    Each fetch/save holds mailbox_lock so browsing cannot overwrite it out of
    order; release the lock between messages so readers need not wait for a whole
    sync. Save progress directly, but advance the cursor only after all history
    pages succeed. Failures replay that interval safely on the next pass.
    Sender rules and AI run afterward in the worker, not in this function.
    """
    cursor = Account.objects.get(pk=1).history_id
    page_token = None
    completed = 0
    while cursor is not None:
        report("history", completed, None)
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
            # Expired history loses an interval; keep the cache and resume from a fresh anchor.
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
        with mailbox_lock():
            Message.objects.filter(pk__in=deleted).delete()
        for message_id in sorted(changed - deleted):
            update_message(
                client,
                message_id,
                labels_only=Message.objects.filter(pk=message_id).exists(),
            )
            completed += 1
            report("changes", completed, None)
        page_token = page.get("nextPageToken")
        # A page token is not a durable history cursor; acknowledge only the completed interval.
        if not page_token:
            cursor = page["historyId"]
            break

    # First connection and expired history start here, without a separate message preload.
    if cursor is None:
        cursor = get_profile(client)["historyId"]
    Account.objects.filter(pk=1).update(
        history_id=cursor, synced_at=int(time.time() * 1000)
    )
    report("complete", completed, completed)


def get_thread_messages(client: GmailResource, thread_id: str) -> list[Message]:
    """Fetch conversation details and save current labels and missing bodies."""
    with mailbox_lock():
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
        for message in thread.get("messages", []):
            # Draft content can change; it is not part of this immutable-message cache.
            if "DRAFT" not in message.get("labelIds", []):
                save_or_create_message(message)
    return sorted(
        [
            message_from_gmail(message)
            for message in thread.get("messages", [])
            if "DRAFT" not in message.get("labelIds", [])
        ],
        key=lambda message: (message.received_at, message.id),
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
    client: GmailResource, messages: list[Message], label_id: str
) -> None:
    """Write up to 1,000 messages; callers acknowledge decisions, history refreshes labels."""
    client.users().messages().batchModify(
        userId="me",
        body={"ids": [message.id for message in messages], "addLabelIds": [label_id]},
    ).execute(num_retries=2)


def remove_label_from_messages(
    client: GmailResource, messages: list[Message], label_id: str
) -> None:
    """Remove one label from up to 1,000 messages; history refreshes cached labels."""
    client.users().messages().batchModify(
        userId="me",
        body={
            "ids": [message.id for message in messages],
            "removeLabelIds": [label_id],
        },
    ).execute(num_retries=2)


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
