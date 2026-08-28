"""Tests for the job engine and handler registry.

These need the database: the whole point of the design is that dedup and
claiming are *database* guarantees (a partial unique index and a single
conditional UPDATE), not Python checks that two threads could both pass.

`TestCase` for the sequential tests; `TransactionTestCase` for the concurrency
ones, because threads need committed rows to see each other's work.
"""

from __future__ import annotations

import itertools
import threading
import time
from datetime import timedelta

from django.db import OperationalError, connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from music.core import events
from music.jobs import engine, registry
from music.models import Job, JobState

_kind_counter = itertools.count()

#: Upper bound on "this concurrent test should already be finished".
PATIENCE = 25.0


def register_test_kind(testcase, *, max_attempts=3, cooldown=False,
                       description="a test job"):
    """Register a uniquely named job kind, unregistered again on teardown."""
    kind = f"test-kind-{next(_kind_counter)}"

    @registry.job(kind, max_attempts=max_attempts, cooldown=cooldown,
                  description=description)
    def _handler(job):
        return "done"

    testcase.addCleanup(registry._REGISTRY.pop, kind, None)
    return kind


# ==========================================================================
# registry
# ==========================================================================


class RegistryTests(SimpleTestCase):
    def _kind(self):
        kind = f"test-registry-{next(_kind_counter)}"
        self.addCleanup(registry._REGISTRY.pop, kind, None)
        return kind

    def test_the_decorator_registers_a_spec(self):
        kind = self._kind()

        @registry.job(kind, max_attempts=7, cooldown=True, description="Explicit.")
        def handler(job):
            return "ok"

        spec = registry.get(kind)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.kind, kind)
        self.assertIs(spec.handler, handler)
        self.assertEqual(spec.max_attempts, 7)
        self.assertIs(spec.cooldown, True)
        self.assertEqual(spec.description, "Explicit.")

    def test_the_decorator_returns_the_original_function(self):
        kind = self._kind()

        def original(job):
            return "ok"

        decorated = registry.job(kind)(original)
        self.assertIs(decorated, original)

    def test_defaults(self):
        kind = self._kind()

        @registry.job(kind)
        def handler(job):
            return "ok"

        spec = registry.get(kind)
        self.assertEqual(spec.max_attempts, 3)
        self.assertIs(spec.cooldown, False)

    def test_description_defaults_to_the_first_docstring_line(self):
        kind = self._kind()

        @registry.job(kind)
        def handler(job):
            """Download one track.

            Longer explanation that must not end up in the description.
            """
            return "ok"

        self.assertEqual(registry.get(kind).description, "Download one track.")

    def test_description_is_empty_without_a_docstring(self):
        kind = self._kind()

        @registry.job(kind)
        def handler(job):
            return "ok"

        self.assertEqual(registry.get(kind).description, "")

    def test_a_duplicate_kind_raises(self):
        kind = self._kind()

        @registry.job(kind)
        def first(job):
            return "first"

        with self.assertRaises(RuntimeError) as caught:

            @registry.job(kind)
            def second(job):
                return "second"

        self.assertIn(kind, str(caught.exception))
        self.assertIn("already registered", str(caught.exception))
        # The incumbent must survive the failed re-registration.
        self.assertIs(registry.get(kind).handler, first)

    def test_get_returns_none_for_an_unknown_kind(self):
        self.assertIsNone(registry.get("no-such-kind"))

    def test_all_specs_returns_a_copy(self):
        kind = self._kind()

        @registry.job(kind)
        def handler(job):
            return "ok"

        specs = registry.all_specs()
        self.assertIn(kind, specs)
        specs.clear()
        self.assertIsNotNone(registry.get(kind), "all_specs() aliased the registry")


# ==========================================================================
# engine — enqueue
# ==========================================================================


