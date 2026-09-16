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
                    name="Account",
                    fields=[
                        (
                            "id",
                            models.IntegerField(
                                default=1, primary_key=True, serialize=False
                            ),
                        ),
                        ("email", models.EmailField(max_length=254)),
                        ("history_id", models.TextField(null=True)),
                        ("synced_at", models.BigIntegerField(null=True)),
                    ],
                    options={
                        "db_table": "account",
                        "constraints": [
                            models.CheckConstraint(
                                condition=models.Q(("id", 1)), name="one_account"
                            )
                        ],
                    },
                ),
            ],
        ),
    ]
