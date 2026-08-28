"""
Maintenance handlers.

`maintenance.update_ytdlp` is the one that upgrades yt-dlp and restarts the
service. The previous version got this wrong in a way worth restating, because
the fix is the whole reason this handler looks the way it does
(docs/CODE-AUDIT.md A7):

It spawned a detached ``sh -c "sleep 3; sudo systemctl restart …"`` and then
returned, leaving the framework to persist SUCCESS afterwards. Since SQLite's
busy timeout is longer than three seconds, a contended write could still be in
flight when the process was killed — so the job stayed RUNNING, boot recovery
requeued it, and the upgrade ran again. The `Popen` result was never checked
either, so a missing sudoers rule meant the restart silently never happened
while the job cheerfully reported success.

Here the terminal state is written *before* the restart is requested, and the
restart is a synchronous ``systemctl --no-block`` that returns as soon as the
job is queued with systemd — no sleep, no race — with ``sudo -n`` and
``check=True`` so a missing sudoers rule fails loudly instead of silently.
"""

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

    # Nothing changed — do not restart. This matters most for the scheduled
    # run (YTDLP_AUTO_UPDATE_HOURS): pip reports success whether or not it had
    # anything to do, so restarting unconditionally would bounce the service on
    # every tick, killing whatever was downloading, to install nothing.
    if before and version and before == version:
        return f"yt-dlp is already {version}; no restart needed"

    message = f"yt-dlp upgraded {before or 'unknown'} -> {version}; restarting service"

    # Persist the outcome BEFORE anything can kill this process.
    Job.objects.filter(pk=job_obj.pk).update(
        state=JobState.SUCCEEDED,
        message=message,
        finished_at=timezone.now(),
        lease_expires_at=None,
    )

    if not settings.SYSTEMD_SERVICE:
        return f"yt-dlp upgraded to {version}; no SYSTEMD_SERVICE set, not restarting"

    # Resolve to an absolute path: sudoers matches the command line literally,
    # and the rule in deploy/sudoers-music-manager names full paths. The
    # argument list below must stay byte-identical to that rule.
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
