"""In-process change broadcast. Correct only in a single process (core.runtime)."""

from __future__ import annotations

import threading
import time

_condition = threading.Condition(threading.Lock())
_revision = 0

#: Advisory only: the revision is global, the topic set says what moved.
_topics: set[str] = set()


def bump(topic: str = "state") -> int:
    """Record that something changed and wake every waiting stream."""
    global _revision
    with _condition:
        _revision += 1
        _topics.add(topic)
        _condition.notify_all()
        return _revision


def current() -> int:
    with _condition:
        return _revision


def snapshot() -> tuple[int, frozenset[str]]:
    with _condition:
        return _revision, frozenset(_topics)


def wait_for_change(since: int, timeout: float) -> tuple[int, frozenset[str]]:
    """Block until the revision differs from `since`, or `timeout` elapses."""
    deadline = time.monotonic() + timeout
    with _condition:
        while _revision == since:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _condition.wait(remaining)
        return _revision, frozenset(_topics)


def reset_for_tests() -> None:
    global _revision
    with _condition:
        _revision = 0
        _topics.clear()
