"""The single connected Gmail account."""

from __future__ import annotations

from django.db import models


class Account(models.Model):
    id = models.IntegerField(primary_key=True, default=1)
    email = models.EmailField()
    history_id = models.TextField(null=True)
    synced_at = models.BigIntegerField(null=True)

    class Meta:
        # Keep the existing table adopted by the app-split migration, not accounts_account.
        db_table = "account"
        constraints = (
            models.CheckConstraint(condition=models.Q(id=1), name="one_account"),
        )
