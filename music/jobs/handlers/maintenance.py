"""Maintenance handlers."""

from __future__ import annotations

import logging
import shutil
import subprocess

from django.conf import settings
from django.utils import timezone

from music.models import Job, JobState
from music.jobs.registry import job

log = logging.getLogger("music.jobs.maintenance")


@job("maintenance.update_ytdlp", max_attempts=1,
     description="Upgrade yt-dlp, then restart the service")
def update_ytdlp(job_obj) -> str:
    from music.ingest import youtube

    before = youtube.ytdlp_version()
    version = youtube.upgrade_ytdlp()

    # pip reports success whether or not it had anything to do, so a version
    # comparison is the only way to avoid restarting on every scheduled tick.
    if before and version and before == version:
        return f"yt-dlp is already {version}; no restart needed"

    message = f"yt-dlp upgraded {before or 'unknown'} -> {version}; restarting service"

    # Persist the terminal state BEFORE the restart can kill this process,
    # otherwise the job stays RUNNING and boot recovery re-runs the upgrade.
    Job.objects.filter(pk=job_obj.pk).update(
        state=JobState.SUCCEEDED,
        message=message,
        finished_at=timezone.now(),
        lease_expires_at=None,
    )

    if not settings.SYSTEMD_SERVICE:
        return f"yt-dlp upgraded to {version}; no SYSTEMD_SERVICE set, not restarting"

    # Absolute path: sudoers matches the command line literally, so the argv
    # below must stay byte-identical to deploy/sudoers-music-manager.
    systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"

    try:
        subprocess.run(
            [
                "sudo", "-n", systemctl, "restart", "--no-block",
                settings.SYSTEMD_SERVICE,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        log.error("systemctl not found; cannot restart %s", settings.SYSTEMD_SERVICE)
        return f"yt-dlp upgraded to {version}, but systemctl is unavailable"
    except subprocess.CalledProcessError as exc:
        log.error(
            "restart of %s refused: %s. Is the sudoers rule installed? "
            "See deploy/sudoers-music-manager.",
            settings.SYSTEMD_SERVICE, (exc.stderr or "").strip(),
        )
        return (
            f"yt-dlp upgraded to {version}, but the restart was refused — "
            f"check the sudoers rule"
        )
    except subprocess.TimeoutExpired:
        log.error("restart of %s timed out", settings.SYSTEMD_SERVICE)
        return f"yt-dlp upgraded to {version}, but the restart timed out"

    return message


@job("maintenance.reap", max_attempts=1, description="Reclaim expired leases and prune old jobs")
def reap(job_obj) -> str:
    from music.jobs import engine

    stats = engine.reap()
    return f"reclaimed {stats['reclaimed']}, pruned {stats['pruned']}"
