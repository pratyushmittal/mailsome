"""Describe the saved configuration fingerprint; no data or storage changes."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("classifications", "0001_initial")]

    operations = [
        migrations.AlterField(
            model_name="classification",
            name="policy",
            field=models.TextField(
                help_text=(
                    "SHA-256 fingerprint of the AI-enabled labels and AI settings used for "
                    "this classification. Identifies results produced under a different configuration."
                )
            ),
        ),
    ]
