"""
Django settings for music_manager.

Every value comes from the environment. Missing or malformed configuration
raises ImproperlyConfigured with a message that says what to fix, rather than
a bare KeyError traceback at import time.

Numeric knobs are clamped here as well as validated in the settings form, so a
hand-edited .env cannot brick the service (see docs/CODE-AUDIT.md A9).
"""

from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .env import (
    env_bool,
    env_float,
    env_int,
    env_list,
    env_path,
    env_str,
)

BASE_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Core Django
# --------------------------------------------------------------------------

SECRET_KEY = env_str(
    "DJANGO_SECRET_KEY",
    required=True,
    help="Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(50))\"",
)

DEBUG = env_bool("DJANGO_DEBUG", default=False)

# "*" is accepted but warned about at startup (see music.apps).
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "music",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "music_manager.urls"
WSGI_APPLICATION = "music_manager.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
            ],
        },
    },
]


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
#
# WAL lets the request threads and the worker threads share one file safely.
# busy_timeout is generous because SD/USB writes on a Pi can stall for
# seconds under load; a blocked write is far better than SQLITE_BUSY.

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": env_path("DATABASE_PATH", default=BASE_DIR / "db.sqlite3"),
        "OPTIONS": {
            "timeout": 30,
            "init_command": (
                "PRAGMA journal_mode=WAL;"
                "PRAGMA synchronous=NORMAL;"
                "PRAGMA busy_timeout=30000;"
                "PRAGMA temp_store=MEMORY;"
                "PRAGMA mmap_size=67108864;"
                "PRAGMA cache_size=-8000;"
            ),
        },
        # Test against a real file rather than Django's default in-memory
        # database. That default runs SQLite in shared-cache mode, whose
        # locking differs from a file's: concurrent threads raise "database
        # table is locked" where WAL on a file simply serializes them. Since
        # this app's correctness rests on several threads writing one SQLite
        # file — job claiming, the worker pool, SSE streams — the in-memory
        # database would be testing something the app never does, and failing
        # on it. The file is created and destroyed per run.
        "TEST": {"NAME": str(BASE_DIR / ".test.sqlite3")},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --------------------------------------------------------------------------
# Static files
# --------------------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

USE_TZ = True
TIME_ZONE = env_str("TIME_ZONE", default="UTC")
LANGUAGE_CODE = "en-us"


# --------------------------------------------------------------------------
# Music library
# --------------------------------------------------------------------------

#: Destination root. Organized files live at
#: LIBRARY_ROOT/<Album Artist>/<Album>/<NN> - <Title>.<ext>
LIBRARY_ROOT = env_path("LIBRARY_ROOT", required=True)

#: Directories scanned for existing audio files. Colon- or comma-separated.
#: These may overlap LIBRARY_ROOT; already-organized files are detected and skipped.
SCAN_ROOTS = [Path(p) for p in env_list("SCAN_ROOTS", default=[], separator=None)]

#: Where freshly downloaded audio lands before it is identified and organized.
DOWNLOAD_STAGING = env_path("DOWNLOAD_STAGING", default=BASE_DIR / "staging")

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac", ".wma"}

#: report-only  — plan duplicates, change nothing (default; safest)
#: keep-best    — keep the highest bitrate, move the rest to LIBRARY_ROOT/.duplicates
#: keep-both    — keep both, disambiguating with a " (2)" suffix
DUPLICATE_POLICY = env_str(
    "DUPLICATE_POLICY",
    default="report-only",
    choices=("report-only", "keep-best", "keep-both"),
)

#: Organizing never runs automatically unless this is on. Off means the app
#: computes a manifest and waits for an explicit apply from the dashboard.
AUTO_ORGANIZE = env_bool("AUTO_ORGANIZE", default=False)


# --------------------------------------------------------------------------
# YouTube ingestion
# --------------------------------------------------------------------------

PLAYLIST_URL = env_str("PLAYLIST_URL", default="")
YTDLP_PATH = env_str("YTDLP_PATH", default="yt-dlp")
PIP_PATH = env_str("PIP_PATH", default="pip")
FFMPEG_LOCATION = env_str("FFMPEG_LOCATION", default="")
AUDIO_QUALITY = env_str("AUDIO_QUALITY", default="192")


# --------------------------------------------------------------------------
# Identification providers
# --------------------------------------------------------------------------

#: Order matters — cheapest first. Unknown names are rejected at startup.
IDENTIFY_CHAIN = env_list(
    "IDENTIFY_CHAIN", default=["tags", "acoustid", "shazam", "gemini"]
)

ACOUSTID_API_KEY = env_str("ACOUSTID_API_KEY", default="")
FPCALC_PATH = env_str("FPCALC_PATH", default="fpcalc")
#: AcoustID asks for no more than 3 requests/second.
ACOUSTID_RATE_PER_SEC = env_float("ACOUSTID_RATE_PER_SEC", default=3.0, minimum=0.1)

GEMINI_API_KEY = env_str("GEMINI_API_KEY", default="")
GEMINI_MODEL = env_str("GEMINI_MODEL", default="gemini-flash-latest")
#: Free tier is very limited. Default is deliberately slow.
GEMINI_RATE_PER_MIN = env_float("GEMINI_RATE_PER_MIN", default=10.0, minimum=0.1)
GEMINI_DAILY_BUDGET = env_int("GEMINI_DAILY_BUDGET", default=200, minimum=0)

