"""
Run the identification chain against a single file and print what it found.

This replaces `test_tagger.py` at the repository root, which called
`django.setup()` and ran a live Shazam/Gemini pass **at import**, with no
`__main__` guard. Its filename matched unittest's `test*.py` discovery pattern
and discovery imports modules before the test database exists, so a bare
`python manage.py test` recognized real audio, wrote to the real db.sqlite3 and
renamed real files (docs/CODE-AUDIT.md A10).

A management command is out of discovery's reach entirely. As a second belt,
the work runs inside a transaction that is always rolled back: this is a
diagnostic, and running a diagnostic against the live library must not change
it. To actually apply identification, enqueue the `identify.track` job.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from music.models import Track


class _Rollback(Exception):
    """Sentinel: unwinds the transaction once the result has been printed."""


class Command(BaseCommand):
    help = "Run the identification chain against one track and print the result."

    def add_arguments(self, parser) -> None:
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument(
            "--track-id", type=int, default=None, metavar="N", help="Track row id."
        )
        target.add_argument(
            "--path",
            default=None,
            metavar="PATH",
            help=(
                "Audio file path. Uses the existing Track row if there is one, "
                "otherwise a temporary row that is rolled back afterwards."
            ),
        )

    def handle(self, *args, **options) -> None:
        # Late import: the providers pull in shazamio / pyacoustid, and a
        # missing one must not break `manage.py help`.
        from music.identify import identify

        try:
            with transaction.atomic():
                track = self._resolve_track(options)
                self.stdout.write(f"identifying: {track.path}")
                result = identify(self._context_for(track))
                self._write_result(result)
                raise _Rollback
        except _Rollback:
            pass

        self.stdout.write(
            self.style.WARNING(
                "nothing was saved: this command is read-only. Enqueue the "
                "identify.track job to apply a result."
            )
        )

    def _resolve_track(self, options) -> Track:
        if options["track_id"] is not None:
            try:
                return Track.objects.get(pk=options["track_id"])
            except Track.DoesNotExist:
                raise CommandError(f"no Track with id {options['track_id']}") from None

        path = Path(options["path"]).expanduser()
        if not path.is_file():
            raise CommandError(f"not a file: {path}")

        existing = Track.objects.filter(path=str(path)).first()
        if existing is not None:
            return existing

        # Created inside the transaction that this command always rolls back,
        # so the providers get a real, saveable row without leaving one behind.
        self.stdout.write("no Track row for that path; using a temporary one")
        return Track.objects.create(path=str(path))

    @staticmethod
    def _context_for(track: Track):
        """Assemble the same context the `identify.track` job builds.

        Deliberately identical rather than minimal: a diagnostic that fed the
        chain less than the pipeline does would answer a question nobody asked.
        The YouTube title in particular is the only thing Gemini has to work
        with, so omitting it would make this command disagree with production
        on exactly the tracks it is most often run against.
        """
        from music.identify import IdentifyContext
        from music.library import tagio

        video = getattr(track, "youtube_video", None)
        return IdentifyContext(
            path=Path(track.path),
            duration=track.duration,
            fingerprint=track.fingerprint,
            existing=tagio.read_tags(Path(track.path)),
            hint_title=video.title if video is not None else "",
            hint_url=video.url if video is not None else "",
        )

    def _write_result(self, result) -> None:
        """Print whatever the chain returned, without assuming its shape.

        The chain's metadata type is free to grow fields; a command that hard
        codes them would quietly stop printing the new ones.
        """
        if result is None:
            self.stdout.write(self.style.WARNING("no provider returned a match"))
            return

        fields = self._as_mapping(result)
        width = max((len(key) for key in fields), default=0)
        for key in sorted(fields):
            value = fields[key]
            if value in (None, "", 0, 0.0):
                continue
            self.stdout.write(f"  {key.ljust(width)} : {value}")

    @staticmethod
    def _as_mapping(result) -> dict:
        if dataclasses.is_dataclass(result) and not isinstance(result, type):
            return dataclasses.asdict(result)
        if isinstance(result, Mapping):
            return dict(result)
        data = getattr(result, "__dict__", None)
        if data:
            return {k: v for k, v in data.items() if not k.startswith("_")}
        return {"result": str(result)}
