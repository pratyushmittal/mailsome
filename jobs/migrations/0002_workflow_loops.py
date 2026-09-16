"""Replace task identities with pending flags; do not transfer old queued work."""

from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def reset_work(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    alias = schema_editor.connection.alias
    work = apps.get_model("jobs", "Work").objects.using(alias)
    requests = apps.get_model("classifications", "AIRequest").objects.using(alias)
    # An unfinished charge still needs a retry gate, even if its workflow row was lost.
    if requests.filter(status="running").exists():
        work.get_or_create(kind="labeling")
    requests.filter(status="running").update(
        status="interrupted", error_kind="process_stopped", cost_usd=None
    )
    work.update(retry_ai=False)
    work.filter(kind="sync").update(progress={"status": "idle"})
    work.filter(kind="labeling").update(
        progress={
            "status": "failed",
            "needs_retry": True,
            "error": "Labeling paused after switching workers. Click Refresh to resume; interrupted AI cost may be unknown.",
        }
    )


class Migration(migrations.Migration):
    dependencies = [
        ("jobs", "0001_initial"),
        ("classifications", "0001_initial"),
    ]

    # Discarded requests cannot safely be restored as runnable queue rows.
    operations = [
        migrations.AddField("work", "pending", models.BooleanField(default=False)),
        migrations.RunPython(reset_work),
        migrations.RemoveField("work", "task_id"),
        migrations.RemoveField("work", "generation"),
    ]
