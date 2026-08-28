"""Plan — or, with --apply, perform — the moves into the Plex layout.

A dry run is the default. The manifest lives in the `planned_path` /
`plan_note` columns rather than in memory, and this command pages through them.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db.models import F

from music.models import Track, TrackState


class Command(BaseCommand):
    help = (
        "Plan the Plex-layout moves for identified tracks (default: dry run). "
        "Pass --apply to carry them out."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually move the files. Without this the manifest is only printed.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            metavar="N",
            help="Handle at most N tracks. Useful for a first cautious pass.",
        )

    def handle(self, *args, **options) -> None:
        # Late import: see scan_library for why command modules do not import
        # their service at module level.
        from music.library import organizer

        limit = options["limit"]
        if limit is not None and limit < 1:
            raise CommandError("--limit must be 1 or more")

        if options["apply"]:
            summary = self._apply(organizer, limit)
            self.stdout.write(self.style.SUCCESS("applied:"))
            self._write_counts(summary)
            return

        summary = self._plan(organizer, limit)
        self._write_manifest(limit)
        self._write_counts(summary)
        self.stdout.write(
            self.style.WARNING(
                "dry run: nothing was moved. Re-run with --apply to carry this out."
            )
        )

    # -- the two service calls ---------------------------------------------
    #
    # `plan_all` and `apply_all` sweep everything and take no limit, because
    # the pipeline never wants one. A capped pass drives the per-track entry
    # points instead — the same two the `organize.track` job handler calls.

    def _plan(self, organizer, limit: int | None) -> dict[str, int]:
        if limit is None:
            return organizer.plan_all()
        return self._each(
            organizer.plan_track,
            Track.objects.filter(state=TrackState.IDENTIFIED).order_by("id")[:limit],
        )

    def _apply(self, organizer, limit: int | None) -> dict[str, int]:
        if limit is None:
            return organizer.apply_all()
        planned = (
            Track.objects.exclude(planned_path="")
            .exclude(planned_path=F("path"))
            .exclude(state__in=[TrackState.MISSING, TrackState.SKIPPED])
            .order_by("id")[:limit]
        )
        return self._each(organizer.apply_track, planned)

    def _each(self, action, tracks) -> dict[str, int]:
        counts = {"processed": 0, "errors": 0}
        for track in tracks:
            try:
                action(track)
            except Exception as exc:
                # The organizer logs this too; an operator running a capped
                # pass by hand wants it on the terminal.
                self.stderr.write(self.style.ERROR(f"  {track.path}: {exc}"))
                counts["errors"] += 1
            else:
                counts["processed"] += 1
        return counts

    # -- output -------------------------------------------------------------

    def _write_manifest(self, limit: int | None) -> None:
        """Print the planned moves straight from the rows the planner wrote.

        Streamed with `iterator()`, and only the three columns it prints.
        """
        planned = (
            Track.objects.exclude(planned_path="")
            .exclude(planned_path=F("path"))
            .only("path", "planned_path", "plan_note")
            .order_by("planned_path")
        )
        if limit:
            planned = planned[:limit]

        count = 0
        for track in planned.iterator(chunk_size=200):
            note = f"   [{track.plan_note}]" if track.plan_note else ""
            self.stdout.write(f"  {track.path}\n    -> {track.planned_path}{note}")
            count += 1

        if not count:
            self.stdout.write("  no moves planned: everything is already in place.")

    def _write_counts(self, counts) -> None:
        for key in sorted(counts):
            self.stdout.write(f"  {key}: {counts[key]}")
