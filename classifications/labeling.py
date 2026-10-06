"""Evaluate stored mail with Jev; one request per email, all questions together."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.db.models import F
from typesafe_sdk import (
    Noul,
    NoulCriteria,
    RetryPolicy,
    Score,
    SystemOneResponse,
    TypeSafeClient,
)

from classifications import usage, utils
from classifications.models import LabelDecision
from inbox import gmail
from inbox.models import Message, Tab

BATCH_SIZE = 100  # Bound each worker pass, not the number of emails in an API request.


def _message_state(message: Message, config: dict[str, Any]) -> dict[str, Any]:
    """Send stored text and metadata, never Gmail IDs, HTML, or attachment contents."""
    # Omit absent attachments and recipients instead of sending empty fields.
    optional: dict[str, Any] = {}
    if message.attachment_count:
        optional["attachment_count"] = message.attachment_count
        optional["attachments"] = [
            part["name"] for part in message.rich_body["attachments"]
        ]
    # Recipients reveal forwarding and who else received the mail; skip empty headers.
    # NULL marks rows whose recipient headers were never fetched.
    recipients = {
        name: values for name, values in (message.recipients or {}).items() if values
    }
    if recipients:
        optional["recipients"] = recipients
    # The SDK handles JSON serialization; keep the stored text unchanged.
    return {
        "user_context": config["user_context"],
        "email": {
            "sender": message.sender,
            "subject": message.subject,
            "received_at": datetime.fromtimestamp(
                message.received_at / 1000, ZoneInfo("Asia/Kolkata")
            ).isoformat(),
            **optional,
            "body": message.body,
        },
    }


def _save_classification(
    message: Message, labels: list[Tab], response: SystemOneResponse, levels: int
) -> None:
    # Resolve all expected SDK answers before any persistence; no generated-ID mapping.
    probabilities = {tab.pk: response.nouls[f"tab_{tab.pk}"].noul for tab in labels}
    importance = response.scores["importance"].score / (levels - 1)
    selected = Message.objects.inbox().filter(pk=message.pk)
    if not selected.exists():
        return
    for tab in labels:
        probability = probabilities[tab.pk]
        if probability < tab.acceptance_threshold:
            continue
        LabelDecision.objects.update_or_create(
            message_id=message.pk,
            label_id=tab.label_id,
            source=LabelDecision.Source.AI,
            defaults={
                "ai_score": probability,
                "reason": "",
                "applied": LabelDecision.objects.filter(
                    message_id=message.pk, label_id=tab.label_id, applied=True
                ).exists(),
            },
        )
    # Completion follows all saved decisions; failed persistence remains retryable.
    selected.update(importance=importance, ai_classified=True)


def process() -> bool:
    """Process up to 100 emails sequentially; the existing worker owns periodic retries."""
    config = utils.settings()
    if not config["enabled"]:
        return False
    pending = list(Message.objects.classifiable()[:BATCH_SIZE])
    if pending:
        labels = list(
            Tab.objects.filter(auto_classify=True, label_id__isnull=False).order_by(
                "pk"
            )
        )
        if labels and not gmail.can_label():
            raise ValueError("Reconnect Gmail to grant label access.")
        questions: dict[str, Noul | Score] = {
            f"tab_{tab.pk}": Noul(
                instructions={
                    "task": "Does `email` belong to this category? Use `user_context` as background.",
                    "category": tab.name,
                    "description": tab.description,
                },
                criteria=NoulCriteria(
                    true="The email matches the described category.",
                    false="The email does not match the described category.",
                ),
            )
            for tab in labels
        }
        questions["importance"] = Score(
            instructions="Rate the importance of `email` to the recipient using `user_context` and the ordered levels.",
            criteria=config["importance_levels"],
        )
        with TypeSafeClient(
            api_key=config["api_key"],
            model=config["model"],
            retry=RetryPolicy(max_retries=0),
            timeout=60,
        ) as client:
            for message in pending:
                state = _message_state(message, config)
                Message.objects.filter(pk=message.pk).update(
                    ai_attempts=F("ai_attempts") + 1
                )
                request_id = usage.start(config, message_count=1)
                response = None
                status, error_kind = "failed", "request_failed"
                try:
                    response = client.system_one(state=state, questions=questions)
                    _save_classification(
                        message, labels, response, len(config["importance_levels"])
                    )
                    status, error_kind = "completed", None
                finally:
                    usage.finish(request_id, status, response, error_kind)
    utils.apply_pending_labels()
    return Message.objects.classifiable().exists()
