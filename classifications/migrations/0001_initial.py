"""Adopt existing inbox tables without creating or copying them."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [("inbox", "0003_split_model_state")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.CreateModel(
                    name="Classification",
                    fields=[
                        (
                            "message",
                            models.OneToOneField(
                                on_delete=django.db.models.deletion.CASCADE,
                                primary_key=True,
                                serialize=False,
                                to="inbox.message",
                            ),
                        ),
                        ("policy", models.TextField()),
                    ],
                    options={
                        "db_table": "classifications",
                    },
                ),
                migrations.CreateModel(
                    name="LabelDecision",
                    fields=[
                        (
                            "pk",
                            models.CompositePrimaryKey(
                                "message_id",
                                "label_id",
                                "source",
                                blank=True,
                                editable=False,
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                        ("label_id", models.TextField()),
                        ("source", models.CharField(max_length=16)),
                        ("reason", models.TextField()),
                        (
                            "applied",
                            models.BooleanField(db_default=False, default=False),
                        ),
                        (
                            "message",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                to="inbox.message",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "label_decisions",
                    },
                ),
                migrations.CreateModel(
                    name="AITracking",
                    fields=[
                        (
                            "id",
                            models.IntegerField(
                                default=1, primary_key=True, serialize=False
                            ),
                        ),
                        ("started_at", models.BigIntegerField()),
                    ],
                    options={
                        "db_table": "ai_tracking",
                        "constraints": [
                            models.CheckConstraint(
                                condition=models.Q(("id", 1)), name="one_tracking_start"
                            )
                        ],
                    },
                ),
                migrations.CreateModel(
                    name="AIRequest",
                    fields=[
                        (
                            "id",
                            models.AutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        ("started_at", models.BigIntegerField()),
                        ("finished_at", models.BigIntegerField(null=True)),
                        ("model", models.TextField()),
                        ("reasoning", models.TextField()),
                        ("message_count", models.IntegerField()),
                        ("status", models.CharField(max_length=20)),
                        ("response_id", models.TextField(null=True)),
                        ("input_tokens", models.BigIntegerField(null=True)),
                        ("cached_tokens", models.BigIntegerField(null=True)),
                        ("cache_write_tokens", models.BigIntegerField(null=True)),
                        ("output_tokens", models.BigIntegerField(null=True)),
                        ("reasoning_tokens", models.BigIntegerField(null=True)),
                        ("cost_usd", models.FloatField(null=True)),
                        ("pricing", models.JSONField(null=True)),
                        ("error_kind", models.CharField(max_length=60, null=True)),
                    ],
                    options={
                        "db_table": "ai_requests",
                    },
                ),
            ],
        ),
    ]
