"""Adopt existing inbox tables without creating or copying them."""

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [("inbox", "0003_split_model_state")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.CreateModel(
                    name="Work",
                    fields=[
                        (
                            "kind",
                            models.CharField(
                                max_length=16, primary_key=True, serialize=False
                            ),
                        ),
                        ("task_id", models.CharField(max_length=36, null=True)),
                        ("generation", models.PositiveIntegerField(default=0)),
                        ("retry_ai", models.BooleanField(default=False)),
                        ("progress", models.JSONField(default=dict)),
                    ],
                    options={"db_table": "inbox_work"},
                ),
            ],
        ),
    ]
