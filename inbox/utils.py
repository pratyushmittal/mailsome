"""Gmail message conversion, metadata persistence, and header parsing utilities."""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from django.db.models import Q

from inbox.content import attachment_count, message_text, rich_body
from inbox.models import Message

if TYPE_CHECKING:
    from googleapiclient._apis.gmail.v1.schemas import Message as GmailMessage

RECIPIENT_HEADERS = ("To", "Cc", "Bcc", "Delivered-To")


def save_or_create_message(message: GmailMessage) -> None:
    """Save received metadata and labels, filling only missing body caches.

    The caller serializes the Gmail fetch and save with mailbox_lock. Update
    explicit metadata fields, never overwrite AI completion or downloaded bodies.
    Preserve attachment counts when the response lacks enough MIME structure.
    """
    converted = message_from_gmail(message)
    # Only these metadata fields may overwrite an existing row; never save the whole converted instance.
    updates: dict[str, Any] = {
        "thread_id": converted.thread_id,
        "sender": converted.sender,
        "subject": converted.subject,
        "received_at": converted.received_at,
        "labels": converted.labels,
        "unsubscribe": converted.unsubscribe,
        "recipients": converted.recipients,
    }
    # Header-only responses must not erase previously fetched MIME descriptors.
    if converted.attachment_count is not None:
        updates["attachment_count"] = converted.attachment_count
    Message.objects.update_or_create(pk=converted.pk, defaults=updates)

    # Legacy header-only responses cannot tell us whether this message has any body.
    payload = message.get("payload", {})
    if not payload.get("mimeType"):
        return

    missing_body = Message.objects.filter(pk=converted.pk, body__isnull=True)
    # Conditional writes preserve content filled by another reader during this request.
    if missing_body.exists():
        missing_body.update(body=message_text(payload))
    missing_rich = Message.objects.filter(pk=converted.pk).filter(
        Q(rich_body__isnull=True) | ~Q(rich_body__has_key="attachments")
    )
    # Older formatted caches need attachment descriptors; otherwise keep their downloaded content.
    previous = missing_rich.values_list("rich_body").first()
    if previous is not None:
        # Fill absent formatted fields (notably attachment descriptors), keeping existing content.
        missing_rich.update(rich_body={**rich_body(payload), **(previous[0] or {})})


def message_from_gmail(message: GmailMessage) -> Message:
    """Convert Gmail metadata into an unsaved local Message, without fetching content.

    Map IDs, headers, epoch-millisecond received time, labels, attachment count,
    and a validated unsubscribe link. Preserve repeated recipient headers and
    display names. Missing MIME details leave attachment_count unknown (None).

    Requires id, threadId, and internalDate; optional headers use defaults.
    Use save_or_create_message() for persistence: saving this instance over a cached
    row would overwrite body and classification state with model defaults.
    """
    headers = {
        header["name"].lower(): header["value"]
        for header in message.get("payload", {}).get("headers", [])
    }
    return Message(
        id=message["id"],
        thread_id=message["threadId"],
        sender=headers.get("from", ""),
        subject=headers.get("subject", "(No subject)"),
        received_at=int(message["internalDate"]),
        labels=list(message.get("labelIds", [])),
        attachment_count=attachment_count(message.get("payload", {})),
        unsubscribe=_unsubscribe_link(headers.get("list-unsubscribe", "")),
        # Preserve repeated headers and display names; never substitute the connected account.
        recipients={
            name: [
                header["value"]
                for header in message.get("payload", {}).get("headers", [])
                if header["name"].casefold() == name.casefold()
                and header["value"].strip()
            ]
            for name in RECIPIENT_HEADERS
        },
    )


def _unsubscribe_link(header: str) -> str:
    """Extract a supported destination from the email's List-Unsubscribe header.

    Scan the first 16,000 characters for angle-bracketed URLs, e.g.
    '<mailto:leave@example.com>, <https://example.com/unsubscribe>'. Ignore
    candidates containing whitespace, control characters, or backslashes.

    Prefer the first HTTPS URL with a public-looking hostname and no embedded
    credentials; reject IP literals and the local-domain suffixes checked below.
    If none qualifies, use the first mailto URL containing one supported address,
    no fragment, and only optional subject/body query fields without controls.
    Re-encode those query fields before returning the mailto URL.

    Return an empty string when no candidate qualifies. This only parses the
    advertised header: it does not inspect the body, resolve hosts, visit links,
    send mail, or confirm that the destination actually unsubscribes the user.
    """
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
