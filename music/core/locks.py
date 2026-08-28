"""
Per-key mutexes.

The previous version could run a DELETE job for a track while a DOWNLOAD job
for the same track was mid-flight on another worker thread: the file was
removed and the row deleted underneath the downloader, whose next save()
silently re-INSERTed the deleted row because the primary key was still set
(docs/CODE-AUDIT.md A5).

Everything runs in one process, so a keyed mutex is the whole fix — no
distributed lock, no advisory row locking, no extra dependency.

Locks are reference-counted and dropped when idle, so scanning a large library
does not leave one lock object per track alive forever.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager


class KeyedLocks:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        #: key -> [lock, waiter_count]
        self._locks: dict[str, list] = {}

    def _acquire_entry(self, key: str) -> threading.RLock:
        with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                # Re-entrant on purpose. A job handler takes the track's lock
                # and then calls library code that takes it again for exactly
                # the same reason (organizer.apply_track, revert_track). With a
                # plain Lock that nesting is a deadlock which no timeout would
                # catch — the worker would sit there until the lease expired,
                # the reaper would requeue the job, and it would wedge again.
                # RLock keeps mutual exclusion between threads, which is the
                # whole guarantee this class exists to provide.
                entry = [threading.RLock(), 0]
                self._locks[key] = entry
            entry[1] += 1
            return entry[0]

    def _release_entry(self, key: str) -> None:
        with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                return
            entry[1] -= 1
            if entry[1] <= 0:
                self._locks.pop(key, None)

    @contextmanager
    def acquire(self, key: str, *, timeout: float | None = None):
        """Hold the lock for `key` for the duration of the block.

        Yields True when the lock was taken, False when `timeout` elapsed
        first. Callers that pass a timeout MUST check the yielded value:

            with locks.acquire(key, timeout=0) as got:
                if not got:
                    return  # someone else owns this track right now
        """
        lock = self._acquire_entry(key)
        acquired = False
        try:
            if timeout is None:
                lock.acquire()
                acquired = True
            else:
                acquired = lock.acquire(timeout=timeout) if timeout > 0 else lock.acquire(blocking=False)
            yield acquired
        finally:
            if acquired:
                lock.release()
            self._release_entry(key)

    def held_count(self) -> int:
        with self._guard:
            return len(self._locks)


#: Guards all work touching a single Track row / file.
track_locks = KeyedLocks()
