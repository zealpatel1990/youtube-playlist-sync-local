"""
In-process change broadcast.

This replaces the previous design's SSE polling, where every connected browser
tab fetched and sorted every Video and LocalTrack row every few seconds
(docs/CODE-AUDIT.md A2) — a cost that scaled with library size multiplied by
open tabs, burning CPU on an otherwise idle Pi.

Here, anything that changes user-visible state calls `bump()`. SSE streams
block on a condition variable until the revision moves. An idle dashboard
issues **zero queries per tick, regardless of how many tabs are open**, and a
single change wakes every waiter at once instead of each rediscovering it.

Correctness depends on the app running as ONE process — see core.runtime, which
enforces that at startup rather than trusting a comment.
"""

from __future__ import annotations

import threading
import time

_condition = threading.Condition(threading.Lock())
_revision = 0

#: Coarse topics let the dashboard refetch only the fragment that changed.
#: A topic is advisory: the revision is global, the topic set says what moved.
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
    """Block until the revision differs from `since`, or `timeout` elapses.

    Returns the current revision and the topics touched. When the revision is
    unchanged the caller should emit a keepalive rather than a change event.
    """
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
