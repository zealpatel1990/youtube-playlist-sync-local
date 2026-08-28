"""
yt-dlp upgrades: the one dependency the app maintains itself.

Everything else is pinned in requirements.txt and moves when a human decides.
yt-dlp is different — YouTube changes on its own schedule and a stale copy stops
downloading altogether — so it can be upgraded from the dashboard, or on a timer
via YTDLP_AUTO_UPDATE_HOURS.

That timer is what makes the tests here matter. An upgrade restarts the service,
so an unattended one that fires when there is nothing to install would bounce a
24/7 box (and kill whatever was mid-download) to achieve nothing.
"""

from __future__ import annotations

import subprocess
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from music.jobs.handlers import maintenance
from music.models import Job, JobState


def _job(**kwargs) -> Job:
    return Job.objects.create(kind="maintenance.update_ytdlp", **kwargs)


class UpgradeRestartTests(TestCase):
    """The restart decision, which is the whole risk in an unattended upgrade."""

    @override_settings(SYSTEMD_SERVICE="music_manager")
    def test_no_restart_when_the_version_did_not_change(self):
        job = _job()
        with mock.patch("music.ingest.youtube.ytdlp_version", return_value="2026.08.19"), \
             mock.patch("music.ingest.youtube.upgrade_ytdlp", return_value="2026.08.19"), \
             mock.patch.object(subprocess, "run") as run:
            message = maintenance.update_ytdlp(job)

        run.assert_not_called()
        self.assertIn("already", message)

    @override_settings(SYSTEMD_SERVICE="music_manager")
    def test_restart_when_the_version_moved(self):
        job = _job()
        with mock.patch("music.ingest.youtube.ytdlp_version", return_value="2026.08.19"), \
             mock.patch("music.ingest.youtube.upgrade_ytdlp", return_value="2026.09.01"), \
             mock.patch.object(subprocess, "run") as run:
            maintenance.update_ytdlp(job)

        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertIn("systemctl", " ".join(argv))
        # --no-block returns as soon as systemd accepts the job, so there is no
        # sleep to race the terminal write (A7).
        self.assertIn("--no-block", argv)
        self.assertIn("-n", argv)  # sudo -n: a missing sudoers rule fails loudly
        self.assertIs(run.call_args.kwargs["check"], True)
        self.assertIn("timeout", run.call_args.kwargs)

    @override_settings(SYSTEMD_SERVICE="music_manager")
    def test_terminal_state_is_written_before_the_restart(self):
        """A7 — the old code restarted first and lost its own SUCCESS write."""
        job = _job()
        seen = {}

        def capture(*args, **kwargs):
            seen["state"] = Job.objects.get(pk=job.pk).state
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("music.ingest.youtube.ytdlp_version", return_value="a"), \
             mock.patch("music.ingest.youtube.upgrade_ytdlp", return_value="b"), \
             mock.patch.object(subprocess, "run", side_effect=capture):
            maintenance.update_ytdlp(job)

        self.assertEqual(seen["state"], JobState.SUCCEEDED)

    @override_settings(SYSTEMD_SERVICE="")
    def test_no_systemd_service_means_no_restart_attempt(self):
        job = _job()
        with mock.patch("music.ingest.youtube.ytdlp_version", return_value="a"), \
             mock.patch("music.ingest.youtube.upgrade_ytdlp", return_value="b"), \
             mock.patch.object(subprocess, "run") as run:
            message = maintenance.update_ytdlp(job)

        run.assert_not_called()
        self.assertIn("not restarting", message)

    @override_settings(SYSTEMD_SERVICE="music_manager")
    def test_a_refused_restart_is_reported_not_swallowed(self):
        """Without the sudoers rule the old code reported success regardless."""
        job = _job()
        error = subprocess.CalledProcessError(1, ["sudo"], stderr="a password is required")
        with mock.patch("music.ingest.youtube.ytdlp_version", return_value="a"), \
             mock.patch("music.ingest.youtube.upgrade_ytdlp", return_value="b"), \
             mock.patch.object(subprocess, "run", side_effect=error):
            message = maintenance.update_ytdlp(job)

        self.assertIn("refused", message)


class ScheduledUpgradeTests(TestCase):
    """The timer only queues the job; these are its guard rails."""

    def _run_task(self) -> None:
        from music.jobs import scheduled

        scheduled._update_ytdlp()

    def test_it_queues_an_upgrade_when_the_queue_is_quiet(self):
        self._run_task()
        self.assertTrue(
            Job.objects.filter(kind="maintenance.update_ytdlp").exists()
        )

    def test_it_defers_while_a_download_is_running(self):
        # Restarting mid-download kills the transfer and leaves a partial file.
        Job.objects.create(
            kind="youtube.download", state=JobState.RUNNING,
            started_at=timezone.now(),
        )
        self._run_task()
        self.assertFalse(
            Job.objects.filter(kind="maintenance.update_ytdlp").exists()
        )

    def test_it_defers_while_moves_are_being_applied(self):
        Job.objects.create(kind="organize.apply_all", state=JobState.QUEUED)
        self._run_task()
        self.assertFalse(
            Job.objects.filter(kind="maintenance.update_ytdlp").exists()
        )

    def test_a_finished_download_does_not_block_it(self):
        Job.objects.create(
            kind="youtube.download", state=JobState.SUCCEEDED,
            finished_at=timezone.now(),
        )
        self._run_task()
        self.assertTrue(
            Job.objects.filter(kind="maintenance.update_ytdlp").exists()
        )

    def test_repeated_ticks_do_not_stack_upgrades(self):
        self._run_task()
        self._run_task()
        self.assertEqual(
            Job.objects.filter(kind="maintenance.update_ytdlp").count(), 1
        )

    @override_settings(YTDLP_AUTO_UPDATE_HOURS=0)
    def test_zero_hours_disables_the_schedule(self):
        from music.jobs import scheduled

        task = next(t for t in scheduled.tasks() if t.name == "update_ytdlp")
        # The worker loop skips any task whose interval is not positive.
        self.assertEqual(task.interval_seconds, 0)

    @override_settings(YTDLP_AUTO_UPDATE_HOURS=168)
    def test_hours_are_converted_to_seconds(self):
        from music.jobs import scheduled

        task = next(t for t in scheduled.tasks() if t.name == "update_ytdlp")
        self.assertEqual(task.interval_seconds, 168 * 3600)
