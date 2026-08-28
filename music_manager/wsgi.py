"""
WSGI entry point. Also boots the in-process worker pool.

Two deliberate differences from the previous version:

**It loads .env itself.** Previously only manage.py did, and the committed
systemd unit had no `EnvironmentFile=` — so gunicorn raised KeyError during app
import and crash-looped until systemd's start limit halted the unit
(docs/CODE-AUDIT.md A1). The unit now carries `EnvironmentFile=`, and this is
the belt to that braces: `load_dotenv` does not override real environment
variables, so systemd still wins where it provides a value.

**A failed worker start is loud.** The old code swallowed the exception and
left a web app that served pages while silently processing no jobs.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env", override=False)
except ImportError:  # pragma: no cover - python-dotenv is a hard requirement
    pass

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "music_manager.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()

log = logging.getLogger("music")

# Start the workers only in the process that actually serves requests.
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
