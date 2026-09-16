from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inbox", "0005_message_rich_body")]

    operations = [
        migrations.AddField(
            "message", "attachment_count", models.PositiveIntegerField(null=True)
        ),
    ]
