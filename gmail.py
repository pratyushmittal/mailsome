"""Gmail access and bounded, restart-safe synchronization."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from email.message import Message
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import google_auth_httplib2
import html2text
import httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Gmail methods are generated dynamically; stubs are only needed by the type checker.
if TYPE_CHECKING:
    from googleapiclient._apis.gmail.v1 import GmailResource

from store import cache_message, database, message_summary

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
WINDOW_MS = 14 * 24 * 60 * 60 * 1000
Progress = Callable[[str, int, int | None], None]


def cutoff_time() -> int:
    return int(time.time() * 1000) - WINDOW_MS


def private_write(path: Path, content: str) -> None:
    # Atomic replacement prevents a crash from leaving a half-written token file.
    temporary = path.with_suffix(".tmp")
    with os.fdopen(
        os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w"
    ) as file:
        file.write(content)
    temporary.chmod(0o600)
    temporary.replace(path)


@contextmanager
def service(directory: Path) -> Iterator[GmailResource]:
    credentials = Credentials.from_authorized_user_file(directory / "token.json")
    # Access tokens expire independently of the Gmail history cursor.
    if not credentials.valid:
        credentials.refresh(Request())
        private_write(directory / "token.json", credentials.to_json())

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


def get_message(
    client: GmailResource, message_id: str, *, full: bool = False
) -> dict[str, Any] | None:
    options = (
        {} if full else {"metadataHeaders": ["From", "Subject", "List-Unsubscribe"]}
    )
    try:
        return dict(
            client.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="full" if full else "metadata",
                **options,
            )
            .execute(num_retries=2)
        )
    except HttpError as error:
        # A message can disappear between the list/history and detail requests.
        if error.resp.status == 404:
            return None
        raise


def update_message(
    db: sqlite3.Connection, client: GmailResource, message_id: str, cutoff: int
) -> None:
    message = get_message(client, message_id)
    # A permanent deletion is also reflected in the local cache.
    if message is None:
        db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        return
    cache_message(db, message, cutoff)


def apply_history(
    db: sqlite3.Connection,
    client: GmailResource,
    cursor: str,
    cutoff: int,
    report: Progress,
) -> str:
    report("history", 0, None)
    pages = 0
    known = {row[0] for row in db.execute("SELECT id FROM messages")}
    changed: set[str] = set()
    deleted: set[str] = set()
    page_token = None
    while True:
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
        pages += 1
        report("history", pages, None)
        for event in page.get("history", []):
            for item in event.get("messagesAdded", []):
                changed.add(item["message"]["id"])
            for kind in ("labelsAdded", "labelsRemoved"):
                for item in event.get(kind, []):
                    # Unknown mail only needs fetching when it enters the inbox.
                    if item["message"]["id"] in known or (
                        kind == "labelsAdded" and "INBOX" in item["labelIds"]
                    ):
                        changed.add(item["message"]["id"])
            deleted.update(
                item["message"]["id"] for item in event.get("messagesDeleted", [])
            )

        page_token = page.get("nextPageToken")
        # Only the final page's cursor is safe to persist after applying all changes.
        if not page_token:
            break

    report("changes", 0, len(changed - deleted))
    for completed, message_id in enumerate(sorted(changed - deleted), start=1):
        update_message(db, client, message_id, cutoff)
        report("changes", completed, len(changed - deleted))
    db.executemany("DELETE FROM messages WHERE id = ?", [(value,) for value in deleted])
    return page["historyId"]


def inbox_ids(
    client: GmailResource,
    cutoff: int,
    query: str = "",
    report: Progress = lambda stage, completed, total: None,
) -> set[str]:
    """Use Gmail's query parser for both the bounded sync and custom tabs."""
    report("listing", 0, None)
    listed: set[str] = set()
    page_token = None
    while True:
        page = (
            client.users()
            .messages()
            .list(
                userId="me",
                labelIds=["INBOX"],
                q=f"after:{cutoff // 1000}" + (f" ({query})" if query else ""),
                pageToken=page_token,
                maxResults=500,
                includeSpamTrash=False,
            )
            .execute(num_retries=2)
        )
        listed.update(message["id"] for message in page.get("messages", []))
        report("listing", len(listed), None)
        page_token = page.get("nextPageToken")
        # Gmail omits nextPageToken when the bounded inbox snapshot is complete.
        if not page_token:
            break

    return listed


def rebuild(
    db: sqlite3.Connection, client: GmailResource, cutoff: int, report: Progress
) -> str:
    # Capture the cursor first, then replay changes that happen during the snapshot.
    cursor = client.users().getProfile(userId="me").execute(num_retries=2)["historyId"]
    listed = inbox_ids(client, cutoff, report=report)

    # Count actual IDs across all pages, rather than presenting Gmail's estimate as exact.
    report("headers", 0, len(listed))
    for completed, message_id in enumerate(sorted(listed), start=1):
        update_message(db, client, message_id, cutoff)
        report("headers", completed, len(listed))

    stale = {row[0] for row in db.execute("SELECT id FROM messages")} - listed
    db.executemany("DELETE FROM messages WHERE id = ?", [(value,) for value in stale])
    return apply_history(db, client, cursor, cutoff, report)


def sync(
    directory: Path,
    client: GmailResource,
    *,
    report: Progress = lambda stage, completed, total: None,
) -> None:
    cutoff = cutoff_time()
    with database(directory) as db:
        cursor = db.execute("SELECT history_id FROM account WHERE id = 1").fetchone()[0]
        # The first connection has no cursor; later refreshes use mailbox history.
        if cursor is None:
            cursor = rebuild(db, client, cutoff, report)
        else:
            try:
                cursor = apply_history(db, client, cursor, cutoff, report)
            except HttpError as error:
                # Expired history requires only a bounded rebuild, never all mail.
                if error.resp.status != 404:
                    raise
                cursor = rebuild(db, client, cutoff, report)

        report("saving", 0, None)
        db.execute("DELETE FROM messages WHERE received_at < ?", (cutoff,))
        # Cache changes and cursor commit together. Failures leave both unchanged.
        db.execute(
            "UPDATE account SET history_id = ?, synced_at = ? WHERE id = 1",
            (cursor, int(time.time() * 1000)),
        )


