from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inbox", "0004_message_recipients")]

    operations = [
        migrations.AddField("message", "rich_body", models.JSONField(null=True)),
    ]
