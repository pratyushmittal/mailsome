"""Durable mailbox workflow progress."""

from __future__ import annotations

from django.db import models


class Work(models.Model):
    """One durable request/progress record per mailbox workflow, not per email."""

    kind = models.CharField(max_length=16, primary_key=True)
    pending = models.BooleanField(default=False)
    retry_ai = models.BooleanField(default=False)
    progress = models.JSONField(default=dict)
    reclassification = models.JSONField(
        default=dict,
        db_default={},
        help_text="Sync-only reset selection: message_ids and label_ids. Retained through failures until label removal and cache refresh succeed; never exposed in progress.",
    )
    retry_at = models.BigIntegerField(
        default=0,
        db_default=0,
        help_text="Earliest automatic quota retry, in Unix milliseconds; Refresh cannot bypass it.",
    )
    retry_delay = models.PositiveIntegerField(
        default=0,
        db_default=0,
        help_text="Previous quota cooldown in seconds, for exponential backoff.",
    )

    class Meta:
        db_table = "inbox_work"
