"""One local Gmail account. No public hosting or separate application login."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("MAILSOME_DATA_DIR", BASE_DIR / "data")).resolve()
ORIGIN = "http://localhost:8002"
DEBUG = False
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
ROOT_URLCONF = "mailsome.urls"
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "accounts",
    "inbox",
    "classifications",
    "jobs",
]
MIDDLEWARE = [
    "mailsome.middleware.LocalSecurity",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_AGE = 600
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
# The private key is loaded by manage.py/asgi.py, not generated on settings import.
SECRET_KEY = os.environ.get("MAILSOME_SESSION_SECRET", "")
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "mailsome.sqlite3",
        "OPTIONS": {"timeout": 30, "transaction_mode": "IMMEDIATE"},
    }
}
USE_TZ = True
# Display aware timestamps in IST; keep timezone-aware storage and calculations.
TIME_ZONE = "Asia/Kolkata"
DATA_UPLOAD_MAX_MEMORY_SIZE = 128 * 1024
# Local terminal diagnostics are separate from browser error responses.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {"format": "{levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "console",
        },
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# Templates own page/form state; JavaScript only enhances normal links and forms.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": ["django.template.context_processors.request"]
        },
    }
]
CSRF_FAILURE_VIEW = "mailsome.errors.csrf_failure"
CSRF_COOKIE_HTTPONLY = True
FILE_UPLOAD_MAX_MEMORY_SIZE = 128 * 1024

# Disposable reader data, never workflow state or an extension of the inbox/AI cache.
# At most 32 entries of 2 MiB each; every entry also expires after 60 seconds.
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "reader": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "reader",
        "OPTIONS": {"MAX_ENTRIES": 32},
    },
}

# Shared cadence for the periodic Gmail/AI workers and frontend status polling.
SYNC_INTERVAL = 60

# Optional environment default; the local AI settings form can save a separate key.
TYPESAFE_API_KEY = os.environ.get("TYPESAFE_API_KEY", "")
