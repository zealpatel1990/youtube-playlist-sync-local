#!/usr/bin/env python
"""Django's command-line utility."""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    # override=False so a real environment variable always beats the file.
    load_dotenv(BASE_DIR / ".env", override=False)
except ImportError:
    pass


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "music_manager.settings")

    # A management command must not start a second worker pool competing with
    # the live service. `runserver` is exempt: workers live inside the web
    # process, so disabling them there would leave a dashboard that runs
    # nothing. Only the threads are gated — handlers still register in
    # MusicConfig.ready(), since enqueuing needs the registry either way.
    if "runserver" not in sys.argv:
        os.environ.setdefault("MUSIC_MANAGER_DISABLE_WORKERS", "1")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Is it installed and is your virtual "
            "environment activated?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
