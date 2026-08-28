"""
The worker pool and scheduler.

Workers **block on an event**, they do not poll. `engine.enqueue` sets the
event; a worker wakes, drains everything claimable, and goes back to waiting.
An idle system therefore issues zero queries — the previous version ran one
SELECT per worker every two seconds forever, which on a Pi 2 is a measurable
share of a core doing nothing.

`WORKER_IDLE_WAKE_SECONDS` is a safety net for work enqueued by another process
(a management command), not a poll interval; five minutes is a fine default.
"""

from __future__ import annotations

import logging
import threading
import time

from django.conf import settings
from django.db import close_old_connections

from music.core import runtime
from music.core.events import bump

from . import engine, registry

log = logging.getLogger("music.worker")

_started = False
_start_lock = threading.Lock()
_stop = threading.Event()
_wake = threading.Event()
_threads: list[threading.Thread] = []


def start() -> bool:
    """Boot the pool. Idempotent; safe to call from wsgi import."""
    global _started
    with _start_lock:
        if _started:
            return True

        lock_path = settings.BASE_DIR / ".music_manager.lock"
        if not runtime.acquire_singleton(lock_path):
            log.error(
                "another music_manager process already holds %s. Workers will NOT "
                "start in this process. Run gunicorn with --workers 1 and scale "
                "with WORKER_THREADS instead.",
                lock_path,
            )
            return False

        # Handlers are normally registered by MusicConfig.ready(); this is a
        # no-op then, and a safety net for any entry point that starts the pool
        # without Django's app registry having run.
        registry.load_handlers()
        engine.set_wake_event(_wake)

        try:
            engine.requeue_orphans()
        except Exception:
            log.exception("could not requeue interrupted jobs; continuing")

        count = settings.WORKER_THREADS
        for index in range(count):
            thread = threading.Thread(
                target=_worker_loop, name=f"worker-{index}", daemon=True
            )
            thread.start()
            _threads.append(thread)

        scheduler = threading.Thread(
            target=_scheduler_loop, name="scheduler", daemon=True
        )
        scheduler.start()
        _threads.append(scheduler)

        _started = True
        log.info(
            "started %s worker thread(s); %s job kind(s) registered",
            count, len(registry.all_specs()),
        )
        # Anything left QUEUED from last run should go now.
        _wake.set()
        return True


def stop(timeout: float = 5.0) -> None:
    """Signal shutdown and wait briefly. Used by tests and management commands."""
    global _started
    _stop.set()
    _wake.set()
    for thread in _threads:
        thread.join(timeout=timeout)
    _threads.clear()
    runtime.release_singleton()
    _stop.clear()
    _wake.clear()
    _started = False


def is_running() -> bool:
    return _started


# --------------------------------------------------------------------------


def _worker_loop() -> None:
    while not _stop.is_set():
        # Wait for work. The timeout only catches jobs enqueued out-of-process.
        _wake.wait(timeout=settings.WORKER_IDLE_WAKE_SECONDS)
        if _stop.is_set():
            return
        # Clear before draining: an enqueue during the drain re-sets it and we
        # simply loop again, so no wakeup can be lost.
        _wake.clear()
        try:
            _drain()
        except Exception:
            log.exception("worker loop error; continuing")
            time.sleep(1.0)


def _drain() -> None:
    while not _stop.is_set():
        close_old_connections()
        try:
            job = engine.claim_next()
        except Exception:
            log.exception("could not claim a job")
            return
        if job is None:
            return
        _run(job)


def _run(job) -> None:
    spec = registry.get(job.kind)
    if spec is None:
        engine.finish_failure(job, f"no handler registered for kind {job.kind!r}")
        return

    log.info("running %s #%s", job.kind, job.pk)
    bump("jobs")
    started = time.monotonic()
    try:
        message = spec.handler(job) or ""
    except Exception as exc:
        import traceback

        engine.finish_failure(job, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
    else:
        engine.finish_success(job, message)
        log.info("finished %s #%s in %.1fs: %s",
                 job.kind, job.pk, time.monotonic() - started, message[:120])
    finally:
        close_old_connections()

    if spec.cooldown and settings.WORKER_COOLDOWN_SECONDS > 0:
        # Interruptible: a shutdown should not wait out the cooldown.
        _stop.wait(settings.WORKER_COOLDOWN_SECONDS)


def _scheduler_loop() -> None:
    """Periodic maintenance: lease reaping, pruning, optional sync and rescan.

    One thread handles all of it. Each task tracks its own next-due time, so
    the thread wakes once a minute, does nothing in the common case, and sleeps
    again.
    """
    from music.jobs import scheduled

    next_due: dict[str, float] = {}
    tick = 60.0

    while not _stop.is_set():
        now = time.monotonic()
        for task in scheduled.tasks():
            if task.interval_seconds <= 0:
                continue
            due = next_due.get(task.name)
            if due is None:
                # Stagger first runs so a restart does not fire everything at once.
                next_due[task.name] = now + min(task.interval_seconds, 30.0)
                continue
            if now >= due:
                next_due[task.name] = now + task.interval_seconds
                close_old_connections()
                try:
                    task.run()
                except Exception:
                    log.exception("scheduled task %s failed", task.name)
        _stop.wait(tick)
