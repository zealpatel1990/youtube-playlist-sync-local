"""Scan directories for existing audio files and record them as Tracks.

Thin on purpose: resolve arguments, call `music.library.scanner`, print what
came back — so the batch path and the worker path cannot drift apart.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from music.models import ScanRoot


class Command(BaseCommand):
    help = "Scan directories for audio files and record them as Tracks."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--root",
            action="append",
            dest="roots",
            metavar="PATH",
            default=None,
            help=(
                "Directory to scan. Repeatable. Defaults to every enabled "
                "ScanRoot, or SCAN_ROOTS from the environment if none exist."
            ),
        )

    def handle(self, *args, **options) -> None:
        # Imported inside handle so a missing optional dependency (mutagen)
        # disables one command rather than breaking `manage.py help`, which
        # imports every command module in the app.
        from music.library import scanner

        roots = [Path(root).expanduser() for root in (options["roots"] or [])]
        roots = roots or self._default_roots()
        if not roots:
            raise CommandError(
                "no directories to scan. Pass --root PATH, add a ScanRoot row, "
                "or set SCAN_ROOTS in .env."
            )

        totals: dict[str, int] = {}
        for root in roots:
            self.stdout.write(f"scanning {root}")
            result = scanner.scan_root(root)
            self.stdout.write(f"  {result}")
            for key, value in _counts(result).items():
                totals[key] = totals.get(key, 0) + value

        if len(roots) > 1:
            self.stdout.write(self.style.SUCCESS(f"total across {len(roots)} roots:"))
            for key in sorted(totals):
                self.stdout.write(f"  {key}: {totals[key]}")

    def _default_roots(self) -> list[Path]:
        """Enabled ScanRoot rows, falling back to the configured SCAN_ROOTS —
        on a fresh install nothing has created the rows yet."""
        rows = [
            Path(path)
            for path in ScanRoot.objects.filter(enabled=True).values_list(
                "path", flat=True
            )
        ]
        return rows or [Path(path) for path in settings.SCAN_ROOTS]


def _counts(result) -> dict[str, int]:
    """The integer fields of whatever the scanner returned. Against the shape,
    not the type, so a new `ScanResult` counter appears here for free."""
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        data = dataclasses.asdict(result)
    elif isinstance(result, dict):
        data = result
    else:
        data = getattr(result, "__dict__", {}) or {}
    return {key: value for key, value in data.items() if isinstance(value, int)}
