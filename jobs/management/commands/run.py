"""Start the local web server and its two workflow loops together."""

import signal
import socket
import subprocess
import sys
import time
from types import FrameType

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from jobs.runtime import file_lock


def stop(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


class Command(BaseCommand):
    help = "Migrate safely, then run the loopback web server and sync/classification loops."

    def handle(self, *args, **options):
        with socket.socket() as port:
            # Another server may already be using the app port.
            if port.connect_ex(("127.0.0.1", 8002)) == 0:
                raise CommandError(
                    "Port 8002 is in use. Stop the existing server before starting Mailsome."
                )
        # A web port can be free while a standalone workflow loop is still running.
        try:
            with (
                file_lock(settings.DATA_DIR, "sync.lock", blocking=False),
                file_lock(settings.DATA_DIR, "labeling.lock", blocking=False),
            ):
                call_command("migrate", interactive=False)
        except BlockingIOError:
            raise CommandError(
                "Stop mailbox workers before applying migrations."
            ) from None
        settings.DATABASES["default"]["NAME"].chmod(0o600)
        processes: list[subprocess.Popen] = []
        # One Ctrl+C/SIGTERM stops all children, including a partially started group.
        previous = signal.signal(signal.SIGTERM, stop)
        try:
            for command in (
                ["manage.py", "mail_worker", "sync"],
                ["manage.py", "mail_worker", "labeling"],
                [
                    "-m",
                    "uvicorn",
                    "mailsome.asgi:application",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8002",
                    "--no-access-log",
                    # Django uses HTTP ASGI only; startup/recovery belong to this supervisor and worker.
                    "--lifespan",
                    "off",
                ],
            ):
                processes.append(
                    subprocess.Popen([sys.executable, *command], cwd=settings.BASE_DIR)
                )
            while all(process.poll() is None for process in processes):
                time.sleep(0.25)
            raise CommandError("The web server or mailbox worker stopped unexpectedly.")
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, previous)
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
            for process in reversed(processes):
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