SHAZAM_ENABLED = env_bool("SHAZAM_ENABLED", default=True)
SHAZAM_RATE_PER_MIN = env_float("SHAZAM_RATE_PER_MIN", default=20.0, minimum=0.1)

#: A provider result below this confidence is discarded and the chain continues.
IDENTIFY_MIN_CONFIDENCE = env_float(
    "IDENTIFY_MIN_CONFIDENCE", default=0.5, minimum=0.0, maximum=1.0
)

#: Hard ceiling on any single network call made by a provider.
PROVIDER_TIMEOUT_SECONDS = env_float(
    "PROVIDER_TIMEOUT_SECONDS", default=30.0, minimum=1.0
)


# --------------------------------------------------------------------------
# Worker / job engine
# --------------------------------------------------------------------------
#
# On a Pi 2, one ffmpeg transcode already saturates a core. Two workers is the
# practical ceiling; the default of 1 keeps the dashboard responsive.

WORKER_THREADS = env_int("WORKER_THREADS", default=1, minimum=1, maximum=8)

#: Safety-net wakeup. Workers are event-driven, so this only catches work
#: enqueued outside this process (a management command, say). It is NOT a
#: poll interval in the old sense — an idle system does no queries between wakeups.
WORKER_IDLE_WAKE_SECONDS = env_float(
    "WORKER_IDLE_WAKE_SECONDS", default=300.0, minimum=5.0
)

#: How long a claimed job stays claimed before the reaper may reclaim it.
#: Handlers doing long work call job.heartbeat() to extend it.
JOB_LEASE_SECONDS = env_float("JOB_LEASE_SECONDS", default=900.0, minimum=30.0)

#: Pause after network-heavy jobs, to stay polite to YouTube.
WORKER_COOLDOWN_SECONDS = env_float("WORKER_COOLDOWN_SECONDS", default=5.0, minimum=0.0)

#: 0 disables the periodic playlist sync.
SYNC_INTERVAL_MINUTES = env_int("SYNC_INTERVAL_MINUTES", default=0, minimum=0)

#: 0 disables the periodic library rescan.
RESCAN_INTERVAL_MINUTES = env_int("RESCAN_INTERVAL_MINUTES", default=0, minimum=0)

#: Upgrade yt-dlp on a schedule; 0 (default) means only the dashboard button.
#:
#: yt-dlp alone gets this. It is the one dependency that rots on someone else's
#: timetable — YouTube changes and downloads simply stop — and the fix is always
#: the same upgrade. Everything else is pinned in requirements.txt and moves
#: when a human decides it should, because an unattended upgrade of a tagging or
#: web library on a 24/7 Pi risks breaking a service to fix nothing.
#:
#: An upgrade restarts the service, so a run in the small hours is kindest.
YTDLP_AUTO_UPDATE_HOURS = env_int("YTDLP_AUTO_UPDATE_HOURS", default=0, minimum=0)

#: Retention for terminal Job rows; the reaper prunes older ones so the table
#: cannot grow without bound on the SD card.
JOB_RETENTION_DAYS = env_int("JOB_RETENTION_DAYS", default=14, minimum=1)


# --------------------------------------------------------------------------
# Web / SSE
# --------------------------------------------------------------------------

#: How long an SSE connection blocks before emitting a keepalive. Streams wait
#: on an in-process condition variable, so this costs nothing while idle — it is
#: not a poll interval. Longer is cheaper.
SSE_KEEPALIVE_SECONDS = env_float("SSE_KEEPALIVE_SECONDS", default=25.0, minimum=1.0)

#: Streams close themselves after this long so gunicorn threads always recycle
#: (see docs/CODE-AUDIT.md A2). The browser reconnects automatically.
SSE_MAX_STREAM_SECONDS = env_float(
    "SSE_MAX_STREAM_SECONDS", default=600.0, minimum=30.0
)

PAGE_SIZE = env_int("PAGE_SIZE", default=50, minimum=5, maximum=500)

SYSTEMD_SERVICE = env_str("SYSTEMD_SERVICE", default="music_manager")


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
#
# Console only: journald already captures it, and the old dual FileHandler
# setup wrote every record twice onto the SD card with no rotation.

LOG_LEVEL = env_str(
    "LOG_LEVEL",
    default="INFO",
    choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "concise": {
            "format": "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            "datefmt": "%H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "concise",
        },
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "music": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.db.backends": {"level": "WARNING", "propagate": False},
    },
}


# --------------------------------------------------------------------------
# Startup validation
# --------------------------------------------------------------------------

_VALID_PROVIDERS = {"tags", "acoustid", "shazam", "gemini"}
_unknown = set(IDENTIFY_CHAIN) - _VALID_PROVIDERS
if _unknown:
    raise ImproperlyConfigured(
        f"IDENTIFY_CHAIN contains unknown provider(s): {', '.join(sorted(_unknown))}. "
        f"Valid providers: {', '.join(sorted(_VALID_PROVIDERS))}."
    )

if not DEBUG and SECRET_KEY.startswith("django-insecure-"):
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY is still a development key while DJANGO_DEBUG is off. "
        "Generate a real one: python -c \"import secrets; print(secrets.token_urlsafe(50))\""
    )
