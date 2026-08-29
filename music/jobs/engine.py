"""Job queue mechanics: enqueue, claim, complete, reap."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from music.core import events
from music.models import Job, JobState

from . import registry

log = logging.getLogger("music.jobs")


def enqueue(
    kind: str,
    payload: dict | None = None,
    *,
    dedup_key: str | None = None,
    priority: int = 0,
    max_attempts: int | None = None,
    delay_seconds: float = 0.0,
) -> Job:
    """Create a job, or return the existing active one with the same dedup key."""
    spec = registry.get(kind)
    if spec is None:
        raise ValueError(f"unknown job kind: {kind!r}")

    scheduled_for = timezone.now() + timedelta(seconds=max(0.0, delay_seconds))
    key = dedup_key or ""

    try:
        with transaction.atomic():
            job = Job.objects.create(
                kind=kind,
                payload=payload or {},
                dedup_key=key,
                priority=priority,
                max_attempts=max_attempts or spec.max_attempts,
                scheduled_for=scheduled_for,
            )
    except IntegrityError:
        # The partial unique index rejected it: an equivalent job is already
        # QUEUED or RUNNING.
        existing = (
            Job.objects.filter(dedup_key=key, state__in=JobState.active())
            .order_by("id")
            .first()
        )
        if existing is not None:
            log.debug("enqueue: reusing active %s job #%s", kind, existing.pk)
            return existing
        raise  # the constraint fired for some other reason; do not mask it

    log.info("enqueued %s #%s", kind, job.pk)
    wake_workers()
    events.bump("jobs")
    return job


def claim_next() -> Job | None:
    """Atomically claim the highest-priority due job.

    The conditional UPDATE is the entire lock: SQLite runs a single statement
    atomically, so exactly one worker can move a given row out of QUEUED.
    """
    now = timezone.now()
    lease_until = now + timedelta(seconds=settings.JOB_LEASE_SECONDS)

    candidates = (
        Job.objects.filter(state=JobState.QUEUED, scheduled_for__lte=now)
        .order_by("-priority", "scheduled_for", "id")
        .values_list("id", flat=True)[:20]
    )

    for job_id in list(candidates):
        updated = Job.objects.filter(id=job_id, state=JobState.QUEUED).update(
            state=JobState.RUNNING,
            started_at=now,
            lease_expires_at=lease_until,
            attempts=F("attempts") + 1,
        )
        if updated:
            return Job.objects.get(id=job_id)
    return None


def heartbeat(job: Job, status: str = "") -> None:
    """Extend a lease, optionally recording what the job is doing right now.

    `status` rides along on the write that was happening anyway, so live
    progress costs no extra query. Callers must already be throttling their
    heartbeats — a progress hook that fires per chunk must not reach here.
    """
    lease_until = timezone.now() + timedelta(seconds=settings.JOB_LEASE_SECONDS)
    fields = {"lease_expires_at": lease_until}
    if status:
        fields["message"] = status[:2000]
    Job.objects.filter(id=job.pk, state=JobState.RUNNING).update(**fields)
    if status:
        events.bump("jobs")


def finish_success(job: Job, message: str = "") -> None:
    Job.objects.filter(id=job.pk).update(
        state=JobState.SUCCEEDED,
        message=(message or "")[:2000],
        error="",
        finished_at=timezone.now(),
        lease_expires_at=None,
    )
    events.bump("jobs")


def finish_failure(job: Job, error: str) -> None:
    """Retry with backoff while attempts remain, then fail terminally."""
    job.refresh_from_db(fields=["attempts", "max_attempts"])
    text = (error or "")[:4000]

    if job.attempts < job.max_attempts:
        delay = min(60.0 * (2 ** (job.attempts - 1)), 3600.0)
        Job.objects.filter(id=job.pk).update(
            state=JobState.QUEUED,
            error=text,
            lease_expires_at=None,
            started_at=None,
            scheduled_for=timezone.now() + timedelta(seconds=delay),
        )
        log.warning(
            "job %s #%s failed (attempt %s/%s), retrying in %.0fs: %s",
            job.kind, job.pk, job.attempts, job.max_attempts, delay, text[:200],
        )
    else:
        Job.objects.filter(id=job.pk).update(
            state=JobState.FAILED,
            error=text,
            finished_at=timezone.now(),
            lease_expires_at=None,
        )
        log.error(
            "job %s #%s failed permanently after %s attempts: %s",
            job.kind, job.pk, job.attempts, text[:200],
        )
    events.bump("jobs")


def reap(now=None) -> dict[str, int]:
    """Reclaim expired leases and prune old terminal rows."""
    now = now or timezone.now()

    reclaimed = Job.objects.filter(
        state=JobState.RUNNING, lease_expires_at__lt=now
    ).update(
        state=JobState.QUEUED,
        started_at=None,
        lease_expires_at=None,
        error="lease expired; the worker holding this job stopped responding",
    )
    if reclaimed:
        log.warning("reclaimed %s job(s) with expired leases", reclaimed)
        wake_workers()

    cutoff = now - timedelta(days=settings.JOB_RETENTION_DAYS)
    pruned, _ = Job.objects.filter(
        state__in=(JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED),
        finished_at__lt=cutoff,
    ).delete()
    if pruned:
        log.info("pruned %s finished job(s) older than %s days",
                 pruned, settings.JOB_RETENTION_DAYS)

    if reclaimed or pruned:
        events.bump("jobs")
    return {"reclaimed": reclaimed, "pruned": pruned}


def requeue_orphans() -> int:
    """At boot, return any RUNNING rows to the queue.

    Safe only because the runtime guard guarantees a single process; a second
    process would steal jobs a sibling is actively running.
    """
    count = Job.objects.filter(state=JobState.RUNNING).update(
        state=JobState.QUEUED,
        started_at=None,
        lease_expires_at=None,
        error="interrupted by a restart",
    )
    if count:
        log.info("requeued %s interrupted job(s) from the previous run", count)
    return count


# Set by the worker pool at startup. In-process, so the app must run as a
# single process — workers block on this event instead of polling.
_wake_event = None


def set_wake_event(event) -> None:
    global _wake_event
    _wake_event = event


def wake_workers() -> None:
    if _wake_event is not None:
        _wake_event.set()
