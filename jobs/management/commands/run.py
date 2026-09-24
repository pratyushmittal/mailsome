"""Migrate, then run the local web server and its periodic workers."""

import socket
from pathlib import Path

import uvicorn
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Migrate, then run the local web server and mailbox pipeline."

    def handle(self, *args, **options):
        with socket.socket() as port:
            # Another server may already be using the app port.
            if port.connect_ex(("127.0.0.1", 8002)) == 0:
                raise CommandError(
                    "Port 8002 is in use. Stop the existing server before starting Mailsome."
                )
        call_command("migrate", interactive=False)
        Path(settings.DATABASES["default"]["NAME"]).chmod(0o600)
        uvicorn.run(
            "mailsome.asgi:application",
            host="127.0.0.1",
            port=8002,
            access_log=True,
            lifespan="on",
        )
