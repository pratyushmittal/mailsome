"""Completion has been copied to Message; label decisions and usage are untouched."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("classifications", "0002_classification_policy_help_text"),
        ("inbox", "0007_message_ai_classified"),
    ]
    operations = [migrations.DeleteModel(name="Classification")]
