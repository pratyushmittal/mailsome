"""Classification settings, reset policy, and acknowledgments of Gmail label writes."""

from __future__ import annotations

import json
from typing import Any

from django.conf import settings as django_settings

from classifications.models import LabelDecision
from inbox import gmail
from inbox.models import Message, Tab

DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_IMPORTANCE_LEVELS = [
    "Low importance: routine automated messages such as OTP codes and login notifications.",
    "Normal importance: personal correspondence and useful updates without an urgent deadline.",
    "Highest importance: time-sensitive matters such as expiring subscriptions and upcoming meetings.",
]


def settings() -> dict[str, Any]:
    """Read server-side AI settings and optional user-provided mail context.

    Missing configuration defaults to AI disabled; this does not contact TypeSafe.
    The returned API key is for internal use only, never a browser response.
    """
    path = django_settings.DATA_DIR / "typesafe.json"
    return {
        "enabled": False,
        "api_key": django_settings.TYPESAFE_API_KEY,
        "model": DEFAULT_MODEL,
        "importance_levels": DEFAULT_IMPORTANCE_LEVELS,
        "importance_threshold": 0.7,
        "user_context": "",
        **(json.loads(path.read_text()) if path.exists() else {}),
    }


def enabled_labels() -> list[dict[str, str]]:
    """Return pinned labels enabled for future AI assignments, independent of sender rules."""
    return [
        {"id": tab.label_id, "name": tab.name, "description": tab.description}
        for tab in Tab.objects.order_by("id")
        if tab.label_id and tab.auto_classify
    ]


def _reset_targets(selection: dict[str, list[str]]) -> dict[str, list[str]]:
    """Select confirmed inbox message/label pairs not protected by current sender rules."""
    messages = list(
        Message.objects.inbox()
        .filter(pk__in=selection["message_ids"])
        .only("id", "sender")
    )
    # Lost history can hide assignments; remove even when the cached label is absent.
    return {
        tab.label_id: [
            message.id for message in messages if message.sender_email not in tab.people
        ]
        for tab in Tab.objects.filter(
            label_id__in=selection["label_ids"], auto_classify=True
        )
    }


def reset_classifications(selection: dict[str, list[str]]) -> None:
    """Remove labels in Gmail, then make selected mail eligible for the worker.

    Current sender rules protect each message/label pair. Keep decisions and
    completion flags until removals succeed. History sync refreshes cached labels;
    AI writes do not rely on them.
    """
    from jobs.pipeline import sync_requested

    targets = _reset_targets(selection)
    # Importance-only setups have no labels to remove and may lack label access.
    if targets:
        with gmail.service() as client:
            for label_id, message_ids in targets.items():
                gmail.remove_label_from_messages(client, message_ids, label_id)
        sync_requested.set()

    selected = Message.objects.inbox().filter(pk__in=selection["message_ids"])
    # Clear applied history too, including obsolete sender decisions: current Tab.people owns protection.
    LabelDecision.objects.filter(message__in=selected, label_id__in=targets).delete()
    selected.update(ai_classified=False, ai_attempts=0, importance=None)


def apply_pending_labels() -> None:
    """Bulk-add saved AI decisions without paying again."""
    from jobs.pipeline import sync_requested

    pending = LabelDecision.objects.filter(
        source=LabelDecision.Source.AI,
        applied=False,
        message__in=Message.objects.inbox(),
    )
    for label_id in list(pending.values_list("label_id", flat=True).distinct()):
        # Opt-out stops even saved writes.
        if not settings()["enabled"] or not gmail.can_label():
            return
        # A tab may have been removed or disabled since selecting its pending label.
        if not Tab.objects.filter(label_id=label_id, auto_classify=True).exists():
            continue
        message_ids = list(
            pending.filter(label_id=label_id).values_list("message_id", flat=True)
        )
        # Sync or another authorized write may have cleared these pending decisions.
        if not message_ids:
            continue
        with gmail.service() as client:
            for start in range(0, len(message_ids), 1_000):
                batch = message_ids[start : start + 1_000]
                # Write even labels the cache shows: it lags after resets, and re-adding is a no-op.
                gmail.add_label_to_messages(client, batch, label_id)
                LabelDecision.objects.filter(
                    message_id__in=batch,
                    label_id=label_id,
                    source=LabelDecision.Source.AI,
                    applied=False,
                ).update(applied=True)
                sync_requested.set()
