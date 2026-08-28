"""Incremental directory scan.

Nothing is fully materialized: the walk is `os.scandir` with an explicit stack,
and the database side streams too. A rescan of an unchanged library costs one
stat and one indexed lookup per file — no tag read, no hash, no write.

No bare `save()` anywhere: one on a row another job has deleted silently
re-INSERTs it, so the one-at-a-time fallback uses `force_insert=True`.
Identification is deliberately not done here.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from music.core import events
from music.library import tagio
from music.library.organizer import DUPLICATES_DIRNAME
from music.models import ScanRoot, Source, Track, TrackState

log = logging.getLogger("music.library.scanner")

__all__ = ["ScanResult", "ensure_roots", "scan_root", "scan_all"]

#: Rows per database round trip.
BATCH_SIZE = 100

#: Files between heartbeats. A scan of a large root outlives the job lease, and
#: the reaper would otherwise reclaim a healthy scan and rerun it from the top.
HEARTBEAT_EVERY = 50

#: mtime is a float through SQLite; compare with a tolerance, not for equality.
MTIME_TOLERANCE = 0.001

#: Never descended into, on top of every dotted name. Rescanning `.duplicates`
#: would recreate the rows the duplicate policy just resolved.
SKIP_DIRS = {DUPLICATES_DIRNAME, "@eadir", "lost+found"}

#: Fields a rescan may overwrite on a file whose bytes changed on disk.
_UPDATE_FIELDS = [
    "size_bytes",
    "mtime",
    "duration",
    "bitrate",
    "title",
    "artist",
    "album",
    "album_artist",
    "track_no",
    "disc_no",
    "year",
    "genre",
    "is_compilation",
    "musicbrainz_recording_id",
    "musicbrainz_release_id",
    "state",
    "updated_at",
]


@dataclass
class ScanResult:
    seen: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    #: Rows whose file has disappeared since the last scan.
    missing: int = 0

    def __str__(self) -> str:
        return (
            f"{self.seen} seen, {self.added} added, {self.updated} updated, "
            f"{self.skipped} unchanged, {self.missing} missing, {self.errors} error(s)"
        )


def scan_root(
    root: Path,
    *,
    source: str = Source.LIBRARY,
    heartbeat: Callable[[], None] | None = None,
) -> ScanResult:
    """Walk one directory tree and reconcile it with the Track table.

    Files that have gone are marked `MISSING`, never deleted — the likely cause
    is an unplugged drive, and the row holds real identification work.
    """
    result = ScanResult()
    root = Path(root).expanduser()

    if not root.is_dir():
        log.warning("scan root %s does not exist or is not a directory", root)
        result.errors += 1
        return result

    log.info("scanning %s", root)
    new_rows: list[Track] = []
    changed_rows: list[Track] = []

    for file_path, stat in _iter_audio_files(root):
        result.seen += 1
        if heartbeat is not None and result.seen % HEARTBEAT_EVERY == 0:
            heartbeat()

        try:
            _consider(file_path, stat, source, result, new_rows, changed_rows)
        except Exception:
            # One unreadable file must not end the scan.
            log.exception("could not process %s", file_path)
            result.errors += 1

        if len(new_rows) >= BATCH_SIZE or len(changed_rows) >= BATCH_SIZE:
            _flush(new_rows, changed_rows, result)

    _flush(new_rows, changed_rows, result)
    result.missing = _mark_missing(root, heartbeat=heartbeat)

    log.info("scanned %s: %s", root, result)
    events.bump("tracks")
    return result


def ensure_roots() -> int:
    """Seed `ScanRoot` rows from `settings.SCAN_ROOTS`. Returns the number added.

    Here rather than in the job handler, so a `scan_all()` from a command or
    the shell does not silently scan nothing.
    """
    added = 0
    for configured in settings.SCAN_ROOTS:
        _, created = ScanRoot.objects.get_or_create(
            path=str(configured), defaults={"enabled": True}
        )
        added += int(created)
    if added:
        log.info("registered %s scan root(s) from SCAN_ROOTS", added)
    return added


def scan_all(heartbeat: Callable[[], None] | None = None) -> dict[str, ScanResult]:
    """Scan every enabled `ScanRoot`, keyed by path.

    A failing root is recorded on its own row and the next one still runs.
    """
    ensure_roots()

    # Loaded whole: a handful of roots, and the loop writes to this same table.
    roots = list(ScanRoot.objects.filter(enabled=True).order_by("path"))
    if not roots:
        log.warning(
            "no scan roots configured. Set SCAN_ROOTS in .env, or add one from "
            "the dashboard, then scan again."
        )
        return {}

    results: dict[str, ScanResult] = {}
    for root in roots:
        root.last_scan_started_at = timezone.now()
        root.save(update_fields=["last_scan_started_at"])

        try:
            result = scan_root(Path(root.path), heartbeat=heartbeat)
            error = ""
        except Exception as exc:
            log.exception("scan of %s failed", root.path)
            result = ScanResult(errors=1)
            error = f"{type(exc).__name__}: {exc}"[:2000]

        root.last_scan_finished_at = timezone.now()
        root.files_seen = result.seen
        root.files_added = result.added
        root.last_error = error
        root.save(
            update_fields=[
                "last_scan_finished_at",
                "files_seen",
                "files_added",
                "last_error",
            ]
        )
        results[root.path] = result

    return results


# --- The walk ----------------------------------------------------------


def _iter_audio_files(root: Path) -> Iterator[tuple[str, os.stat_result]]:
    """Yield `(path, stat)` for every audio file under `root`.

    An explicit stack, so one directory handle is open at a time. Symlinked
    *directories* are not followed: a link back up the tree never finishes.
    """
    extensions = settings.AUDIO_EXTENSIONS
    stack: list[str] = [str(root)]

    while stack:
        current = stack.pop()
        subdirectories: list[str] = []
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    name = entry.name
                    # Covers .duplicates, .Trash-1000, editor temp files, and
                    # macOS AppleDouble "._Song.mp3" stubs — which carry an
                    # audio extension but are metadata, not audio.
                    if name.startswith("."):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if name.lower() not in SKIP_DIRS:
                                subdirectories.append(entry.path)
                            continue
                        if not entry.is_file():
                            continue
                        if os.path.splitext(name)[1].lower() not in extensions:
                            continue
                        stat = entry.stat()
                    except OSError as exc:
                        log.warning("skipping %s: %s", entry.path, exc)
                        continue
                    yield entry.path, stat
        except OSError as exc:
            # A permissions problem, or a drive that went away mid-scan.
            log.warning("cannot read %s: %s", current, exc)
            continue

        stack.extend(subdirectories)


def _consider(
    file_path: str,
    stat: os.stat_result,
    source: str,
    result: ScanResult,
    new_rows: list[Track],
    changed_rows: list[Track],
) -> None:
    """Decide what one file needs, doing the least IO that can decide it."""
    existing = (
        Track.objects.filter(path=file_path)
        .values("id", "size_bytes", "mtime", "state")
        .first()
    )

    if existing is not None and _unchanged(existing, stat):
        result.skipped += 1
        return

    # Only now, for a genuinely new or changed file, is it worth opening it.
    metadata, duration, bitrate = tagio.read_metadata(Path(file_path))

    if existing is None:
        new_rows.append(
            Track(
                path=file_path,
                source=source,
                state=TrackState.DISCOVERED,
                size_bytes=stat.st_size,
                mtime=stat.st_mtime,
                duration=duration,
                bitrate=bitrate,
                title=metadata.title,
                artist=metadata.artist,
                album=metadata.album,
                album_artist=metadata.album_artist,
                track_no=metadata.track_no,
                disc_no=metadata.disc_no,
                year=metadata.year,
                genre=metadata.genre,
                is_compilation=metadata.is_compilation,
                musicbrainz_recording_id=metadata.musicbrainz_recording_id,
                musicbrainz_release_id=metadata.musicbrainz_release_id,
            )
        )
        result.added += 1
        return

    # Changed on disk, or back after being marked MISSING. Either way the file
    # is the authority on itself again, so re-read it and hand it back to the
    # pipeline.
    row = Track(pk=existing["id"], path=file_path)
    row.size_bytes = stat.st_size
    row.mtime = stat.st_mtime
    row.duration = duration
    row.bitrate = bitrate
    row.title = metadata.title
    row.artist = metadata.artist
    row.album = metadata.album
    row.album_artist = metadata.album_artist
    row.track_no = metadata.track_no
    row.disc_no = metadata.disc_no
    row.year = metadata.year
    row.genre = metadata.genre
    row.is_compilation = metadata.is_compilation
    row.musicbrainz_recording_id = metadata.musicbrainz_recording_id
    row.musicbrainz_release_id = metadata.musicbrainz_release_id
    row.state = TrackState.DISCOVERED
    # bulk_update does not run auto_now, so updated_at is set by hand or the
    # dashboard's "recently changed" ordering quietly stops being true.
    row.updated_at = timezone.now()
    changed_rows.append(row)
    result.updated += 1


def _unchanged(existing: dict, stat: os.stat_result) -> bool:
    if existing["state"] == TrackState.MISSING:
        return False  # the file is back; re-read it
    return existing["size_bytes"] == stat.st_size and (
        abs((existing["mtime"] or 0.0) - stat.st_mtime) <= MTIME_TOLERANCE
    )


# --- Persistence -------------------------------------------------------


def _flush(new_rows: list[Track], changed_rows: list[Track], result: ScanResult) -> None:
    """Commit one batch. Both lists are emptied whatever happens."""
    if new_rows:
        _insert(new_rows, result)
        new_rows.clear()
    if changed_rows:
        try:
            with transaction.atomic():
                Track.objects.bulk_update(
                    changed_rows, _UPDATE_FIELDS, batch_size=BATCH_SIZE
                )
        except Exception:
            log.exception("could not update %s changed track(s)", len(changed_rows))
            result.errors += len(changed_rows)
            result.updated -= len(changed_rows)
        changed_rows.clear()


def _insert(rows: list[Track], result: ScanResult) -> None:
    """Insert a batch, falling back to one-at-a-time on a path collision.

    Not `ignore_conflicts`, which would leave the counts lying about how many
    rows were really added.
    """
    try:
        with transaction.atomic():
            Track.objects.bulk_create(rows, batch_size=BATCH_SIZE)
        return
    except IntegrityError:
        log.info("batch insert collided; falling back to individual inserts")

    inserted = 0
    for row in rows:
        try:
            with transaction.atomic():
                row.save(force_insert=True)
            inserted += 1
        except IntegrityError:
            # Someone else got there first; their row wins.
            log.debug("track already exists for %s", row.path)
    result.added -= len(rows) - inserted


def _mark_missing(root: Path, *, heartbeat: Callable[[], None] | None = None) -> int:
    """Flag rows under `root` whose file is gone. Never deletes one.

    A stat per known track is the price of not holding the set of seen paths in
    memory; it is paid right after the walk while the dentry cache is warm.
    """
    prefix = os.path.join(str(root), "")
    candidates = (
        Track.objects.filter(path__startswith=prefix)
        .exclude(state=TrackState.MISSING)
        .values_list("id", "path")
        .order_by("id")
    )

    gone: list[int] = []
    total = 0
    for index, (track_id, path) in enumerate(candidates.iterator(chunk_size=500), 1):
        if not os.path.exists(path):
            gone.append(track_id)
        if len(gone) >= BATCH_SIZE:
            total += _flag_missing(gone)
            gone.clear()
        if heartbeat is not None and index % (HEARTBEAT_EVERY * 10) == 0:
            heartbeat()

    total += _flag_missing(gone)
    if total:
        log.warning("%s track(s) under %s no longer exist on disk", total, root)
    return total


def _flag_missing(track_ids: list[int]) -> int:
    if not track_ids:
        return 0
    return Track.objects.filter(id__in=track_ids).update(
        state=TrackState.MISSING, updated_at=timezone.now()
    )
