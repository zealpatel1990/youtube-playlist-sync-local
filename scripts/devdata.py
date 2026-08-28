#!/usr/bin/env python
"""
Manage the local test sandbox in _devdata/.

The sandbox mirrors the Pi's layout, so the only difference between testing here
and running there is the paths:

    _devdata/music/          <- /media/pi/500gb hdd/Music          (scan root)
    _devdata/youtube_music/  <- /media/pi/500gb hdd/Youtube_Music  (scan root)
    _devdata/library/        <- LIBRARY_ROOT
    _devdata/staging/        <- DOWNLOAD_STAGING
    _devdata/db/             <- the container's SQLite file

Usage:
    python scripts/devdata.py seed            create the tree and sample tracks
    python scripts/devdata.py clean-staging   delete downloads waiting to be processed
    python scripts/devdata.py reset           wipe the sandbox and re-seed
    python scripts/devdata.py status          what is in there now

This is a standalone script rather than a management command on purpose: it
deletes files, and that is not something worth exposing on a box serving a live
library. It also carries a __main__ guard and lives outside the test discovery
pattern, unlike the previous version's repo-root test_tagger.py, which ran a
live tagging pass against the real database whenever anyone typed
`manage.py test` (docs/CODE-AUDIT.md A10).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SANDBOX = REPO / "_devdata"

AREAS = {
    "music": "scan root, standing in for the existing Music folder",
    "youtube_music": "scan root, standing in for Youtube_Music",
    "library": "LIBRARY_ROOT, where organized files land",
    "staging": "DOWNLOAD_STAGING, downloads awaiting identification",
    "db": "SQLite database for the container",
}

#: One frame of MPEG-1 Layer III, 128 kbps, 44.1 kHz. Repeating it produces a
#: file mutagen genuinely parses, tags and re-saves — enough to exercise the
#: whole pipeline without shipping copyrighted audio.
FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413

# Deliberately untidy, and chosen to cover the cases that actually break:
# a flat dump with no album folders, a multi-disc set, a compilation whose
# tracks have different performers, a file with no tags at all, and a title
# full of characters no filesystem accepts.
SAMPLES = [
    ("music/dump/01.mp3",        dict(title="Airbag", artist="Radiohead", album="OK Computer", track=1)),
    ("music/dump/02.mp3",        dict(title="Paranoid Android", artist="Radiohead", album="OK Computer", track=2)),
    ("music/dump/06.mp3",        dict(title="Karma Police", artist="Radiohead", album="OK Computer", track=6)),
    ("music/wall/d1t01.mp3",     dict(title="In The Flesh?", artist="Pink Floyd", album="The Wall", track=1, disc=1)),
    ("music/wall/d2t04.mp3",     dict(title="Hey You", artist="Pink Floyd", album="The Wall", track=4, disc=2)),
    ("music/comp/a.mp3",         dict(title="Song One", artist="Artist A", album="Now 42", aa="Various Artists", track=1, comp=True)),
    ("music/comp/b.mp3",         dict(title="Song Two", artist="Artist B", album="Now 42", aa="Various Artists", track=2, comp=True)),
    ("music/odd/illegal.mp3",    dict(title='Rock & Roll: Part 2 (Live)', artist="AC/DC", album="Live", track=7)),
    ("music/odd/untagged.mp3",   dict()),
    # The YouTube side: id-named files, as the downloader leaves them, with the
    # video title as the only metadata — exactly what identification must work
    # from when AcoustID and Shazam miss.
    ("youtube_music/dQw4w9WgXcQ.mp3", dict(title="Anuv Jain - HUSN (Official Video)")),
    ("youtube_music/aBcDeFgHiJk.mp3", dict(title="Zaalima")),
    ("youtube_music/xYzAbCdEfGh.mp3", dict(title="Coke Studio Bharat | Khalasi | Aditya Gadhvi x Achint")),
    # A byte-identical copy of a track above, so duplicate detection has
    # something real to find.
    ("youtube_music/duplicate-of-airbag.mp3", dict(title="Airbag", artist="Radiohead", album="OK Computer", track=1)),
]


def _write(path: Path, frames: int = 300, **tags) -> None:
    from mutagen.id3 import ID3, TALB, TCMP, TIT2, TPE1, TPE2, TPOS, TRCK

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(FRAME * frames)
    if not tags:
        return
    tag = ID3()
    if "title" in tags:  tag.add(TIT2(encoding=3, text=tags["title"]))
    if "artist" in tags: tag.add(TPE1(encoding=3, text=tags["artist"]))
    if "album" in tags:  tag.add(TALB(encoding=3, text=tags["album"]))
    if "aa" in tags:     tag.add(TPE2(encoding=3, text=tags["aa"]))
    if "track" in tags:  tag.add(TRCK(encoding=3, text=str(tags["track"])))
    if "disc" in tags:   tag.add(TPOS(encoding=3, text=str(tags["disc"])))
    if tags.get("comp"): tag.add(TCMP(encoding=3, text="1"))
    tag.save(path)


def _guard(path: Path) -> Path:
    """Refuse to touch anything outside the sandbox.

    Cheap, and the difference between a reset and a very bad afternoon.
    """
    resolved = path.resolve()
    if resolved != SANDBOX.resolve() and SANDBOX.resolve() not in resolved.parents:
        sys.exit(f"refusing to operate on {resolved}: outside {SANDBOX}")
    return resolved


def cmd_seed(_args) -> None:
    for area in AREAS:
        (SANDBOX / area).mkdir(parents=True, exist_ok=True)
    written = 0
    for relative, tags in SAMPLES:
        target = _guard(SANDBOX / relative)
        if target.exists():
            continue
        _write(target, **tags)
        written += 1
    print(f"sandbox ready at {SANDBOX}")
    print(f"  {written} sample track(s) written "
          f"({len(SAMPLES) - written} already present)")
    cmd_status(None)


def cmd_clean_staging(_args) -> None:
    staging = _guard(SANDBOX / "staging")
    removed = size = 0
    for item in staging.iterdir() if staging.exists() else []:
        if item.is_file():
            size += item.stat().st_size
            item.unlink()
            removed += 1
        elif item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
            removed += 1
    print(f"removed {removed} item(s), {size / 1048576:.0f} MB from {staging}")


def cmd_reset(args) -> None:
    if not args.yes:
        sys.exit("reset deletes the whole sandbox. Re-run with --yes to confirm.")
    target = _guard(SANDBOX)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    print(f"removed {target}")
    cmd_seed(args)


def cmd_status(_args) -> None:
    print(f"\n{'area':16} {'files':>7} {'size':>10}  note")
    for area, note in AREAS.items():
        directory = SANDBOX / area
        if not directory.exists():
            print(f"{area:16} {'-':>7} {'-':>10}  (missing)")
            continue
        files = [p for p in directory.rglob("*") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        print(f"{area:16} {len(files):>7} {total / 1048576:>9.0f}M  {note}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="create the tree and sample tracks").set_defaults(func=cmd_seed)
    sub.add_parser("clean-staging", help="delete pending downloads").set_defaults(func=cmd_clean_staging)
    sub.add_parser("status", help="show what is in the sandbox").set_defaults(func=cmd_status)
    reset = sub.add_parser("reset", help="wipe the sandbox and re-seed")
    reset.add_argument("--yes", action="store_true", help="confirm deletion")
    reset.set_defaults(func=cmd_reset)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