def message_text(payload: dict[str, Any]) -> str:
    """Prefer plain text; render HTML-only messages as inert Markdown text."""
    texts: dict[str, list[str]] = {"text/plain": [], "text/html": []}
    parts = [payload]
    while parts:
        part = parts.pop(0)
        headers = {
            header["name"].lower(): header["value"]
            for header in part.get("headers", [])
        }
        # Attachments (including attached emails) aren't part of the message reader.
        if part.get("filename") or headers.get(
            "content-disposition", ""
        ).lower().startswith("attachment"):
            continue
        parts.extend(part.get("parts", []))
        data = part.get("body", {}).get("data")
        # Some bodies are attachment-backed; don't download those automatically.
        if part.get("mimeType") not in texts or not data:
            continue
        content_type = Message()
        content_type["Content-Type"] = headers.get("content-type", part["mimeType"])
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        try:
            text = raw.decode(
                content_type.get_content_charset() or "utf-8", errors="replace"
            )
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        texts[part["mimeType"]].append(text)

    # HTML is never inserted into the page, so scripts and tracking images cannot run.
    if texts["text/plain"]:
        return "\n\n".join(texts["text/plain"])
    return (
        html2text.html2text("\n\n".join(texts["text/html"]))
        or "No inline text body is available."
    )


def read_message(directory: Path, client: GmailResource, message_id: str) -> str | None:
    with database(directory) as db:
        message = get_message(client, message_id, full=True)
        # Mail may leave the inbox while the cached list is displayed.
        if message is None or not cache_message(db, message, cutoff_time()):
            db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            return None
        body = message_text(message.get("payload", {}))
        db.execute("UPDATE messages SET body = ? WHERE id = ?", (body, message_id))
        return body


def sender_history(
    client: GmailResource,
    sender: str,
    page_token: str | None,
    *,
    individual: bool = False,
    all_mail: bool = False,
) -> dict[str, Any]:
    """Fetch one page of conversation headers, including older and archived mail."""
    # The action sidebar shows individual emails, including others in the same conversation.
    if individual:
        page = (
            client.users()
            .messages()
            .list(
                userId="me",
                q=f"from:{json.dumps(sender)}",
                maxResults=20 if all_mail else 6,
                pageToken=page_token,
                includeSpamTrash=False,
            )
            .execute(num_retries=2)
        )
        summaries = []
        for item in page.get("messages", []):
            message = get_message(client, item["id"])
            # Messages can disappear after listing; Gmail search may also match aliases.
            if (
                message is not None
                and message_summary(message)["sender_email"] == sender
            ):
                summaries.append(message_summary(message))
        return {
            "messages": sorted(
                summaries, key=lambda item: item["received_at"], reverse=True
            ),
            "next_page": page.get("nextPageToken"),
        }
    page = (
        client.users()
        .threads()
        .list(
            userId="me",
            q=f"from:{json.dumps(sender)}",
            maxResults=10,
            pageToken=page_token,
            includeSpamTrash=False,
        )
        .execute(num_retries=2)
    )
    conversations = []
    for thread in page.get("threads", []):
        try:
            details = (
                client.users()
                .threads()
                .get(
                    userId="me",
                    id=thread["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject"],
                )
                .execute(num_retries=2)
            )
        except HttpError as error:
            # A conversation may be deleted after it appears in the search results.
            if error.resp.status == 404:
                continue
            raise
        summaries = [
            message_summary(dict(message)) for message in details.get("messages", [])
        ]
        # Search can match another participant; show only messages from the exact sender.
        matching = [
            message for message in summaries if message["sender_email"] == sender
        ]
        if matching:
            conversations.append(
                max(matching, key=lambda message: message["received_at"])
            )
    return {
        "messages": sorted(
            conversations, key=lambda message: message["received_at"], reverse=True
        ),
        "next_page": page.get("nextPageToken"),
    }


def can_label(directory: Path) -> bool:
    path = directory / "token.json"
    # Existing read-only tokens remain useful for reading until the owner reconnects.
    if not path.exists():
        return False
    scopes = json.loads(path.read_text()).get("scopes", [])
    return bool(set(SCOPES + ["https://mail.google.com/"]).intersection(scopes))


def list_labels(client: GmailResource) -> list[dict[str, Any]]:
    return [
        dict(label)
        for label in client.users()
        .labels()
        .list(userId="me")
        .execute(num_retries=2)
        .get("labels", [])
    ]


def apply_label(
    directory: Path, client: GmailResource, message_id: str, label_id: str
) -> None:
    """Read current labels first; retries only add, and never change read status."""
    message = get_message(client, message_id)
    with database(directory) as db:
        # Mail can leave the bounded inbox between classification and the Gmail write.
        if message is None or not cache_message(db, message, cutoff_time()):
            db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            return
        # A previous attempt or the owner may already have applied this label.
        if label_id not in message.get("labelIds", []):
            updated = (
                client.users()
                .messages()
                .modify(userId="me", id=message_id, body={"addLabelIds": [label_id]})
                .execute(num_retries=2)
            )
            message["labelIds"] = updated["labelIds"]
            cache_message(db, message, cutoff_time())
        db.execute(
            "UPDATE label_decisions SET applied = 1 WHERE message_id = ? AND label_id = ?",
            (message_id, label_id),
        )
