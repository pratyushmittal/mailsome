"""Private local configuration, shared by the web server and management commands."""

import os
import secrets
from pathlib import Path


def configure() -> None:
    os.umask(0o077)  # SQLite, WAL files, and credentials are owner-only from creation.
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mailsome.settings")
    directory = Path(
        os.environ.get(
            "MAILSOME_DATA_DIR", Path(__file__).resolve().parent.parent / "data"
        )
    )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / "session-secret"
    try:
        # Exclusive creation prevents competing processes from rotating the signing key.
        with open(
            path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)
        ) as file:
            file.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    os.environ.setdefault("MAILSOME_SESSION_SECRET", path.read_text())
