"""Durable classification decisions and AI accounting."""

from __future__ import annotations

from django.db import models


class LabelDecision(models.Model):
    """Saved AI instructions and legacy sender decisions, not Gmail's current labels.

    History and detail reads cache Gmail label IDs in Message.labels. Sender rules
    use cached inbox mail directly; legacy sender decisions remain historical records.
    AI independently evaluates unchecked mail and records its matches here.
    Decisions are saved before Gmail writes so failures can retry without paying
    for AI again. Successful writes mark the decision applied; history sync updates
    Message.labels for changes made here or directly in Gmail.
    Applied decisions stay applied after manual removal, preventing reapplication.
    Archiving and aging preserve decisions; permanently deleting a message removes them.
    """

    class Source(models.TextChoices):
        SENDER = "sender", "Sender rule"
        AI = "ai", "AI classification"

    pk = models.CompositePrimaryKey("message_id", "label_id", "source")
    message = models.ForeignKey("inbox.Message", on_delete=models.CASCADE)
    label_id = models.TextField(
        help_text="Gmail label ID, not a local tab reference. Decisions survive unpinning a tab.",
    )
    source = models.CharField(max_length=16, choices=Source)
    reason = models.TextField()
    applied = models.BooleanField(
        default=False,
        db_default=False,
        help_text=(
            "This label was confirmed present in Gmail, either already there or after "
            "a successful write. Retained after manual removal to avoid reapplying it; "
            "not an indication that the label is currently present."
        ),
    )

    class Meta:
        db_table = "label_decisions"


class AIRequest(models.Model):
    started_at = models.BigIntegerField()
    finished_at = models.BigIntegerField(null=True)
    model = models.TextField()
    reasoning = models.TextField()
    message_count = models.IntegerField()
    status = models.CharField(max_length=20)
    response_id = models.TextField(null=True)
    input_tokens = models.BigIntegerField(null=True)
    cached_tokens = models.BigIntegerField(null=True)
    cache_write_tokens = models.BigIntegerField(null=True)
    output_tokens = models.BigIntegerField(null=True)
    reasoning_tokens = models.BigIntegerField(null=True)
    cost_usd = models.FloatField(null=True)
    pricing = models.JSONField(null=True)
    error_kind = models.CharField(max_length=60, null=True)

    class Meta:
        # Preserve the table adopted from the original app; Django defaults to classifications_airequest.
        db_table = "ai_requests"
