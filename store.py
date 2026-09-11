"""A bounded inbox cache plus durable local preferences and usage accounting."""

import ipaddress
import json
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit


@contextmanager
def database(directory: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(directory / "mail.sqlite3", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    with database(directory) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                email TEXT NOT NULL,
                history_id TEXT,
                synced_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS tabs (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                query TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                subject TEXT NOT NULL,
                received_at INTEGER NOT NULL,
                labels TEXT NOT NULL,
                body TEXT
            );
        """)
        # Existing cached bodies lack unsubscribe metadata; NULL asks the reader to fetch headers once.
        if "unsubscribe" not in {
            row[1] for row in db.execute("PRAGMA table_info(messages)")
        }:
            db.execute("ALTER TABLE messages ADD COLUMN unsubscribe TEXT")
        db.execute(
            "CREATE TABLE IF NOT EXISTS senders (email TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT '')"
        )
        # Preserve query tabs from the earlier UI until the owner converts or removes them.
        columns = {row[1] for row in db.execute("PRAGMA table_info(tabs)")}
        for name, definition in {
            "label_id": "TEXT",
            "description": "TEXT NOT NULL DEFAULT ''",
            "people": "TEXT NOT NULL DEFAULT '[]'",
            "auto_classify": "INTEGER NOT NULL DEFAULT 0",
            "position": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            # Older installations lack the label settings; this migration is restart-safe.
            if name not in columns:
                db.execute(f"ALTER TABLE tabs ADD COLUMN {name} {definition}")
        # Seed the original ID order only once; later startups preserve the owner's ordering.
        if "position" not in columns:
            db.execute("UPDATE tabs SET position = id")
        db.executescript("""
            CREATE UNIQUE INDEX IF NOT EXISTS tabs_label ON tabs(label_id);
            CREATE TABLE IF NOT EXISTS ai_tracking (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                started_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_requests (
                id INTEGER PRIMARY KEY,
                started_at INTEGER NOT NULL,
                finished_at INTEGER,
                model TEXT NOT NULL,
                reasoning TEXT NOT NULL,
                message_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                response_id TEXT,
                input_tokens INTEGER,
                cached_tokens INTEGER,
                cache_write_tokens INTEGER,
                output_tokens INTEGER,
                reasoning_tokens INTEGER,
                cost_usd REAL,
                pricing TEXT NOT NULL,
                error_kind TEXT
            );
            CREATE TABLE IF NOT EXISTS classifications (
                message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                policy TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS label_decisions (
                message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                label_id TEXT NOT NULL,
                source TEXT NOT NULL,
                reason TEXT NOT NULL,
                applied INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(message_id, label_id, source)
            );
        """)
        # Accounting outlives the rolling mail cache and starts only when tracking is installed.
        db.execute(
            "INSERT OR IGNORE INTO ai_tracking VALUES (1, ?)",
            (int(time.time() * 1000),),
        )
    (directory / "mail.sqlite3").chmod(0o600)


def cache_message(db: sqlite3.Connection, message: dict[str, Any], cutoff: int) -> bool:
    labels = message.get("labelIds", [])
    # Messages may have been archived, trashed, or aged out since listing them.
    if (
        "INBOX" not in labels
        or {"TRASH", "SPAM"}.intersection(labels)
        or int(message["internalDate"]) < cutoff
    ):
        db.execute("DELETE FROM messages WHERE id = ?", (message["id"],))
        return False

    summary = message_summary(message)
    # Preserve a previously opened body when refreshing metadata or rebuilding.
    db.execute(
        """INSERT INTO messages
           (id, thread_id, sender, subject, received_at, labels, unsubscribe)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             thread_id = excluded.thread_id, sender = excluded.sender,
             subject = excluded.subject, received_at = excluded.received_at,
             labels = excluded.labels, unsubscribe = excluded.unsubscribe""",
        (
            message["id"],
            message["threadId"],
            summary["sender"],
            summary["subject"],
            int(message["internalDate"]),
            json.dumps(labels),
            summary["unsubscribe"],
        ),
    )
    return True


def message_summary(message: dict[str, Any]) -> dict[str, Any]:
    headers = {
        header["name"].lower(): header["value"]
        for header in message.get("payload", {}).get("headers", [])
    }
    sender = headers.get("from", "")
    return {
        "id": message["id"],
        "thread_id": message["threadId"],
        "sender": sender,
        "sender_email": parseaddr(sender)[1].casefold(),
        "subject": headers.get("subject", "(No subject)"),
        "received_at": int(message["internalDate"]),
        "labels": message.get("labelIds", []),
        "unsubscribe": unsubscribe_link(headers.get("list-unsubscribe", "")),
    }


def unsubscribe_link(header: str) -> str:
    """Offer advertised links only; never fetch untrusted URLs on the server."""
    # Reject raw controls before URL parsing can silently strip them.
    links = [
        link
        for link in re.findall(r"<([^<>]+)>", header[:16_000])
        if "\\" not in link and not any(c.isspace() or ord(c) < 32 for c in link)
    ]
    for link in links:
        try:
            url = urlsplit(link)
            host = url.hostname or ""
            # Only public-looking HTTPS destinations; no credentials, local hosts or IP literals.
            if (
                url.scheme != "https"
                or url.username
                or url.password
                or "." not in host
                or host.endswith((".local", ".localhost", ".internal", "."))
            ):
                continue
            try:
                ipaddress.ip_address(host)
            except ValueError:
                return link
        except ValueError:
            continue  # Malformed ports/IPv6 headers are not usable unsubscribe links.
    # Mail-only lists open the owner's mail client, never send through our Gmail API.
    for link in links:
        try:
            url = urlsplit(link)
        except ValueError:
            continue  # Malformed mailto authorities must not prevent opening an email.
        fields = parse_qsl(url.query, keep_blank_values=True)
        # Permit a prefilled unsubscribe subject/body, never extra recipients or mail headers.
        if (
            url.scheme == "mailto"
            and re.fullmatch(
                r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                url.path,
            )
            and not url.fragment
            and all(
                name in {"subject", "body"} and not any(ord(c) < 32 for c in value)
                for name, value in fields
            )
        ):
            return "mailto:" + url.path + ("?" + urlencode(fields) if fields else "")
    return ""
