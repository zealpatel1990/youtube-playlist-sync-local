"""
Periodic tasks, run by the single scheduler thread in `worker.py`.

Everything here must be cheap and idempotent: the scheduler wakes once a minute
and these run on a box where an unnecessary query is a real cost. Tasks that do
actual work enqueue a Job rather than doing it inline, so the scheduler thread
never blocks on IO.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from django.conf import settings

log = logging.getLogger("music.scheduler")


@dataclass(frozen=True)
class ScheduledTask:
    name: str
    interval_seconds: float
    run: Callable[[], None]


def _reap() -> None:
    from . import engine

    engine.reap()


def _sync_playlist() -> None:
    from . import engine

    if not settings.PLAYLIST_URL:
        return
    engine.enqueue(
        "youtube.sync",
        {"url": settings.PLAYLIST_URL},
        dedup_key="youtube.sync",
        priority=5,
    )


def _rescan_library() -> None:
    from . import engine

    engine.enqueue("library.scan_all", dedup_key="library.scan_all", priority=1)


def _update_ytdlp() -> None:
    """Queue a yt-dlp upgrade, but only when nothing else is in flight.

    The upgrade restarts the service, which would kill an in-progress download
    mid-write. Deferring costs nothing — the next tick is an hour away at most,
    and yt-dlp being a few hours stale never matters. A restart landing on top
    of a half-written MP3 does.
    """
    from music.models import Job, JobState

    from . import engine

    busy = Job.objects.filter(
        state__in=JobState.active(),
        kind__in=("youtube.download", "youtube.sync", "organize.apply_all"),
    ).exists()
    if busy:
        log.info("deferring the yt-dlp upgrade: downloads or moves are running")
        return

    engine.enqueue(
        "maintenance.update_ytdlp",
        dedup_key="maintenance.update_ytdlp",
        priority=-5,
    )


def _resume_pipeline() -> None:
    """Re-enqueue work for tracks whose retry window has come round.

    This is the one periodic query against the Track table. It is indexed on
    (state, retry_at) and returns nothing in the steady state.
    """
    from django.utils import timezone

    from music.models import Track, TrackState

    from . import engine

    due = Track.objects.filter(
        state=TrackState.FAILED, retry_at__lte=timezone.now()
    ).values_list("id", flat=True)[:50]

    for track_id in list(due):
        engine.enqueue(
            "identify.track",
            {"track_id": track_id},
            dedup_key=f"identify.track:{track_id}",
        )


def tasks() -> list[ScheduledTask]:
    return [
        ScheduledTask("reap", 60.0, _reap),
        ScheduledTask("resume", 300.0, _resume_pipeline),
        ScheduledTask(
            "sync_playlist", settings.SYNC_INTERVAL_MINUTES * 60.0, _sync_playlist
        ),
        ScheduledTask(
            "rescan_library", settings.RESCAN_INTERVAL_MINUTES * 60.0, _rescan_library
        ),
        ScheduledTask(
            "update_ytdlp", settings.YTDLP_AUTO_UPDATE_HOURS * 3600.0, _update_ytdlp
        ),
    ]
