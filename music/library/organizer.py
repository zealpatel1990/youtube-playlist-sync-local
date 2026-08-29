"""Planning and applying the Plex layout.

The only module that moves the user's files. Nothing is ever deleted — a losing
duplicate is parked in `LIBRARY_ROOT/.duplicates/` and every move records
`previous_path`. Nothing is written outside `LIBRARY_ROOT`: every destination is
checked with `is_within` first, because destinations are computed from tag data
that came off the internet and an artist name with `../..` only has to work once.

Every write uses `save(update_fields=[...])` or a queryset `update()`, because a
bare `save()` on a row a concurrent job deleted silently re-INSERTs it; every
file operation runs under that track's key lock. Path computation is in
`music/plex.py`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Sequence

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from music import plex
from music.core import artwork, events
from music.core.fileio import hash_file, is_within, move_file, prune_empty_dirs, unique_path
from music.core.locks import track_locks
from music.identify.base import TrackMetadata
from music.library import tagio
from music.models import Track, TrackState

log = logging.getLogger("music.library.organizer")

__all__ = [
    "OrganizeError",
    "DUPLICATES_DIRNAME",
    "plan_track",
    "plan_all",
    "apply_track",
    "apply_all",
    "revert_track",
    "find_duplicates",
    "ensure_content_hash",
]

#: Where a losing duplicate is parked. Dotted so the scanner walks past it and
#: the files do not reappear as new tracks on the next pass.
DUPLICATES_DIRNAME = ".duplicates"

#: Rows per page when sweeping the whole table — see `_paged`.
PAGE_SIZE = 200

#: Ceiling on the members reported for one duplicate group, so the same file
#: copied a thousand times cannot turn a report into an out-of-memory kill.
MAX_GROUP_MEMBERS = 50


class OrganizeError(RuntimeError):
    """A move was refused or failed. The file is left exactly where it was."""


# --- Planning ----------------------------------------------------------


def plan_track(track: Track) -> str:
    """Compute where this track belongs, store it, and return a human note.

    The note is what the dashboard shows in the manifest before applying.
    """
    library_root = Path(settings.LIBRARY_ROOT)
    naming = plex.naming_from_track(track)

    if not track.has_core_metadata:
        # Refusing to plan is the point: a file with no title would be filed
        # under "Unknown Artist/Unknown Album".
        return _save_plan(track, "", "not enough metadata yet; waiting for identification")

    destination = plex.build_path(library_root, naming)

    if not is_within(destination, library_root):
        log.error(
            "refusing to plan %s: computed destination %s is outside LIBRARY_ROOT",
            track.path, destination,
        )
        return _save_plan(track, "", "refused: destination is outside the library root")

    if plex.is_already_organized(track.path, library_root, naming):
        return _save_plan(track, track.path, "already in place")

    note = f"move to {destination}"
    if destination.exists():
        note = f"{note} — {_conflict_note()}"
    return _save_plan(track, str(destination), note)


def plan_all(
    states: Sequence[str] = (TrackState.IDENTIFIED,),
    heartbeat: Callable[[], None] | None = None,
) -> dict[str, int]:
    """Plan every track in `states`. Counts only; the manifest lives in the
    `planned_path`/`plan_note` columns."""
    stats = {"planned": 0, "in_place": 0, "skipped": 0, "errors": 0}
    processed = 0

    for track in _paged(Track.objects.filter(state__in=list(states))):
        processed += 1
        if heartbeat is not None and processed % 50 == 0:
            heartbeat()
        try:
            plan_track(track)
        except Exception:
            log.exception("could not plan %s", track.path)
            stats["errors"] += 1
            continue

        if not track.planned_path:
            stats["skipped"] += 1
        elif track.planned_path == track.path:
            stats["in_place"] += 1
        else:
            stats["planned"] += 1

    log.info("plan: %s", stats)
    events.bump("tracks")
    return stats


def _save_plan(track: Track, planned_path: str, note: str) -> str:
    track.planned_path = planned_path
    track.plan_note = note[:255]
    track.save(update_fields=["planned_path", "plan_note", "updated_at"])
    return note


def _conflict_note() -> str:
    policy = settings.DUPLICATE_POLICY
    if policy == "keep-best":
        return "a file is already there; the higher bitrate will win"
    if policy == "keep-both":
        return "a file is already there; this one will be given a suffix"
    return "a file is already there; report-only, so nothing will move"


# --- Applying ----------------------------------------------------------


def apply_track(track: Track, *, write_tags: bool = True) -> Path:
    """Move one track into place. Returns where the file actually ended up.

    Held under the track's key lock for the whole operation, so another job
    cannot delete or re-download this track between our read and our write.
    Tags are written *before* the move: within one filesystem the move is a
    rename, so writing first is one rewrite rather than two, and a failed tag
    write fails while the file is still at its recorded location.
    """
    library_root = Path(settings.LIBRARY_ROOT)

    with track_locks.acquire(f"track:{track.pk}"):
        try:
            track.refresh_from_db()
        except Track.DoesNotExist as exc:
            # Deleted underneath us; re-creating it here would resurrect it.
            raise OrganizeError(f"track {track.pk} was deleted while it waited") from exc

        source_path = Path(track.path)
        if not source_path.is_file():
            _flag_missing(track)
            raise OrganizeError(f"file has gone missing: {track.path}")

        if not track.planned_path and not track.has_core_metadata:
            # The computed destination would be "Unknown Artist/Unknown Album/…".
            raise OrganizeError(
                f"refusing to organize {track.path}: no plan, and not enough "
                f"metadata to compute one"
            )

        naming = plex.naming_from_track(track)
        if plex.is_already_organized(source_path, library_root, naming):
            _finish_in_place(track)
            return source_path

        destination = (
            Path(track.planned_path)
            if track.planned_path
            else plex.build_path(library_root, naming)
        )

        if not is_within(destination, library_root):
            raise OrganizeError(
                f"refusing to move {track.path}: {destination} is outside "
                f"LIBRARY_ROOT ({library_root})"
            )

        if destination == source_path:
            _finish_in_place(track)
            return source_path

        resolved = _resolve_conflict(track, source_path, destination)
        if resolved is None:
            return Path(track.path)  # policy said do not move; row already noted
        destination = resolved

        if write_tags:
            _write_tags(track, source_path)

        moved = move_file(source_path, destination)
        _record_move(
            track,
            moved,
            state=TrackState.ORGANIZED,
            note="organized",
            previous=str(source_path),
            planned="",
        )
        _prune_from(source_path.parent)

        log.info("organized %s -> %s", source_path, moved)
        events.bump("tracks")
        return moved


def apply_all(heartbeat: Callable[[], None] | None = None) -> dict[str, int]:
    """Apply every planned move. The destructive step, run only on request."""
    stats = {"moved": 0, "skipped": 0, "errors": 0}
    processed = 0

    queryset = Track.objects.exclude(planned_path="").exclude(
        state__in=[TrackState.MISSING, TrackState.SKIPPED]
    )

    for track in _paged(queryset):
        processed += 1
        if heartbeat is not None and processed % 10 == 0:
            heartbeat()

        if not track.needs_move:
            _finish_in_place(track)
            stats["skipped"] += 1
            continue

        before = track.path
        try:
            destination = apply_track(track)
        except OrganizeError as exc:
            # An expected refusal: a missing file, or a destination outside the
            # root. Counted, and the sweep continues.
            log.warning("skipped %s: %s", track.path, exc)
            stats["errors"] += 1
            continue
        except Exception:
            log.exception("could not organize %s", track.path)
            stats["errors"] += 1
            continue

        # A parked duplicate or a refused move counts as skipped: the file may
        # have moved, but the track was not organized.
        if track.state == TrackState.ORGANIZED and str(destination) != before:
            stats["moved"] += 1
        else:
            stats["skipped"] += 1

    log.info("apply: %s", stats)
    events.bump("tracks")
    return stats


def revert_track(track: Track) -> Path:
    """Move a track back to where it came from. The undo for `apply_track`.

    `previous_path` is cleared, or a second revert would move the file
    somewhere it has never been.
    """
    with track_locks.acquire(f"track:{track.pk}"):
        try:
            track.refresh_from_db()
        except Track.DoesNotExist as exc:
            raise OrganizeError(f"track {track.pk} no longer exists") from exc

        if not track.previous_path:
            raise OrganizeError(f"no previous path recorded for {track.path}")

        current = Path(track.path)
        if not current.is_file():
            _flag_missing(track)
            raise OrganizeError(f"file has gone missing: {track.path}")

        restored = move_file(current, Path(track.previous_path))
        _record_move(
            track,
            restored,
            state=(
                TrackState.IDENTIFIED
                if track.has_core_metadata
                else TrackState.DISCOVERED
            ),
            note="reverted; re-apply to organize",
            previous="",
            planned=str(current),
        )
        _prune_from(current.parent)

        log.info("reverted %s -> %s", current, restored)
        events.bump("tracks")
        return restored


# --- Duplicates --------------------------------------------------------


def find_duplicates() -> list[list[Track]]:
    """Groups of tracks that are probably the same recording.

    Two groupings: identical bytes (`content_hash`, populated lazily by the
    `library.rehash` job) finds the same file copied twice, while `(album
    artist, album, track number, title)` finds the same recording ripped at two
    bitrates, which no hash will ever match.
    """
    groups: list[list[Track]] = []
    seen: set[frozenset[int]] = set()

    # .order_by() is not decoration: Track.Meta sets a default ordering, and
    # Django adds ordering columns to the GROUP BY, which would make every row
    # its own group and this function silently find nothing.
    hashes = (
        Track.objects.exclude(content_hash="")
        .exclude(state=TrackState.MISSING)
        .values("content_hash")
        .annotate(members=Count("id"))
        .filter(members__gt=1)
        .order_by()
    )
    for row in hashes.iterator(chunk_size=100):
        _add_group(
            groups, seen, Track.objects.filter(content_hash=row["content_hash"])
        )

    metadata_keys = (
        Track.objects.exclude(state=TrackState.MISSING)
        .exclude(title="")
        .exclude(album="")
        .filter(track_no__gt=0)
        .values("album_artist", "album", "track_no", "title")
        .annotate(members=Count("id"))
        .filter(members__gt=1)
        .order_by()
    )
    for key in metadata_keys.iterator(chunk_size=100):
        _add_group(
            groups,
            seen,
            Track.objects.filter(
                album_artist=key["album_artist"],
                album=key["album"],
                track_no=key["track_no"],
                title=key["title"],
            ),
            # Tags are user data, and the metadata pass trusts them entirely.
            # Bytes need no corroboration, so only this pass is length-checked.
            check_duration=True,
        )

    if groups:
        log.info("found %s duplicate group(s)", len(groups))
    return groups


#: How far two lengths may differ and still be the same recording. Generous:
#: one rip may carry a second of padding or a fade the other does not.
DURATION_TOLERANCE_SECONDS = 20


def _same_length(members: list[Track]) -> list[Track]:
    """Drop members whose length rules them out as the same recording.

    The metadata grouping believes whatever the files claim, so three tracks
    mis-tagged with one song's title, album and track number are reported as
    three copies of it. Observed on a real library: "Senraan Ra Baairya"
    (6:26), "Laadki" (9:48) and "Rangabati" (6:57) — three different songs,
    grouped because a previous tool had written the same tags into all three.

    That is worse than a wrong page: `plan_track` builds the destination from
    those same four fields, so all three computed one path, and under
    `keep-best` two real songs would have been parked in `.duplicates/`.

    A length is the one claim a mis-tagger cannot fake — it comes from the
    audio. 0 means unknown and never counts as a mismatch, so nothing that
    grouped before duration was recorded starts being dropped now.
    """
    anchor = next((track.duration for track in members if track.duration > 0), 0)
    if not anchor:
        return members
    return [
        track
        for track in members
        if track.duration == 0
        or abs(track.duration - anchor) <= DURATION_TOLERANCE_SECONDS
    ]


def _add_group(
    groups: list[list[Track]],
    seen: set[frozenset[int]],
    queryset,
    *,
    check_duration: bool = False,
) -> None:
    members = list(
        queryset.exclude(state=TrackState.MISSING).order_by("-bitrate", "id")[
            :MAX_GROUP_MEMBERS
        ]
    )
    if check_duration:
        members = _same_length(members)
    if len(members) < 2:
        return
    key = frozenset(track.pk for track in members)
    if key in seen:
        return  # the same set already found by the other grouping
    seen.add(key)
    groups.append(members)


def ensure_content_hash(track: Track) -> str:
    """Hash the file if it has not been hashed. Returns the digest, or ""."""
    if track.content_hash:
        return track.content_hash
    digest = hash_file(track.path)
    if digest:
        track.content_hash = digest
        track.save(update_fields=["content_hash", "updated_at"])
    return digest


# --- Internals ---------------------------------------------------------


def _paged(queryset):
    """Stream a queryset in keyset-paginated pages.

    Not `.iterator()`: the callers write to the rows they are reading, and
    SQLite leaves it undefined whether a row modified during an open SELECT is
    revisited or skipped. Paging by `id > last` is immune to that.
    """
    last_id = 0
    while True:
        page = list(queryset.filter(id__gt=last_id).order_by("id")[:PAGE_SIZE])
        if not page:
            return
        for row in page:
            yield row
        last_id = page[-1].pk


def _resolve_conflict(track: Track, source_path: Path, destination: Path) -> Path | None:
    """Apply `DUPLICATE_POLICY` to an occupied destination. Returns the path to
    move to, or None when the policy says leave this file alone."""
    if not destination.exists():
        return destination

    policy = settings.DUPLICATE_POLICY

    if policy == "keep-both":
        # unique_path only ever appends " (2)"; it never overwrites.
        return unique_path(destination)

    if policy == "keep-best":
        incumbent_bitrate = _bitrate_at(destination)
        if track.bitrate > incumbent_bitrate:
            if _demote(destination) is None:
                return _refuse(track, "could not move the existing file aside")
            return destination
        # Ours is the lesser copy: park it, never delete it.
        parked = _demote(source_path, track=track)
        if parked is None:
            return _refuse(track, "could not park this duplicate")
        log.info("kept the existing %s; parked %s", destination, parked)
        return None

    return _refuse(
        track,
        f"duplicate of {destination.name}; DUPLICATE_POLICY is report-only",
    )


def _refuse(track: Track, note: str) -> None:
    """Record why nothing moved and leave the file untouched."""
    track.plan_note = note[:255]
    track.save(update_fields=["plan_note", "updated_at"])
    log.info("%s: %s", track.path, note)
    return None


def _demote(path: Path, *, track: Track | None = None) -> Path | None:
    """Park a losing duplicate under `LIBRARY_ROOT/.duplicates/`. A Track row
    that owns the file follows it; if that row is busy, nothing moves."""
    library_root = Path(settings.LIBRARY_ROOT)
    try:
        relative = path.resolve().relative_to(library_root.resolve())
    except (ValueError, OSError):
        relative = Path(path.name)

    target = library_root / DUPLICATES_DIRNAME / relative
    if not is_within(target, library_root):
        log.error("refusing to park %s: %s escapes LIBRARY_ROOT", path, target)
        return None

    owner = track
    if owner is None:
        owner = Track.objects.filter(path=str(path)).first()

    if owner is not None and track is None:
        # A different track's file: take its lock without blocking. Failing
        # means another job is working on that track right now.
        with track_locks.acquire(f"track:{owner.pk}", timeout=0) as acquired:
            if not acquired:
                log.info("%s is busy; not parking its file", owner.path)
                return None
            return _park(owner, path, target)

    return _park(owner, path, target)


def _park(owner: Track | None, path: Path, target: Path) -> Path | None:
    try:
        parked = move_file(path, unique_path(target))
    except OSError as exc:
        log.warning("could not park %s: %s", path, exc)
        return None

    if owner is not None:
        _record_move(
            owner,
            parked,
            state=TrackState.SKIPPED,
            note="parked as a duplicate",
            previous=str(path),  # so reverting a duplicate decision is possible
            planned="",
        )
    _prune_from(path.parent)
    return parked


def _bitrate_at(path: Path) -> int:
    """Bitrate of the file at a destination, in kbps. Prefers the Track row."""
    row = Track.objects.filter(path=str(path)).values("bitrate").first()
    if row is not None and row["bitrate"]:
        return row["bitrate"]
    return tagio.read_audio_properties(path)[1]


def _write_tags(track: Track, path: Path) -> None:
    """Write the track's metadata and cover art into the file.

    A failed tag write must not abort the move; it is recorded on the row.
    """
    cover = artwork.fetch(track.cover_url)
    try:
        tagio.write_tags(path, _metadata_from(track), cover=cover)
    except tagio.TagWriteError as exc:
        log.warning("could not tag %s: %s", path, exc)
        track.plan_note = f"moved, but tagging failed: {exc}"[:255]
        track.save(update_fields=["plan_note", "updated_at"])
        return

    if cover and not track.cover_embedded:
        track.cover_embedded = True
        track.save(update_fields=["cover_embedded", "updated_at"])


def _metadata_from(track: Track) -> TrackMetadata:
    """The Track's own view of itself, as a TrackMetadata.

    `effective_album_artist()`, not the raw column: on a compilation that tag
    must read "Various Artists" while `artist` keeps the real performer.
    """
    return TrackMetadata(
        title=track.title,
        artist=track.artist,
        album=track.album,
        album_artist=track.effective_album_artist(),
        track_no=track.track_no,
        disc_no=track.disc_no,
        year=track.year,
        genre=track.genre,
        is_compilation=track.is_compilation,
        musicbrainz_recording_id=track.musicbrainz_recording_id,
        musicbrainz_release_id=track.musicbrainz_release_id,
    )


def _record_move(
    track: Track,
    destination: Path,
    *,
    state: str,
    note: str,
    previous: str,
    planned: str,
) -> None:
    """Persist the outcome of a move in one write.

    Size and mtime are re-read from the file that now exists. Skipping that
    makes every organized track look "changed" to the next scan, which re-reads
    its tags, resets it to DISCOVERED and sends it round the identification
    chain again — on every scan, forever.
    """
    track.path = str(destination)
    track.previous_path = previous
    track.planned_path = planned
    track.plan_note = note[:255]
    track.state = state
    track.organized_at = timezone.now() if state == TrackState.ORGANIZED else None

    try:
        stat = destination.stat()
        track.size_bytes = stat.st_size
        track.mtime = stat.st_mtime
    except OSError as exc:
        log.warning("could not stat %s after moving it: %s", destination, exc)

    track.save(
        update_fields=[
            "path",
            "previous_path",
            "planned_path",
            "plan_note",
            "state",
            "organized_at",
            "size_bytes",
            "mtime",
            "updated_at",
        ]
    )


def _finish_in_place(track: Track) -> None:
    """The file is already where it belongs; record that and move nothing."""
    track.state = TrackState.ORGANIZED
    track.planned_path = ""
    track.plan_note = "already in place"
    track.organized_at = track.organized_at or timezone.now()
    track.save(
        update_fields=[
            "state",
            "planned_path",
            "plan_note",
            "organized_at",
            "updated_at",
        ]
    )


def _flag_missing(track: Track) -> None:
    track.state = TrackState.MISSING
    track.save(update_fields=["state", "updated_at"])


def _prune_from(directory: Path) -> None:
    """Remove directories a move emptied, never climbing past a known root.

    Roots come from settings, not the ScanRoot table: no query per move.
    """
    roots = [
        Path(settings.LIBRARY_ROOT),
        Path(settings.DOWNLOAD_STAGING),
        *[Path(root) for root in settings.SCAN_ROOTS],
    ]
    for root in roots:
        if is_within(directory, root):
            removed = prune_empty_dirs(directory, root)
            if removed:
                log.debug("pruned %s empty director(ies) under %s", removed, root)
            return
