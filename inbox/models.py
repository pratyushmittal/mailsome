"""On-demand mail cache and durable inbox preferences."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parseaddr

from django.db import models


class Tab(models.Model):
    """A pinned Gmail label and local rules/settings, not a mirror of all Gmail labels."""

    name = models.CharField(max_length=225)
    query = models.TextField(default="", db_default="")
    label_id = models.TextField(
        null=True,
        unique=True,
        help_text=(
            "Gmail's ID for the label pinned by this tab, not a local database ID. "
            "Null for legacy query-only tabs without an associated Gmail label."
        ),
    )
    description = models.TextField(default="", db_default="")
    # Preserve existing exact-sender lists during this framework migration.
    people = models.JSONField(default=list, db_default=[])
    auto_classify = models.BooleanField(default=False, db_default=False)
    position = models.IntegerField(default=0, db_default=0)

    class Meta:
        db_table = "tabs"
        ordering = ("position", "id")


class MessageManager(models.Manager["Message"]):
    def inbox(self) -> models.QuerySet[Message]:
        """Select locally cached inbox mail, excluding spam, trash, and drafts."""
        # Match quoted JSON array values, not substrings within Gmail label IDs.
        return self.filter(labels__icontains='"INBOX"').exclude(
            models.Q(labels__icontains='"SPAM"')
            | models.Q(labels__icontains='"TRASH"')
            | models.Q(labels__icontains='"DRAFT"')
        )


class Message(models.Model):
    objects = MessageManager()

    id = models.TextField(primary_key=True)
    thread_id = models.TextField()
    sender = models.TextField()
    subject = models.TextField()
    received_at = models.BigIntegerField(db_index=True)
    labels = models.JSONField(
        default=list,
        help_text="Cached Gmail label IDs, refreshed by history sync and message/thread detail reads.",
    )
    # NULL means old cache metadata has not yet been checked for attachments.
    attachment_count = models.PositiveIntegerField(null=True)
    body = models.TextField(null=True)
    ai_classified = models.BooleanField(
        default=False,
        db_default=False,
        help_text="AI has evaluated this message, including when no labels matched. Only explicit reclassification clears this flag.",
    )
    # NULL means formatted MIME content has not been fetched yet.
    rich_body = models.JSONField(null=True)
    unsubscribe = models.TextField(null=True)
    # NULL marks old cache rows whose recipient headers have not been fetched yet.
    recipients = models.JSONField(null=True)

    # Set by views for the current navigation context; never persisted.
    url: str

    @property
    def sender_email(self) -> str:
        return parseaddr(self.sender)[1].casefold()

    @property
    def date(self) -> datetime:
        return datetime.fromtimestamp(self.received_at / 1000, UTC)

    class Meta:
        db_table = "messages"
        ordering = ("-received_at", "id")


class Sender(models.Model):
    email = models.EmailField(primary_key=True)
    note = models.TextField(default="", db_default="")

    class Meta:
        db_table = "senders"
