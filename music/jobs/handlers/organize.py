"""
Organization job handlers — computing and applying the Plex layout.

Planning and applying are deliberately separate jobs. A plan is free and
reversible; an apply moves thousands of real files. `AUTO_ORGANIZE` defaults to
off, so the normal flow is: scan → identify → plan → *the user reviews the
manifest* → apply.
"""

from __future__ import annotations

import logging

from django.conf import settings

from music.core.locks import track_locks
from music.models import Track, TrackState
from music.jobs import engine
from music.jobs.registry import job

log = logging.getLogger("music.jobs.organize")


@job("organize.track", max_attempts=3, description="Plan (and optionally apply) one track")
def organize_track(job_obj) -> str:
    from music.library import organizer

    track_id = job_obj.payload.get("track_id")
    should_apply = bool(job_obj.payload.get("apply"))
    if not track_id:
        return "no track_id in payload"

    with track_locks.acquire(f"track:{track_id}") as acquired:
        if not acquired:  # pragma: no cover
            return "track busy"

        track = Track.objects.filter(pk=track_id).first()
        if track is None:
            return f"track {track_id} no longer exists"

        note = organizer.plan_track(track)
        if not should_apply:
            return f"planned: {note}"

        track.refresh_from_db()
        if not track.needs_move:
            return f"already in place: {track.path}"

        destination = organizer.apply_track(track)
        return f"moved to {destination}"


@job("organize.plan_all", max_attempts=1,
     description="Compute the destination for every identified track")
def plan_all(job_obj) -> str:
    from music.library import organizer

    def heartbeat() -> None:
        engine.heartbeat(job_obj)

    stats = organizer.plan_all(heartbeat=heartbeat)
    return (
        f"planned {stats.get('planned', 0)} track(s); "
        f"{stats.get('in_place', 0)} already in place; "
        f"{stats.get('skipped', 0)} skipped"
    )


@job("organize.apply_all", max_attempts=1,
     description="Apply every planned move (the destructive step)")
def apply_all(job_obj) -> str:
    from music.library import organizer

    def heartbeat() -> None:
        engine.heartbeat(job_obj)

    stats = organizer.apply_all(heartbeat=heartbeat)
    return (
        f"moved {stats.get('moved', 0)} file(s); "
        f"{stats.get('skipped', 0)} skipped; {stats.get('errors', 0)} error(s)"
    )


@job("organize.revert", max_attempts=2, description="Move a track back to where it came from")
def revert(job_obj) -> str:
    from music.library import organizer

    track_id = job_obj.payload.get("track_id")
    if not track_id:
        return "no track_id in payload"

    with track_locks.acquire(f"track:{track_id}") as acquired:
        if not acquired:  # pragma: no cover
            return "track busy"
        track = Track.objects.filter(pk=track_id).first()
        if track is None:
            return f"track {track_id} no longer exists"
        if not track.previous_path:
            return "no previous path recorded; nothing to revert"
        restored = organizer.revert_track(track)
        return f"restored to {restored}"
