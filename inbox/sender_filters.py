"""Sender-label rules: edit exact Gmail filters and apply rules to stored inbox mail."""

from __future__ import annotations

from inbox import gmail
from inbox.models import Message, Tab
from mailsome.errors import APIError


def replace(label_id: str | None, previous: list[str], senders: list[str]) -> None:
    """Called before saving a sender-rule edit; failures leave it retryable.

    Exact user-created matches are intentionally indistinguishable from ours.
    Never infer ownership from the label alone or guess between duplicate matches.
    """
    # Description/order edits and legacy query tabs do not change Gmail filters.
    if not label_id or set(previous) == set(senders):
        return
    # Fail the edit explicitly; there is no background filter setup queue anymore.
    if not gmail.can_manage_filters():
        raise APIError(
            403,
            "Reconnect Gmail and grant settings access before changing sender rules.",
        )
    with gmail.service() as client:
        gmail.replace_sender_filter(client, label_id, previous, senders)


def apply_sender_rules() -> None:
    """Add missing sender labels to locally cached inbox mail; recompute each pass."""
    from jobs.pipeline import sync_requested

    # Read-only accounts still sync, but cannot apply local sender rules.
    if not gmail.can_label():
        return
    with gmail.service() as client:
        messages = list(Message.objects.inbox().defer("body", "rich_body"))
        for tab in Tab.objects.exclude(label_id=None).exclude(people=[]):
            matches = [
                message
                for message in messages
                if message.sender_email in tab.people
                and tab.label_id not in message.labels
            ]
            for start in range(0, len(matches), 1_000):
                gmail.add_label_to_messages(
                    client, matches[start : start + 1_000], tab.label_id
                )
                sync_requested.set()
