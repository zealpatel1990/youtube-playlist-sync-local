"""Django settings for music_manager.

Every value comes from the environment. Malformed configuration raises
ImproperlyConfigured; numeric knobs are clamped rather than rejected.
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
# WAL lets request threads and worker threads share one file. busy_timeout is
# generous because SD/USB writes on a Pi can stall for seconds under load.

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
        # A real file, not Django's in-memory default: that runs SQLite in
        # shared-cache mode, where concurrent threads raise "database table is
        # locked" instead of serializing as WAL on a file does.
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

#: Directories scanned for existing audio files. Colon- or comma-separated, and
#: may overlap LIBRARY_ROOT — already-organized files are skipped.
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

#: Off: the app computes a manifest and waits for an apply from the dashboard.
AUTO_ORGANIZE = env_bool("AUTO_ORGANIZE", default=False)



# --------------------------------------------------------------------------
# YouTube ingestion
# --------------------------------------------------------------------------

PLAYLIST_URL = env_str("PLAYLIST_URL", default="")
YTDLP_PATH = env_str("YTDLP_PATH", default="yt-dlp")
PIP_PATH = env_str("PIP_PATH", default="pip")
FFMPEG_LOCATION = env_str("FFMPEG_LOCATION", default="")
AUDIO_QUALITY = env_str("AUDIO_QUALITY", default="192")

#: "mp3" (default) re-encodes at AUDIO_QUALITY. "native" remuxes YouTube's own
#: Opus stream into Ogg without re-encoding — measured on ARMv7: 8s versus 77s
#: per track, and 4MB versus 6MB.
#:
#: mp3 is still the default, because the cost that matters is not the download.
#: Plex Web transcodes Opus rather than direct-playing it (it cannot detect
#: browser support, so it converts to be safe), which puts a transcode on EVERY
#: playback — on hardware that takes 77s to encode one track. MP3 direct-plays
#: everywhere, including Plexamp, phones and car stereos, and matches the
#: existing library.
#:
#: Choose "native" if you only ever play through Plexamp or Chromecast, which
#: do direct-play Opus, and want downloads ten times faster.
AUDIO_FORMAT = env_str(
    "AUDIO_FORMAT", default="mp3", choices=("native", "mp3")
)

#: DASH audio arrives as many small fragments; a few at once fills the pipe on
#: a high-latency link. Each costs a socket and a buffer, so keep it modest.
DOWNLOAD_CONCURRENT_FRAGMENTS = env_int(
    "DOWNLOAD_CONCURRENT_FRAGMENTS", default=4, minimum=1, maximum=16
)


# --------------------------------------------------------------------------
# Identification providers
# --------------------------------------------------------------------------

#: Order matters — cheapest first. Unknown names are rejected at startup.
#: `tags` is deliberately NOT first here. It is free and would be the right
#: opener for a well-kept library, but ffmpeg copies YouTube's own metadata into
#: every download, so a fresh file already carries tags like "Full Video: Hookah
#: Bar | Khiladi 786 | Akshay Kumar" — enough for the tags tier to "identify" it
#: and short-circuit the chain before fingerprinting ever runs. Putting acoustid
#: first means real recognition decides, and tags only fill the gaps it leaves.
IDENTIFY_CHAIN = env_list(
    "IDENTIFY_CHAIN", default=["acoustid", "shazam", "gemini", "tags"]
)

ACOUSTID_API_KEY = env_str("ACOUSTID_API_KEY", default="")
FPCALC_PATH = env_str("FPCALC_PATH", default="fpcalc")
#: AcoustID asks for no more than 3 requests/second.
ACOUSTID_RATE_PER_SEC = env_float("ACOUSTID_RATE_PER_SEC", default=3.0, minimum=0.1)

GEMINI_API_KEY = env_str("GEMINI_API_KEY", default="")

#: Pinned, not the "-latest" alias: Google hot-swaps those on every release, and
#: the free-tier quota differs enormously between tiers — flash-lite allows
#: 15 RPM / 500 requests per day, flash only 5 RPM / 20 per day. A silent swap
#: onto a flash model would cut the daily budget by 25x.
#: Both uses here are text-to-text (metadata inference and romanization), which
#: is exactly what flash-lite is for.
GEMINI_MODEL = env_str("GEMINI_MODEL", default="gemini-3.5-flash-lite")
#: Kept under the flash-lite free-tier ceilings (15 RPM, 500 RPD) so the app
#: throttles itself before Google does, leaving headroom for other users of the
#: same key. Lower both if the key is shared.
GEMINI_RATE_PER_MIN = env_float("GEMINI_RATE_PER_MIN", default=12.0, minimum=0.1)
GEMINI_DAILY_BUDGET = env_int("GEMINI_DAILY_BUDGET", default=400, minimum=0)

SHAZAM_ENABLED = env_bool("SHAZAM_ENABLED", default=True)
SHAZAM_RATE_PER_MIN = env_float("SHAZAM_RATE_PER_MIN", default=20.0, minimum=0.1)

#: Apple Music and Deezer catalogue search. Neither needs a key or an account.
#: They search *text*, not audio — see `music/identify/textsearch.py` — so they
#: belong after the fingerprinting tiers and before Gemini, which is the other
#: provider that only ever reads a title and is the one with a real quota.
ITUNES_ENABLED = env_bool("ITUNES_ENABLED", default=True)
#: Apple publishes no documented ceiling and returns 403 under sustained load;
#: about 20/min is what the Store's own search box generates, and the whole
#: library is a one-off sweep rather than steady traffic.
ITUNES_RATE_PER_MIN = env_float("ITUNES_RATE_PER_MIN", default=20.0, minimum=0.1)
#: Which Apple storefront to search. "US" is the default because that is what
#: the provider was measured against, and it carries the Indian film catalogue
#: this library is mostly made of. "IN" ranks regional titles higher.
ITUNES_COUNTRY = env_str("ITUNES_COUNTRY", default="US")

#: After the chain settles on an answer, look it up in a catalogue and fill in
#: whatever it left blank — album, year, track and disc number, cover art.
#: Shazam and Gemini both return a bare title and artist, and the track number
#: is the one field the Plex filename cannot be built without.
#: Costs at most one extra keyless GET per identified track, and none at idle.
IDENTIFY_ENRICH = env_bool("IDENTIFY_ENRICH", default=True)

DEEZER_ENABLED = env_bool("DEEZER_ENABLED", default=True)
#: Deezer documents a 50-requests-per-5-seconds ceiling per IP. Nowhere near
#: it — this is set to be a polite neighbour, not to go fast.
DEEZER_RATE_PER_MIN = env_float("DEEZER_RATE_PER_MIN", default=30.0, minimum=0.1)

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
# On a Pi 2 one ffmpeg transcode saturates a core, so two workers is the
# practical ceiling.

WORKER_THREADS = env_int("WORKER_THREADS", default=1, minimum=1, maximum=8)

#: NOT a poll interval — workers are event-driven and idle between wakeups.
#: This only catches work enqueued outside this process (a management command).
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
#: An upgrade restarts the service, so a run in the small hours is kindest.
YTDLP_AUTO_UPDATE_HOURS = env_int("YTDLP_AUTO_UPDATE_HOURS", default=0, minimum=0)

#: Retention for terminal Job rows; the reaper prunes older ones.
JOB_RETENTION_DAYS = env_int("JOB_RETENTION_DAYS", default=14, minimum=1)


# --------------------------------------------------------------------------
# Web / SSE
# --------------------------------------------------------------------------

#: How long an SSE connection blocks before emitting a keepalive. NOT a poll
#: interval — streams wait on a condition variable, so idle costs nothing.
SSE_KEEPALIVE_SECONDS = env_float("SSE_KEEPALIVE_SECONDS", default=25.0, minimum=1.0)

#: Streams close themselves after this long so gunicorn threads recycle. The
#: browser reconnects automatically.
SSE_MAX_STREAM_SECONDS = env_float(
    "SSE_MAX_STREAM_SECONDS", default=600.0, minimum=30.0
)

PAGE_SIZE = env_int("PAGE_SIZE", default=50, minimum=5, maximum=500)

SYSTEMD_SERVICE = env_str("SYSTEMD_SERVICE", default="music_manager")


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
#
# Console only: journald captures it, and file handlers would write to the SD
# card with no rotation.

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

_VALID_PROVIDERS = {"tags", "acoustid", "shazam", "itunes", "deezer", "gemini"}
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
