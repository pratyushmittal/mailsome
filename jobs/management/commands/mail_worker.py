"""Run one fixed mailbox workflow; just run supervises both processes."""

import signal
from threading import Event
from time import monotonic

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from jobs.runtime import file_lock
from jobs.tasks import periodic_sync, recover_worker, run_work


def loop(kind: str, stopped: Event) -> None:
    next_sync = 0.0
    while not stopped.is_set():
        close_old_connections()
        # Only the sync process owns the timer; manual requests are checked every second.
        if kind == "sync" and monotonic() >= next_sync:
            periodic_sync()
            next_sync = monotonic() + 60
        run_work(kind)
        stopped.wait(1)


class Command(BaseCommand):
    help = "Run the sync or labeling loop, with durable progress and safe restart recovery."

    def add_arguments(self, parser) -> None:
        parser.add_argument("kind", choices=["sync", "labeling"])

    def handle(self, *args, **options) -> None:
        kind = options["kind"]
        stopped = Event()
        previous = {
            signum: signal.signal(signum, lambda *_: stopped.set())
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            with file_lock(settings.DATA_DIR, f"{kind}.lock", blocking=False):
                recover_worker(kind)
                loop(kind, stopped)
        except BlockingIOError:
            raise CommandError(f"A {kind} worker is already running.") from None
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