class EnqueueTests(TestCase):
    def test_enqueue_creates_a_queued_job(self):
        kind = register_test_kind(self, max_attempts=5)

        job = engine.enqueue(kind, {"track_id": 7}, priority=3)

        job.refresh_from_db()
        self.assertEqual(job.kind, kind)
        self.assertEqual(job.state, JobState.QUEUED)
        self.assertEqual(job.payload, {"track_id": 7})
        self.assertEqual(job.priority, 3)
        self.assertEqual(job.attempts, 0)
        self.assertEqual(job.max_attempts, 5)
        self.assertEqual(job.dedup_key, "")
        self.assertIsNone(job.lease_expires_at)
        self.assertIsNone(job.started_at)
        self.assertIsNone(job.finished_at)
        self.assertLessEqual(job.scheduled_for, timezone.now())

    def test_enqueue_defaults_the_payload_to_an_empty_dict(self):
        kind = register_test_kind(self)
        self.assertEqual(engine.enqueue(kind).payload, {})

    def test_enqueue_wakes_the_workers(self):
        kind = register_test_kind(self)
        wake = threading.Event()
        engine.set_wake_event(wake)
        self.addCleanup(engine.set_wake_event, None)

        self.assertFalse(wake.is_set())
        engine.enqueue(kind)
        self.assertTrue(wake.is_set(), "enqueue did not wake the worker pool")

    def test_enqueue_bumps_the_change_revision(self):
        kind = register_test_kind(self)
        before = events.current()
        engine.enqueue(kind)
        self.assertGreater(events.current(), before)
        self.assertIn("jobs", events.snapshot()[1])

    def test_wake_workers_without_an_event_is_a_no_op(self):
        engine.set_wake_event(None)
        engine.wake_workers()  # must not raise

    def test_max_attempts_can_be_overridden_per_job(self):
        kind = register_test_kind(self, max_attempts=3)
        self.assertEqual(engine.enqueue(kind, max_attempts=9).max_attempts, 9)

    def test_delay_seconds_schedules_the_job_for_later(self):
        kind = register_test_kind(self)
        job = engine.enqueue(kind, delay_seconds=600)
        delta = (job.scheduled_for - timezone.now()).total_seconds()
        self.assertGreater(delta, 590)
        self.assertLessEqual(delta, 600)

    def test_a_negative_delay_is_treated_as_immediate(self):
        kind = register_test_kind(self)
        job = engine.enqueue(kind, delay_seconds=-100)
        self.assertLessEqual(job.scheduled_for, timezone.now())

    def test_an_unknown_kind_raises(self):
        with self.assertRaises(ValueError) as caught:
            engine.enqueue("no-such-kind")
        self.assertIn("no-such-kind", str(caught.exception))
        self.assertEqual(Job.objects.count(), 0)


