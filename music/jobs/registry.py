"""
Job handler registry.

Handlers register themselves by decorator, so adding a job kind is one function
and no edit to a central dispatch dict. The old `_HANDLERS` map meant every new
kind touched the runner; here the runner never changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("music.jobs")

#: A handler receives the Job and returns a short human-readable message.
Handler = Callable[[Any], str]


@dataclass(frozen=True)
class JobSpec:
    kind: str
    handler: Handler
    max_attempts: int
    #: Pause after this job kind finishes, to stay polite to remote services.
    cooldown: bool
    description: str


_REGISTRY: dict[str, JobSpec] = {}


def job(
    kind: str,
    *,
    max_attempts: int = 3,
    cooldown: bool = False,
    description: str = "",
) -> Callable[[Handler], Handler]:
    def decorate(func: Handler) -> Handler:
        if kind in _REGISTRY:
            raise RuntimeError(f"job kind {kind!r} is already registered")
        _REGISTRY[kind] = JobSpec(
            kind=kind,
            handler=func,
            max_attempts=max_attempts,
            cooldown=cooldown,
            description=description or (func.__doc__ or "").strip().split("\n")[0],
        )
        return func

    return decorate


def get(kind: str) -> JobSpec | None:
    return _REGISTRY.get(kind)


def all_specs() -> dict[str, JobSpec]:
    return dict(_REGISTRY)


def load_handlers() -> None:
    """Import the modules that register handlers.

    Called once from the worker bootstrap. Import errors are logged and
    swallowed per module so a missing optional dependency (shazamio, say)
    disables one job kind instead of taking down the web app.
    """
    modules = (
        "music.jobs.handlers.library",
        "music.jobs.handlers.identify",
        "music.jobs.handlers.organize",
        "music.jobs.handlers.youtube",
        "music.jobs.handlers.maintenance",
    )
    for name in modules:
        try:
            __import__(name)
        except Exception:
            log.exception("could not load job handlers from %s", name)
