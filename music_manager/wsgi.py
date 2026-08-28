"""WSGI entry point. Also boots the in-process worker pool."""

from __future__ import annotations

import logging
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    # override=False so a real environment variable (systemd) beats the file.
    load_dotenv(BASE_DIR / ".env", override=False)
except ImportError:  # pragma: no cover - python-dotenv is a hard requirement
    pass

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "music_manager.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()

log = logging.getLogger("music")

# Never use gunicorn --preload: threads do not survive the fork, so the workers
# would be started in the master and inherited dead.
if os.environ.get("MUSIC_MANAGER_DISABLE_WORKERS", "").lower() not in ("1", "true", "yes"):
    try:
        from music.jobs.worker import start

        if not start():
            log.error(
                "worker pool did not start — another process holds the lock. "
                "Jobs will not be processed by this process."
            )
    except Exception:
        log.exception(
            "worker pool failed to start; the web UI is up but NO background work "
            "will run. Fix this before relying on the service."
        )