class DedupTests(TestCase):
    def test_the_same_dedup_key_returns_the_same_job(self):
        kind = register_test_kind(self)

        first = engine.enqueue(kind, {"attempt": 1}, dedup_key="track:7")
        second = engine.enqueue(kind, {"attempt": 2}, dedup_key="track:7")

        self.assertEqual(second.pk, first.pk)
        self.assertEqual(Job.objects.count(), 1)
        # The incumbent wins; the duplicate's payload is discarded.
        self.assertEqual(Job.objects.get().payload, {"attempt": 1})

    def test_repeated_enqueues_never_raise(self):
        kind = register_test_kind(self)
        jobs = [engine.enqueue(kind, dedup_key="track:7") for _ in range(10)]
        self.assertEqual(len({job.pk for job in jobs}), 1)
        self.assertEqual(Job.objects.count(), 1)

    def test_dedup_also_matches_a_running_job(self):
        # The wedge the lease exists to unstick: a RUNNING job still owns its
        # dedup key, so re-enqueuing returns it rather than duplicating work.
        kind = register_test_kind(self)
        first = engine.enqueue(kind, dedup_key="track:7")
        engine.claim_next()

        second = engine.enqueue(kind, dedup_key="track:7")

        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.state, JobState.RUNNING)
        self.assertEqual(Job.objects.count(), 1)

    def test_different_dedup_keys_do_not_collide(self):
        kind = register_test_kind(self)
        first = engine.enqueue(kind, dedup_key="track:7")
        second = engine.enqueue(kind, dedup_key="track:8")
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(Job.objects.count(), 2)

    def test_an_empty_dedup_key_never_collides(self):
        # Most jobs are not dedupable; many of them must be able to coexist.
        kind = register_test_kind(self)
        jobs = [engine.enqueue(kind) for _ in range(5)]
        jobs += [engine.enqueue(kind, dedup_key="") for _ in range(5)]
        jobs += [engine.enqueue(kind, dedup_key=None) for _ in range(5)]

        self.assertEqual(len({job.pk for job in jobs}), 15)
        self.assertEqual(Job.objects.count(), 15)

    def test_a_finished_job_releases_its_dedup_key(self):
        kind = register_test_kind(self)
        first = engine.enqueue(kind, dedup_key="track:7")
        engine.finish_success(first, "done")

        second = engine.enqueue(kind, dedup_key="track:7")

        self.assertNotEqual(second.pk, first.pk)
        self.assertEqual(second.state, JobState.QUEUED)
        self.assertEqual(Job.objects.count(), 2)

    def test_a_failed_job_releases_its_dedup_key(self):
        kind = register_test_kind(self, max_attempts=1)
        first = engine.enqueue(kind, dedup_key="track:7")
        claimed = engine.claim_next()
        engine.finish_failure(claimed, "boom")
        self.assertEqual(Job.objects.get(pk=first.pk).state, JobState.FAILED)

        second = engine.enqueue(kind, dedup_key="track:7")
        self.assertNotEqual(second.pk, first.pk)


# ==========================================================================
# engine — claim
# ==========================================================================


class ClaimTests(TestCase):
    def test_claim_next_returns_none_when_the_queue_is_empty(self):
        self.assertIsNone(engine.claim_next())

    @override_settings(JOB_LEASE_SECONDS=60.0)
    def test_claim_transitions_queued_to_running_with_a_lease(self):
        kind = register_test_kind(self)
        job = engine.enqueue(kind)

        claimed = engine.claim_next()

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.pk, job.pk)
        self.assertEqual(claimed.state, JobState.RUNNING)
        self.assertEqual(claimed.attempts, 1)
        self.assertIsNotNone(claimed.started_at)
        self.assertIsNotNone(claimed.lease_expires_at)
        self.assertAlmostEqual(
            (claimed.lease_expires_at - claimed.started_at).total_seconds(),
            60.0,
            places=3,
        )

    def test_a_claimed_job_is_not_claimable_again(self):
        kind = register_test_kind(self)
        engine.enqueue(kind)
        self.assertIsNotNone(engine.claim_next())
        self.assertIsNone(engine.claim_next())

    def test_attempts_increments_on_every_claim(self):
        kind = register_test_kind(self, max_attempts=5)
        job = engine.enqueue(kind)

        for expected in (1, 2, 3):
            claimed = engine.claim_next()
            self.assertEqual(claimed.attempts, expected)
            Job.objects.filter(pk=job.pk).update(
                state=JobState.QUEUED, scheduled_for=timezone.now()
            )

    def test_claim_skips_jobs_scheduled_in_the_future(self):
        kind = register_test_kind(self)
        job = engine.enqueue(kind, delay_seconds=3600)

        self.assertIsNone(engine.claim_next())

        Job.objects.filter(pk=job.pk).update(
            scheduled_for=timezone.now() - timedelta(seconds=1)
        )
        self.assertIsNotNone(engine.claim_next())

    def test_a_due_job_is_claimed_while_a_delayed_sibling_waits(self):
        kind = register_test_kind(self)
        delayed = engine.enqueue(kind, dedup_key="later", delay_seconds=3600)
        due = engine.enqueue(kind, dedup_key="now")

        claimed = engine.claim_next()

        self.assertEqual(claimed.pk, due.pk)
        self.assertEqual(Job.objects.get(pk=delayed.pk).state, JobState.QUEUED)

    def test_higher_priority_is_claimed_first(self):
        kind = register_test_kind(self)
        low = engine.enqueue(kind, {"n": "low"}, priority=-5)
        normal = engine.enqueue(kind, {"n": "normal"}, priority=0)
        high = engine.enqueue(kind, {"n": "high"}, priority=10)

        order = [engine.claim_next().pk for _ in range(3)]

        self.assertEqual(order, [high.pk, normal.pk, low.pk])

    def test_equal_priority_is_claimed_in_enqueue_order(self):
        kind = register_test_kind(self)
        first = engine.enqueue(kind, {"n": 1})
        second = engine.enqueue(kind, {"n": 2})
        third = engine.enqueue(kind, {"n": 3})

        order = [engine.claim_next().pk for _ in range(3)]

        self.assertEqual(order, [first.pk, second.pk, third.pk])

    def test_terminal_jobs_are_never_claimed(self):
        kind = register_test_kind(self)
        for state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
            Job.objects.create(kind=kind, state=state)
        self.assertIsNone(engine.claim_next())


# ==========================================================================
# engine — finish
# ==========================================================================


class FinishTests(TestCase):
    def _claimed(self, **kwargs):
        kind = register_test_kind(self, **kwargs)
        engine.enqueue(kind)
        return engine.claim_next()

    def test_finish_success(self):
        job = self._claimed()

        engine.finish_success(job, "moved 3 files")

        job.refresh_from_db()
        self.assertEqual(job.state, JobState.SUCCEEDED)
        self.assertEqual(job.message, "moved 3 files")
        self.assertEqual(job.error, "")
        self.assertIsNotNone(job.finished_at)
        self.assertIsNone(job.lease_expires_at)

    def test_finish_success_clears_a_previous_error(self):
        job = self._claimed(max_attempts=5)
        engine.finish_failure(job, "transient")
        Job.objects.filter(pk=job.pk).update(
            state=JobState.RUNNING, scheduled_for=timezone.now()
        )
        job.refresh_from_db()

        engine.finish_success(job, "worked this time")

        job.refresh_from_db()
        self.assertEqual(job.error, "")
        self.assertEqual(job.message, "worked this time")

    def test_finish_success_truncates_a_long_message(self):
        job = self._claimed()
        engine.finish_success(job, "x" * 5000)
        job.refresh_from_db()
        self.assertEqual(len(job.message), 2000)

    def test_finish_failure_requeues_with_backoff_then_fails_terminally(self):
        kind = register_test_kind(self, max_attempts=2)
        engine.enqueue(kind)

        first = engine.claim_next()
        self.assertEqual(first.attempts, 1)
        engine.finish_failure(first, "boom")

        job = Job.objects.get(pk=first.pk)
        self.assertEqual(job.state, JobState.QUEUED, "attempts remained; expected a retry")
        self.assertEqual(job.error, "boom")
        self.assertIsNone(job.lease_expires_at)
        self.assertIsNone(job.started_at)
        self.assertIsNone(job.finished_at)
        self.assertGreater(job.scheduled_for, timezone.now())

        # The backoff really holds it back from the queue.
        self.assertIsNone(engine.claim_next())
        Job.objects.filter(pk=job.pk).update(
            scheduled_for=timezone.now() - timedelta(seconds=1)
        )

        second = engine.claim_next()
        self.assertEqual(second.attempts, 2)
        engine.finish_failure(second, "boom again")

        job.refresh_from_db()
        self.assertEqual(job.state, JobState.FAILED, "attempts exhausted; expected FAILED")
        self.assertEqual(job.error, "boom again", "the error was not retained")
        self.assertEqual(job.attempts, 2)
        self.assertIsNotNone(job.finished_at)
        self.assertIsNone(job.lease_expires_at)

    def test_the_backoff_grows_exponentially(self):
        kind = register_test_kind(self, max_attempts=5)
        job = engine.enqueue(kind)

        for attempt, expected_delay in ((1, 60.0), (2, 120.0), (3, 240.0)):
            claimed = engine.claim_next()
            self.assertEqual(claimed.attempts, attempt)
            engine.finish_failure(claimed, "boom")
            job.refresh_from_db()
            delay = (job.scheduled_for - timezone.now()).total_seconds()
            self.assertGreater(delay, expected_delay - 10)
            self.assertLessEqual(delay, expected_delay)
            Job.objects.filter(pk=job.pk).update(
                scheduled_for=timezone.now() - timedelta(seconds=1)
            )

    def test_a_single_attempt_kind_fails_immediately(self):
        job = self._claimed(max_attempts=1)
        engine.finish_failure(job, "boom")
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.FAILED)

    def test_finish_failure_truncates_a_long_error(self):
        job = self._claimed(max_attempts=1)
        engine.finish_failure(job, "x" * 9000)
        job.refresh_from_db()
        self.assertEqual(len(job.error), 4000)

    def test_finish_failure_bumps_the_change_revision(self):
        job = self._claimed(max_attempts=1)
        before = events.current()
        engine.finish_failure(job, "boom")
        self.assertGreater(events.current(), before)


# ==========================================================================
# engine — reap, heartbeat, orphans
# ==========================================================================


class ReapTests(TestCase):
    def test_reap_reclaims_an_expired_lease(self):
        kind = register_test_kind(self)
        engine.enqueue(kind)
        job = engine.claim_next()
        Job.objects.filter(pk=job.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )

        result = engine.reap()

        self.assertEqual(result["reclaimed"], 1)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.QUEUED)
        self.assertIsNone(job.lease_expires_at)
        self.assertIsNone(job.started_at)
        self.assertIn("lease expired", job.error)
        # The wedge is now unstuck: the work can run again.
        self.assertIsNotNone(engine.claim_next())

    def test_reap_leaves_a_live_lease_alone(self):
        kind = register_test_kind(self)
        engine.enqueue(kind)
        job = engine.claim_next()

        result = engine.reap()

        self.assertEqual(result["reclaimed"], 0)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.RUNNING)
        self.assertIsNotNone(job.lease_expires_at)
        self.assertEqual(job.error, "")

    def test_reap_wakes_the_workers_after_reclaiming(self):
        kind = register_test_kind(self)
        engine.enqueue(kind)
        job = engine.claim_next()
        Job.objects.filter(pk=job.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        wake = threading.Event()
        engine.set_wake_event(wake)
        self.addCleanup(engine.set_wake_event, None)

        engine.reap()

        self.assertTrue(wake.is_set())

    @override_settings(JOB_RETENTION_DAYS=7)
    def test_reap_prunes_old_terminal_jobs_and_keeps_recent_ones(self):
        kind = register_test_kind(self)
        now = timezone.now()
        old, recent = [], []
        for state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
            old.append(
                Job.objects.create(
                    kind=kind, state=state, finished_at=now - timedelta(days=30)
                ).pk
            )
            recent.append(
                Job.objects.create(
                    kind=kind, state=state, finished_at=now - timedelta(days=1)
                ).pk
            )
        # Active rows are never pruned, however old they look.
        stale_queued = Job.objects.create(
            kind=kind, state=JobState.QUEUED, finished_at=now - timedelta(days=99)
        )

        result = engine.reap()

        self.assertEqual(result["pruned"], 3)
        self.assertFalse(Job.objects.filter(pk__in=old).exists())
        self.assertEqual(Job.objects.filter(pk__in=recent).count(), 3)
        self.assertTrue(Job.objects.filter(pk=stale_queued.pk).exists())

    @override_settings(JOB_RETENTION_DAYS=7)
    def test_reap_ignores_terminal_jobs_with_no_finished_at(self):
        kind = register_test_kind(self)
        job = Job.objects.create(kind=kind, state=JobState.SUCCEEDED, finished_at=None)
        self.assertEqual(engine.reap()["pruned"], 0)
        self.assertTrue(Job.objects.filter(pk=job.pk).exists())

    def test_reap_on_an_empty_table_reports_nothing(self):
        self.assertEqual(engine.reap(), {"reclaimed": 0, "pruned": 0})

    def test_reap_accepts_an_explicit_now(self):
        kind = register_test_kind(self)
        engine.enqueue(kind)
        engine.claim_next()
        # A "now" far in the future makes even a fresh lease look expired.
        result = engine.reap(now=timezone.now() + timedelta(days=1))
        self.assertEqual(result["reclaimed"], 1)


class HeartbeatTests(TestCase):
    @override_settings(JOB_LEASE_SECONDS=900.0)
    def test_heartbeat_extends_the_lease(self):
        kind = register_test_kind(self)
        engine.enqueue(kind)
        job = engine.claim_next()
        Job.objects.filter(pk=job.pk).update(
            lease_expires_at=timezone.now() + timedelta(seconds=5)
        )
        job.refresh_from_db()
        short_lease = job.lease_expires_at

        engine.heartbeat(job)

        job.refresh_from_db()
        self.assertGreater(job.lease_expires_at, short_lease)
        self.assertGreater(
            (job.lease_expires_at - timezone.now()).total_seconds(), 800
        )

    @override_settings(JOB_LEASE_SECONDS=900.0)
    def test_a_heartbeat_saves_a_job_from_the_reaper(self):
        kind = register_test_kind(self)
        engine.enqueue(kind)
        job = engine.claim_next()
        Job.objects.filter(pk=job.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        job.refresh_from_db()

        engine.heartbeat(job)

        self.assertEqual(engine.reap()["reclaimed"], 0)
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.RUNNING)

    def test_heartbeat_does_nothing_to_a_finished_job(self):
        kind = register_test_kind(self)
        engine.enqueue(kind)
        job = engine.claim_next()
        engine.finish_success(job, "done")

        engine.heartbeat(job)

        job.refresh_from_db()
        self.assertEqual(job.state, JobState.SUCCEEDED)
        self.assertIsNone(job.lease_expires_at)


class RequeueOrphansTests(TestCase):
    def test_running_rows_go_back_to_queued(self):
        kind = register_test_kind(self)
        now = timezone.now()
        running = [
            Job.objects.create(
                kind=kind,
                state=JobState.RUNNING,
                started_at=now,
                lease_expires_at=now + timedelta(seconds=900),
            )
            for _ in range(2)
        ]
        queued = Job.objects.create(kind=kind, state=JobState.QUEUED)
        done = Job.objects.create(
            kind=kind, state=JobState.SUCCEEDED, finished_at=now
        )

        self.assertEqual(engine.requeue_orphans(), 2)

        for job in running:
            job.refresh_from_db()
            self.assertEqual(job.state, JobState.QUEUED)
            self.assertIsNone(job.started_at)
            self.assertIsNone(job.lease_expires_at)
            self.assertIn("restart", job.error)

        queued.refresh_from_db()
        self.assertEqual(queued.state, JobState.QUEUED)
        self.assertEqual(queued.error, "")

        done.refresh_from_db()
        self.assertEqual(done.state, JobState.SUCCEEDED)

    def test_nothing_running_returns_zero(self):
        self.assertEqual(engine.requeue_orphans(), 0)

    def test_requeued_orphans_are_claimable_again(self):
        kind = register_test_kind(self)
        Job.objects.create(kind=kind, state=JobState.RUNNING, started_at=timezone.now())
        engine.requeue_orphans()
        self.assertIsNotNone(engine.claim_next())


# ==========================================================================
# engine — concurrency
# ==========================================================================


class ClaimConcurrencyTests(TransactionTestCase):
    """The claim is a single conditional UPDATE; nothing else guards it.

    Threads need committed rows to see each other's work, hence
    TransactionTestCase rather than TestCase.
    """

    @staticmethod
    def _with_retry(deadline, operation):
        """Run `operation`, retrying SQLite's contention errors.

        The test database is shared-cache in-memory SQLite, whose table-level
        locking surfaces "database table is locked" under write contention.
        That is a storage-layer artefact of the test harness, not a failed
        claim, so retrying keeps the test honest about what it is measuring:
        that a row can never transition out of QUEUED twice.
        """
        while True:
            try:
                return operation()
            except OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    @classmethod
    def _claim_with_retry(cls, deadline):
        return cls._with_retry(deadline, engine.claim_next)

    def _race(self, worker, thread_count):
        errors: list[str] = []
        ready = threading.Barrier(thread_count, timeout=PATIENCE)

        def run():
            try:
                ready.wait()
                worker()
            except BaseException as exc:  # reported on the main thread
                errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                connection.close()

        threads = [threading.Thread(target=run, daemon=True)
                   for _ in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=PATIENCE)
            self.assertFalse(thread.is_alive(), "a claiming thread never finished")
        self.assertEqual(errors, [])

    def test_every_job_is_claimed_exactly_once(self):
        kind = register_test_kind(self)
        job_count = 12
        thread_count = 4
        expected = {engine.enqueue(kind, {"i": i}).pk for i in range(job_count)}

        claimed: list[int] = []
        guard = threading.Lock()
        deadline = time.monotonic() + PATIENCE

        def worker():
            while time.monotonic() < deadline:
                with guard:
                    if len(claimed) >= job_count:
                        return
                job = self._claim_with_retry(deadline)
                if job is None:
                    time.sleep(0.005)
                    continue
                with guard:
                    claimed.append(job.pk)

        self._race(worker, thread_count)

        self.assertEqual(len(claimed), job_count, "not every job was claimed")
        self.assertEqual(
            len(claimed), len(set(claimed)), "a job was claimed more than once"
        )
        self.assertEqual(set(claimed), expected)
        self.assertEqual(
            Job.objects.filter(state=JobState.RUNNING).count(), job_count
        )
        # attempts==1 everywhere is the real proof: a second claim would have
        # incremented it, whatever the claiming thread then did with the row.
        self.assertEqual(Job.objects.filter(attempts=1).count(), job_count)
        self.assertEqual(Job.objects.exclude(attempts=1).count(), 0)

    def test_only_one_thread_wins_a_single_job(self):
        kind = register_test_kind(self)
        job = engine.enqueue(kind)
        thread_count = 8

        winners: list[int] = []
        guard = threading.Lock()
        deadline = time.monotonic() + PATIENCE

        def worker():
            claimed = self._claim_with_retry(deadline)
            if claimed is not None:
                with guard:
                    winners.append(claimed.pk)

        self._race(worker, thread_count)

        self.assertEqual(winners, [job.pk], "the job was claimed by more than one thread")
        job.refresh_from_db()
        self.assertEqual(job.state, JobState.RUNNING)
        self.assertEqual(job.attempts, 1)

    def test_concurrent_enqueues_of_one_dedup_key_produce_one_job(self):
        kind = register_test_kind(self)
        thread_count = 8
        results: list[int] = []
        guard = threading.Lock()
        deadline = time.monotonic() + PATIENCE

        def worker():
            job = self._with_retry(
                deadline, lambda: engine.enqueue(kind, dedup_key="track:7")
            )
            with guard:
                results.append(job.pk)

        self._race(worker, thread_count)

        self.assertEqual(len(results), thread_count)
        self.assertEqual(len(set(results)), 1, "the dedup index let a duplicate through")
        self.assertEqual(Job.objects.filter(dedup_key="track:7").count(), 1)
