"""Move model ownership, leaving all mailbox tables and content-type IDs intact."""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

MOVED_MODELS = {
    "accounts": ("account",),
    "classifications": ("classification", "labeldecision", "airequest", "aitracking"),
    "jobs": ("work",),
}


def move_content_types(
    apps: Apps, schema_editor: BaseDatabaseSchemaEditor, *, reverse: bool = False
) -> None:
    content_types = apps.get_model("contenttypes", "ContentType").objects.using(
        schema_editor.connection.alias
    )
    for app_label, names in MOVED_MODELS.items():
        source, target = (app_label, "inbox") if reverse else ("inbox", app_label)
        # Update in place: generic relations must retain their content-type IDs.
        # Fresh installs have no content types yet; post_migrate creates them.
        content_types.filter(app_label=source, model__in=names).update(app_label=target)


def forwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    move_content_types(apps, schema_editor)


def backwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    move_content_types(apps, schema_editor, reverse=True)


class Migration(migrations.Migration):
    dependencies = [
        ("inbox", "0001_initial"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.DeleteModel(name="Account"),
                migrations.DeleteModel(name="Classification"),
                migrations.DeleteModel(name="LabelDecision"),
                migrations.DeleteModel(name="AIRequest"),
                migrations.DeleteModel(name="AITracking"),
                migrations.DeleteModel(name="Work"),
            ],
        ),
        migrations.RunPython(forwards, backwards),
    ]
