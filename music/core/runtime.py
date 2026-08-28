"""
Single-process guard.

The whole design — in-process worker wakeup, the SSE revision counter, keyed
locks, requeue-orphans-at-boot — is correct only while exactly one process owns
the database. The previous version stated that requirement in a docstring and
enforced nothing, so a stray `--workers 2` would have silently produced double
downloads and jobs stolen from a live sibling (docs/CODE-AUDIT.md A6).

An exclusive advisory lock on a file makes the requirement checkable. It is
best-effort: on a platform without file locking the guard degrades to a warning
rather than blocking startup, because refusing to boot the user's live service
over a missing syscall would be worse than the risk it guards.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("music.runtime")

_handle = None


def acquire_singleton(lock_path: str | Path) -> bool:
    """Take the process lock. False means another process already holds it."""
    global _handle

    lock_path = Path(lock_path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+")
    except OSError as exc:
        log.warning("could not open %s (%s); single-process guard disabled",
                    lock_path, exc)
        return True

    if not _lock_exclusive(handle):
        handle.close()
        return False

    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
    except OSError:
        pass

    _handle = handle  # held for the process lifetime
    return True


def _lock_exclusive(handle) -> bool:
    """Non-blocking exclusive lock. True if taken, False if held elsewhere."""
    try:
        import fcntl
    except ImportError:
        pass
    else:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    try:
        import msvcrt
    except ImportError:
        log.warning("no file-locking API available; single-process guard disabled")
        return True

    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def release_singleton() -> None:
    global _handle
    if _handle is not None:
        try:
            _handle.close()
        except OSError:
            pass
        _handle = None
