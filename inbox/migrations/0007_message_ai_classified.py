"""Keep completed/no-match results on Message instead of a separate policy record."""

from django.db import migrations, models


def copy_completion(apps, schema_editor):
    alias = schema_editor.connection.alias
    completed = apps.get_model("classifications", "Classification").objects.using(alias)
    apps.get_model("inbox", "Message").objects.using(alias).filter(
        pk__in=completed.values("message_id")
    ).update(ai_classified=True)


class Migration(migrations.Migration):
    dependencies = [
        ("inbox", "0006_message_attachment_count"),
        ("classifications", "0002_classification_policy_help_text"),
    ]
    operations = [
        migrations.AddField(
            model_name="message",
            name="ai_classified",
            field=models.BooleanField(
                default=False,
                db_default=False,
                help_text="AI has evaluated this message, including when no labels matched. Only explicit reclassification clears this flag.",
            ),
        ),
        migrations.RunPython(copy_completion, migrations.RunPython.noop),
    ]
