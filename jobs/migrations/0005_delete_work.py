from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("jobs", "0004_reclassification_reset"),
    ]

    operations = [
        migrations.DeleteModel(name="Work"),
    ]
