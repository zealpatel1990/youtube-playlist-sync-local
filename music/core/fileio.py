"""Filesystem primitives: hashing, atomic writes, crash-safe moves."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger("music.fileio")

CHUNK = 1024 * 1024

#: Characters no common filesystem accepts.
_ILLEGAL = '<>:"/\\|?*\0'
#: Windows refuses these basenames regardless of extension.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def hash_file(path: str | Path, *, algorithm: str = "sha1") -> str:
    """Content hash, used for duplicate detection. Returns "" if unreadable."""
    digest = hashlib.new(algorithm)
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(CHUNK):
                digest.update(chunk)
    except OSError as exc:
        log.warning("hash failed for %s: %s", path, exc)
        return ""
    return digest.hexdigest()


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write so the file is either fully old or fully new, never truncated."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding=encoding,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def sanitize_component(name: str, *, fallback: str = "Unknown") -> str:
    """Make one path segment safe. Only genuinely unsafe characters are touched."""
    cleaned = "".join("_" if ch in _ILLEGAL else ch for ch in (name or ""))
    # Control characters break some filesystems and all terminals.
    cleaned = "".join(ch for ch in cleaned if ch >= " ")
    cleaned = cleaned.strip().rstrip(". ")  # trailing dot/space is invalid on Windows
    if cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = f"_{cleaned}"
    if not cleaned:
        return fallback
    # Keep well clear of the 255-byte per-component limit once UTF-8 encoded.
    encoded = cleaned.encode("utf-8")
    if len(encoded) > 200:
        # rstrip again: the cut can land right after a dot, and Windows/exFAT
        # silently drop a trailing dot on create, so the file would land at a
        # path that never matches the computed one and be "moved" every pass.
        cleaned = encoded[:200].decode("utf-8", errors="ignore").strip().rstrip(". ")
    return cleaned or fallback


def unique_path(target: str | Path) -> Path:
    """Return `target`, or the first free "name (2).ext" variant."""
    target = Path(target)
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for counter in range(2, 1000):
        candidate = target.with_name(f"{stem} ({counter}){suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not find a free name near {target}")


def move_file(source: str | Path, target: str | Path, *, overwrite: bool = False) -> Path:
    """Move a file, returning the path actually written (may differ from `target`).

    Across filesystems the copy is size-verified before the source is removed, so
    an interrupted move can leave a stray temp file but never loses data.
    """
    source, target = Path(source), Path(target)
    if not source.exists():
        raise FileNotFoundError(source)

    target.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite:
        target = unique_path(target)

    try:
        os.replace(source, target)
        return target
    except OSError:
        pass  # cross-device, or a platform that refuses replace across mounts

    tmp = target.with_name(f".{target.name}.partial")
    try:
        shutil.copy2(source, tmp)
        if tmp.stat().st_size != source.stat().st_size:
            raise OSError(f"size mismatch after copying {source} -> {target}")
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    source.unlink(missing_ok=True)
    return target


def prune_empty_dirs(start: str | Path, stop_at: str | Path) -> int:
    """Remove directories left empty by a move, walking up to (not past) stop_at."""
    start, stop_at = Path(start), Path(stop_at).resolve()
    removed = 0
    current = start if start.is_dir() else start.parent
    while True:
        current = current.resolve()
        if current == stop_at or stop_at not in current.parents:
            break
        try:
            next(current.iterdir())
            break  # not empty
        except StopIteration:
            pass
        except OSError:
            break
        try:
            current.rmdir()
            removed += 1
        except OSError:
            break
        current = current.parent
    return removed


def is_within(path: str | Path, root: str | Path) -> bool:
    """True when `path` is inside `root`. Guards every destructive operation."""
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False
