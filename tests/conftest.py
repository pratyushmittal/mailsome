"""Shared test setup, discovered automatically by pytest; tests need not import it.

These fixtures keep tests off the owner's database and credentials while exercising
real Django migrations, SQLite transactions, worker threads and process locks.

There are two isolation layers:
- One migrated, file-backed test database per session. Worker connections see the
  same committed rows, and tests exercise file-backed SQLite locking rather than
  relying on an in-memory database. pytest-django creates and resets this database.
- A fresh directory per test for fake credentials, config and lock files. Database
  rows and ID sequences are reset between tests; the session database path stays put.

Upgrade tests can opt into a third, disposable database at an older schema version.
They never rewind the shared database through irreversible data migrations.
"""

import os

import pytest


@pytest.fixture(scope="session")
def django_db_modify_db_settings(
    django_db_modify_db_settings_parallel_suffix, tmp_path_factory
):
    """Override pytest-django's settings hook before it creates the test database.

    The parallel-suffix dependency preserves pytest-django's setup ordering;
    tmp_path_factory gives each session/worker its own unique directory.
    """
    from django.conf import settings

    directory = tmp_path_factory.mktemp("django-db")
    from mailsome.bootstrap import configure

    # Exercise production's private-file startup against an isolated directory only.
    original_umask = os.umask(0o077)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MAILSOME_DATA_DIR", str(directory))
        patch.setenv("MAILSOME_SESSION_SECRET", "mailsome-test-secret")
        configure()
    settings.SECRET_KEY = "mailsome-test-secret"
    settings.DATA_DIR = directory
    # Explicit TEST.NAME avoids both the owner's DB and Django's default in-memory SQLite DB.
    settings.DATABASES["default"]["TEST"]["NAME"] = str(directory / "test.sqlite3")
    yield
    # configure() changes the process-wide mask; don't leak it beyond the test session.
    os.umask(original_umask)


@pytest.fixture(autouse=True)
def isolated_mailbox(transactional_db, django_db_reset_sequences, settings, tmp_path):
    """Give every test clean rows, predictable IDs and private per-test config files.

    transactional_db permits real commits visible to worker threads (no enclosing
    test transaction). pytest-django flushes rows and restores settings afterwards.
    """
    settings.DATA_DIR = tmp_path
    settings.SECRET_KEY = "mailsome-test-secret"


@pytest.fixture
def pre_workflow_database(tmp_path, request):
    """Opt-in database for testing upgrades from the old Work schema.

    jobs/0002 discards obsolete queued requests and cannot safely be reversed.
    Build a separate DB up to jobs/0001 instead, with other apps at their latest
    migrations, or before the classification-model removal when parametrized with
    "classification". The test can seed old rows and migrate forward, then this fixture
    reconnects later tests to the normal session DB even if the assertion fails.
    """
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    original = connection.settings_dict["NAME"]
    # Close before changing NAME so no query accidentally uses the old open connection.
    connection.close()
    connection.settings_dict["NAME"] = str(tmp_path / "upgrade.sqlite3")
    try:
        executor = MigrationExecutor(connection)
        targets = [
            node if node[0] != "jobs" else ("jobs", "0001_initial")
            for node in executor.loader.graph.leaf_nodes()
        ]
        # App-split/import tests seed historical Classification rows before their later removal.
        if getattr(request, "param", None) == "classification":
            targets = [
                {
                    "inbox": ("inbox", "0006_message_attachment_count"),
                    "classifications": (
                        "classifications",
                        "0002_classification_policy_help_text",
                    ),
                }.get(node[0], node)
                for node in targets
            ]
        executor.migrate(targets)
        yield
    finally:
        # Restore pytest's database even when an upgrade assertion fails.
        connection.close()
        connection.settings_dict["NAME"] = original
