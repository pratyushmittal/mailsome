"""Cross-process mailbox coordination and durable, content-free progress."""

import fcntl
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction

from jobs.models import Work


@contextmanager
def file_lock(directory: Path, name: str, *, blocking: bool = True) -> Iterator[None]:
    with open(
        directory / name, "a", opener=lambda path, flags: os.open(path, flags, 0o600)
    ) as file:
        fcntl.flock(file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def mailbox_lock(*, blocking: bool = True) -> AbstractContextManager[None]:
    return file_lock(settings.DATA_DIR, "mailbox.lock", blocking=blocking)


def label_policy_lock() -> AbstractContextManager[None]:
    # Local tab edits must not wait for sync, but cannot race an authorized Gmail label write.
    return file_lock(settings.DATA_DIR, "label-policy.lock")


def get_progress(kind: str) -> dict[str, Any]:
    return Work.objects.filter(kind=kind).values_list(
        "progress", flat=True
    ).first() or {"status": "idle"}


def set_progress(kind: str, values: dict[str, Any]) -> None:
    # IMMEDIATE transactions serialize short progress updates without holding mailbox locks.
    with transaction.atomic():
        work, _ = Work.objects.get_or_create(kind=kind)
        work.progress = {**work.progress, **values}
        work.save(update_fields=["progress"])


def progress_state() -> dict[str, Any]:
    sync, labeling = get_progress("sync"), get_progress("labeling")
    return {
        "sync": sync,
        "labeling": labeling,
        "revision": max(sync.get("revision") or 0, labeling.get("revision") or 0),
    }
