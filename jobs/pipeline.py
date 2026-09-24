"""Independent periodic Gmail sync and database-driven AI classification."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event, Thread
from time import monotonic

from django.conf import settings
from django.db import close_old_connections, connections

from accounts.models import Account
from classifications import labeling, usage
from inbox import gmail, sender_filters
from mailsome.errors import gmail_retry_delay

logger = logging.getLogger(__name__)
sync_requested = Event()


def _classify(stopped: Event) -> None:
    """Drain eligible stored mail in bounded batches, then wait for the next pass."""
    try:
        while not stopped.is_set():
            close_old_connections()
            try:
                while not stopped.is_set() and labeling.process():
                    pass
            except Exception as error:  # noqa: BLE001 — persisted attempts bound periodic retries.
                logger.warning(
                    "Classification failed (%s); a later pass will retry",
                    type(error).__name__,
                )
            stopped.wait(settings.SYNC_INTERVAL)
    finally:
        connections.close_all()


def synchronize() -> None:
    """Only history sync advances the cursor, after its direct writes succeed."""
    if (
        Account.objects.filter(pk=1).exists()
        and (settings.DATA_DIR / "token.json").exists()
    ):
        with gmail.service() as client:
            gmail.sync(client)
        sender_filters.apply_sender_rules()


def _sync(stopped: Event) -> None:
    retry_at = 0.0
    try:
        while not stopped.is_set():
            sync_requested.clear()
            close_old_connections()
            try:
                if monotonic() >= retry_at:
                    synchronize()
            except Exception as error:  # noqa: BLE001 — an unchanged cursor recovers on the next pass.
                delay = gmail_retry_delay(error)
                if delay is not None:
                    retry_at = monotonic() + delay
                logger.warning("Gmail sync failed (%s)", type(error).__name__)
            if not stopped.is_set():
                sync_requested.wait(settings.SYNC_INTERVAL)
    finally:
        connections.close_all()


@contextmanager
def run() -> Iterator[None]:
    """Start one worker per workflow; finish active passes before stopping."""
    usage.recover()
    connections.close_all()
    stopped = Event()
    classifier = Thread(
        target=_classify, args=(stopped,), name="ai-classification", daemon=True
    )
    syncer = Thread(target=_sync, args=(stopped,), name="gmail-sync", daemon=True)
    classifier.start()
    syncer.start()
    try:
        yield
    finally:
        stopped.set()
        sync_requested.set()
        syncer.join()
        classifier.join()
