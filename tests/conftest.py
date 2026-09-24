"""Shared test setup, discovered automatically by pytest; tests need not import it.

These fixtures keep tests off the owner's database and credentials while exercising
real Django migrations, persistence, and background workers.

There are two isolation layers:
- One migrated, file-backed test database per session. Worker connections see the
  same committed rows. pytest-django creates and resets this database.
- A fresh directory per test for fake credentials and config files. Database
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
    from django.db import connections

    connections.settings["default"]["TEST"]["NAME"] = str(directory / "test.sqlite3")
    yield
    # configure() changes the process-wide mask; don't leak it beyond the test session.
    os.umask(original_umask)


@pytest.fixture(autouse=True)
def isolated_mailbox(
    transactional_db,
    django_db_reset_sequences,
    settings,
    tmp_path,
):
    """Give every test clean rows, predictable IDs and private per-test config files.

    transactional_db permits real commits visible to worker threads (no enclosing
    test transaction). pytest-django flushes rows and restores settings afterwards.
    """
    settings.DATA_DIR = tmp_path
    settings.SECRET_KEY = "mailsome-test-secret"
    from jobs import pipeline

    pipeline.sync_requested.clear()


@pytest.fixture
def upgrade_database(tmp_path):
    """Let upgrade tests build an old schema in a disposable DB, never rewind the shared DB."""
    from django.db import connection

    original = connection.settings_dict["NAME"]
    connection.close()
    connection.settings_dict["NAME"] = str(tmp_path / "upgrade.sqlite3")
    try:
        yield
    finally:
        connection.close()
        connection.settings_dict["NAME"] = original
