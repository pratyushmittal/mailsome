"""Replace exact sender-filter matches only when a tab's rules are edited."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from inbox import gmail
from mailsome.errors import APIError

if TYPE_CHECKING:
    from googleapiclient._apis.gmail.v1.schemas import Filter


def _body(label_id: str, senders: list[str]) -> Filter:
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


def _matches(remote: Mapping[str, Any], body: Filter) -> bool:
    # Gmail can return empty optional fields; extra nonempty criteria/actions must match too.
    return {
        key: {
            name: value
            for name, value in remote.get(key, {}).items()
            if value not in (None, "", [], False)
        }
        for key in ("criteria", "action")
    } == body


def replace(label_id: str | None, previous: list[str], senders: list[str]) -> None:
    """Called under the policy lock before saving an edit; failures leave it retryable.

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
        remote = gmail.list_filters(client)
        old = [
            item
            for item in remote
            if previous and _matches(item, _body(label_id, previous))
        ]
        desired = [
            item
            for item in remote
            if senders and _matches(item, _body(label_id, senders))
        ]
        # Duplicate exact matches are ambiguous, including user-created copies.
        if len(old) > 1 or len(desired) > 1:
            raise APIError(
                409,
                "Multiple matching Gmail filters found. Remove duplicates in Gmail and retry this edit.",
            )
        # Reuse an exact desired match after a lost create acknowledgement or failed delete.
        if senders and not desired:
            result = gmail.create_filter(client, _body(label_id, senders))
            # Do not remove the old rule without confirmation that replacement succeeded.
            if not isinstance(result.get("id"), str) or not result["id"]:
                raise APIError(
                    502,
                    "Gmail did not confirm filter creation. Retry this edit to check the result.",
                )
        for item in old:
            gmail.delete_filter(client, item["id"])
