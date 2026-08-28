"""Library scanning job handlers."""

from __future__ import annotations

import logging

from music.models import Track, TrackState
from music.jobs import engine
from music.jobs.registry import job

log = logging.getLogger("music.jobs.library")


@job("library.scan_all", max_attempts=2,
     description="Scan every enabled scan root for audio files")
def scan_all(job_obj) -> str:
    from music.library import scanner

    def heartbeat() -> None:
        engine.heartbeat(job_obj)

    results = scanner.scan_all(heartbeat=heartbeat)
    total_added = sum(result.added for result in results.values())
    total_seen = sum(result.seen for result in results.values())

    if total_added:
        engine.enqueue(
            "identify.pending", {"limit": 1000}, dedup_key="identify.pending"
        )
    return f"scanned {total_seen} file(s) across {len(results)} root(s); {total_added} new"


@job("library.scan_root", max_attempts=2, description="Scan one directory")
def scan_root(job_obj) -> str:
    from pathlib import Path

    from music.library import scanner

    raw_path = job_obj.payload.get("path")
    if not raw_path:
        return "no path in payload"

    def heartbeat() -> None:
        engine.heartbeat(job_obj)

    result = scanner.scan_root(Path(raw_path), heartbeat=heartbeat)
    if result.added:
        engine.enqueue(
            "identify.pending", {"limit": 1000}, dedup_key="identify.pending"
        )
    return (
        f"{raw_path}: {result.seen} seen, {result.added} added, "
        f"{result.updated} updated, {result.errors} error(s)"
    )


@job("library.rehash", max_attempts=1,
     description="Fill in missing content hashes for duplicate detection")
def rehash(job_obj) -> str:
    """Hash files that have none yet, a batch at a time.

    Hashing reads every byte of every file, so this is never folded into a scan
    — a scheduled rescan has to stay cheap enough to run on a Pi, which it
    cannot be if it re-reads tens of gigabytes off a USB disk each time. It is
    triggered only when something actually needs hashes (the duplicates page).

    The job re-enqueues itself while work remains rather than looping, so each
    batch gets its own lease and its own place in the queue: a large library is
    hashed without one job holding a worker for an hour, and a restart resumes
    from where it stopped instead of starting over.
    """
    from music.core.fileio import hash_file

    limit = max(1, int(job_obj.payload.get("limit") or 200))
    pending = list(
        Track.objects.filter(content_hash="")
        .exclude(state=TrackState.MISSING)
        .order_by("id")
        .values_list("id", "path")[:limit]
    )

    hashed = 0
    for index, (track_id, path) in enumerate(pending):
        digest = hash_file(path)
        if digest:
            Track.objects.filter(pk=track_id).update(content_hash=digest)
            hashed += 1
        if index % 25 == 0:
            engine.heartbeat(job_obj)

    remaining = (
        Track.objects.filter(content_hash="")
        .exclude(state=TrackState.MISSING)
        .count()
    )
    if remaining:
        engine.enqueue("library.rehash", {"limit": limit}, dedup_key="library.rehash")
    return f"hashed {hashed} file(s); {remaining} still to do"
