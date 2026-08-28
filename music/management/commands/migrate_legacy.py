"""
Carry the old `playlist` app's rows over into `Track` + `YoutubeVideo`.

The cutover unwires `playlist` from INSTALLED_APPS but leaves its tables in the
database, so the download history is still there — this command copies it
across so nothing is lost and nothing is re-downloaded.

Read with raw SQL through `django.db.connection` rather than the old models,
because by the time this runs the old app is no longer installed and importing
`playlist.models` would fail. The tables are probed via `sqlite_master` (this
project is SQLite-only, and the schema is not managed by any app that is still
installed), so running against a fresh database is a no-op rather than an error.

Idempotent: rows that already exist are counted and skipped, so a second run
changes nothing. Dry run by default.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils.dateparse import parse_datetime

from music.models import Availability, Source, Track, TrackState, YoutubeVideo

LEGACY_VIDEOS = "playlist_video"
LEGACY_TRACKS = "playlist_localtrack"

#: The old Video.status values map one-for-one onto Availability.
_AVAILABILITY = {
    "AVAILABLE": Availability.AVAILABLE,
    "UNAVAILABLE": Availability.UNAVAILABLE,
    "PRIVATE": Availability.PRIVATE,
    "DELETED": Availability.DELETED,
}


class _Rollback(Exception):
    """Sentinel: unwinds the dry run after it has computed the whole plan."""


class Command(BaseCommand):
    help = (
        "Copy legacy playlist_video / playlist_localtrack rows into Track and "
        "YoutubeVideo. Dry run unless --apply is given."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Commit the migration. Without it, everything is rolled back.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Explicit no-op form of the default behaviour.",
        )

    def handle(self, *args, **options) -> None:
        if connection.vendor != "sqlite":
            raise CommandError(
                f"this migration reads the legacy tables with SQLite-specific SQL, "
                f"but the configured database is {connection.vendor}."
            )

        tables = self._legacy_tables()
        if LEGACY_VIDEOS not in tables:
            self.stdout.write(
                self.style.SUCCESS(
                    f"no {LEGACY_VIDEOS} table in this database: nothing to migrate."
                )
            )
            return

        apply_changes = options["apply"] and not options["dry_run"]

        try:
            with transaction.atomic():
                counts = self._migrate(has_tracks=LEGACY_TRACKS in tables)
                if not apply_changes:
                    raise _Rollback
        except _Rollback:
            pass

        for key in sorted(counts):
            self.stdout.write(f"  {key}: {counts[key]}")

        if apply_changes:
            self.stdout.write(self.style.SUCCESS("migration committed."))
        else:
            self.stdout.write(
                self.style.WARNING(
                    "dry run: nothing was written. Re-run with --apply to commit."
                )
            )

    # -- reading the old schema -------------------------------------------

    def _legacy_tables(self) -> set[str]:
        """SQLite's own catalogue is the information_schema equivalent here."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN (%s, %s)",
                [LEGACY_VIDEOS, LEGACY_TRACKS],
            )
            return {row[0] for row in cursor.fetchall()}

    @staticmethod
    def _rows(table: str) -> list[dict]:
        """`SELECT *` mapped onto dicts.

        Named columns would be tidier, but the legacy schema went through four
        migrations and a column that moved would turn this into a crash instead
        of a missing value. Every read below uses `.get()` for the same reason.
        """
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {table}")  # noqa: S608 — fixed literal
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    # -- the migration itself ----------------------------------------------

    def _migrate(self, *, has_tracks: bool) -> dict[str, int]:
        videos = self._rows(LEGACY_VIDEOS)
        legacy_tracks = (
            {row.get("video_id"): row for row in self._rows(LEGACY_TRACKS)}
            if has_tracks
            else {}
        )

        counts = {
            "legacy_videos": len(videos),
            "legacy_local_tracks": len(legacy_tracks),
            "videos_created": 0,
            "videos_already_present": 0,
            "tracks_created": 0,
            "tracks_linked_to_existing": 0,
            "files_not_on_disk": 0,
            "videos_without_a_file": 0,
        }

        existing_video_ids = set(YoutubeVideo.objects.values_list("pk", flat=True))

        for row in videos:
            video_id = str(row.get("id") or "").strip()
            if not video_id:
                continue

            legacy_track = legacy_tracks.get(row.get("id")) or {}
            track = self._track_for(legacy_track, row, counts)

            if video_id in existing_video_ids:
                counts["videos_already_present"] += 1
                self._link_existing(video_id, track, counts)
                continue

            YoutubeVideo.objects.create(
                video_id=video_id,
                title=str(row.get("title") or "")[:512],
                uploader=str(row.get("uploader") or "")[:255],
                # NULL duration is stored as 0, never None: a None reaching a
                # comparison is what raised TypeError at a distance before (A14).
                duration=_positive_int(row.get("duration")),
                url=str(row.get("url") or "")[:1024],
                availability=_AVAILABILITY.get(
                    str(row.get("status") or "").strip().upper(),
                    Availability.UNAVAILABLE,
                ),
                track=track,
                # The legacy retry state belonged to downloading, which is what
                # YoutubeVideo owns now.
                fail_count=_positive_int(legacy_track.get("fail_count")),
                retry_at=_parse_dt(legacy_track.get("retry_at")),
                last_seen_at=_parse_dt(row.get("last_check_at")),
            )
            counts["videos_created"] += 1

        return counts

    def _track_for(
        self, legacy_track: dict, video_row: dict, counts: dict[str, int]
    ) -> Track | None:
        """The Track for one legacy row, created if the old app had a file for it."""
        local_path = str(legacy_track.get("local_path") or "").strip()
        if not local_path:
            counts["videos_without_a_file"] += 1
            return None

        existing = Track.objects.filter(path=local_path).first()
        if existing is not None:
            return existing

        path = Path(local_path)
        on_disk = path.is_file()
        if not on_disk:
            counts["files_not_on_disk"] += 1

        track = Track(
            path=local_path,
            source=Source.YOUTUBE,
            # MISSING is a state the scanner knows how to resolve; claiming a
            # file exists when it does not would strand it instead.
            #
            # Everything else starts at DISCOVERED, including rows the legacy
            # pipeline marked COMPLETED. Those files carry the tags the old
            # tagger embedded, so the free, local `tags` provider re-derives
            # their metadata in one pass with no network call. Asserting
            # IDENTIFIED with empty metadata columns would instead file them
            # all under Unknown Artist / Unknown Album.
            state=TrackState.DISCOVERED if on_disk else TrackState.MISSING,
            duration=_positive_int(video_row.get("duration")),
        )
        if on_disk:
            stat = path.stat()
            track.size_bytes = stat.st_size
            track.mtime = stat.st_mtime
        # Deliberately NOT carrying md5_hash across into content_hash: the new
        # field holds a sha1 (see core.fileio.hash_file), and mixing digests in
        # one column would silently break duplicate detection. The scanner
        # computes a real one on its next pass.
        track.save()
        counts["tracks_created"] += 1
        return track

    @staticmethod
    def _link_existing(video_id: str, track: Track | None, counts: dict) -> None:
        """Attach a track to an already-migrated video, if that is still free.

        `YoutubeVideo.track` is a OneToOne, so a second video pointing at the
        same file would raise. That happens when two legacy rows shared a
        local_path — rare, but a re-run must not blow up on it.
        """
        if track is None:
            return
        video = YoutubeVideo.objects.filter(pk=video_id, track__isnull=True).first()
        if video is None:
            return
        if YoutubeVideo.objects.filter(track=track).exclude(pk=video_id).exists():
            return
        video.track = track
        video.save(update_fields=["track", "updated_at"])
        counts["tracks_linked_to_existing"] += 1


def _positive_int(value) -> int:
    """Legacy NULLs and stray floats become 0, never None."""
    if value is None:
        return 0
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _parse_dt(value):
    """Parse a datetime out of a raw SQLite read.

    Django's datetime converters only run for ORM queries, so a raw cursor
    hands back the stored text — '2024-05-01 12:00:00.123456', naive. USE_TZ is
    on and SQLite stores UTC, so the value is stamped UTC rather than localised
    into whatever TIME_ZONE happens to be.
    """
    if value in (None, ""):
        return None
    parsed = value if isinstance(value, datetime) else parse_datetime(str(value))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed
