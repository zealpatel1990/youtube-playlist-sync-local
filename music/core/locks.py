"""Per-key mutexes, so two jobs never touch the same track at once."""

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
                # RLock, not Lock: a handler takes the track lock and then calls
                # library code (organizer.apply_track) that takes it again.
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

        Yields True when the lock was taken, False when `timeout` elapsed first;
        a caller passing a timeout MUST check the yielded value.
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
