"""
YouTube ingestion, with every yt-dlp and subprocess boundary mocked.

Nothing here touches the network, spawns a child process, or transcodes: the
whole module goes through `music.ingest.youtube._ydl` and `subprocess.run`, so
patching those two names is enough.

The cases are chosen to pin down the audit findings this module exists to fix
rather than to chase coverage — availability classification (A13), a None
duration (A14), the absent `force_generic_extractor` (A15), the guessed output
path, and the untimed pip call (A6).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from music.ingest import youtube
from music.models import Availability, Track, YoutubeVideo


class FakeYDL:
    """Stands in for a `yt_dlp.YoutubeDL` context manager.

    Records the options it was constructed with, so tests can assert on what
    the module asked yt-dlp to do — which is where the A15 and quiet/timeout
    requirements actually live.
    """

    def __init__(self, opts: dict, info, on_extract=None):
        self.opts = opts
        self._info = info
        self._on_extract = on_extract
        self.extract_calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        self.extract_calls.append((url, download))
        if self._on_extract is not None:
            self._on_extract(self)
        return self._info


def patch_ydl(info, on_extract=None):
    """Patch the single yt-dlp seam; the factory records the last instance."""
    holder: dict = {}

    def factory(opts):
        holder["ydl"] = FakeYDL(opts, info, on_extract)
        return holder["ydl"]

    patcher = mock.patch.object(youtube, "_ydl", side_effect=factory)
    return patcher, holder


PLAYLIST = {
    "entries": [
        {
            "id": "vid_public0",
            "title": "Available Song",
            "uploader": "Some Artist",
            "duration": 212,
            "availability": "public",
        },
        {
            # The real-world string: lowercase 'v'. The previous version
            # compared against '[Private Video]' and matched nothing (A13).
            "id": "vid_private",
            "title": "[Private video]",
            "uploader": None,
            "duration": None,
        },
        {
            "id": "vid_deleted",
            "title": "[Deleted video]",
            "uploader": None,
            "duration": None,
        },
        {
            "id": "vid_unlistd",
            "title": "Unlisted Song",
            "duration": 190.7,
            "availability": "unlisted",
        },
        None,  # yt-dlp yields None for entries it could not read at all
    ]
}


class AvailabilityTests(SimpleTestCase):
    """A13 — classify from the structured field, fall back case-insensitively."""

    def test_structured_field_wins(self):
        self.assertEqual(
            youtube.classify_availability({"availability": "public", "title": "x"}),
            Availability.AVAILABLE,
        )
        self.assertEqual(
            youtube.classify_availability({"availability": "private", "title": "x"}),
            Availability.PRIVATE,
        )
        self.assertEqual(
            youtube.classify_availability({"availability": "unlisted", "title": "x"}),
            Availability.AVAILABLE,
        )
        self.assertEqual(
            youtube.classify_availability({"availability": "needs_auth", "title": "x"}),
            Availability.PRIVATE,
        )

    def test_title_fallback_is_case_insensitive(self):
        for title in ("[Private video]", "[Private Video]", "[PRIVATE VIDEO]"):
            with self.subTest(title=title):
                self.assertEqual(
                    youtube.classify_availability({"title": title}),
                    Availability.PRIVATE,
                )
        for title in ("[Deleted video]", "[deleted video]"):
            with self.subTest(title=title):
                self.assertEqual(
                    youtube.classify_availability({"title": title}),
                    Availability.DELETED,
                )

    def test_structured_field_beats_a_misleading_title(self):
        entry = {"availability": "public", "title": "[Private video]"}
        self.assertEqual(youtube.classify_availability(entry), Availability.AVAILABLE)

    def test_missing_duration_alone_does_not_mean_unavailable(self):
        """A live stream reports no duration and is perfectly downloadable.

        The old heuristic filed it as UNAVAILABLE, which meant it was never
        even attempted.
        """
        entry = {"id": "x", "title": "Some Live Set", "duration": None}
        self.assertEqual(youtube.classify_availability(entry), Availability.AVAILABLE)

    def test_empty_entry_is_unavailable(self):
        self.assertEqual(youtube.classify_availability({}), Availability.UNAVAILABLE)


class ListPlaylistTests(SimpleTestCase):
    def test_entries_are_normalized(self):
        patcher, holder = patch_ydl(PLAYLIST)
        with patcher:
            entries = youtube.list_playlist("https://youtube.com/playlist?list=x")

        self.assertEqual([e.video_id for e in entries],
                         ["vid_public0", "vid_private", "vid_deleted", "vid_unlistd"])
        by_id = {e.video_id: e for e in entries}
        self.assertEqual(by_id["vid_public0"].availability, Availability.AVAILABLE)
        self.assertEqual(by_id["vid_private"].availability, Availability.PRIVATE)
        self.assertEqual(by_id["vid_deleted"].availability, Availability.DELETED)
        self.assertEqual(by_id["vid_unlistd"].availability, Availability.AVAILABLE)

    def test_none_duration_becomes_zero_never_none(self):
        """A14 — a None duration reaching a `<` comparison raised TypeError."""
        patcher, _ = patch_ydl(PLAYLIST)
        with patcher:
            entries = youtube.list_playlist("https://youtube.com/playlist?list=x")

        durations = {e.video_id: e.duration for e in entries}
        self.assertEqual(durations["vid_private"], 0)
        self.assertEqual(durations["vid_deleted"], 0)
        for value in durations.values():
            self.assertIsInstance(value, int)
        # A float duration is truncated, not carried through as a float.
        self.assertEqual(durations["vid_unlistd"], 190)

    def test_uploader_and_url_are_filled_in(self):
        patcher, _ = patch_ydl(PLAYLIST)
        with patcher:
            entries = youtube.list_playlist("https://youtube.com/playlist?list=x")

        by_id = {e.video_id: e for e in entries}
        self.assertEqual(by_id["vid_public0"].uploader, "Some Artist")
        self.assertEqual(by_id["vid_private"].uploader, "")  # never None
        self.assertEqual(
            by_id["vid_private"].url,
            "https://www.youtube.com/watch?v=vid_private",
        )

    def test_options_drop_the_deprecated_flag_and_set_a_timeout(self):
        """A15 — no `force_generic_extractor`; A6 — an explicit socket timeout."""
        patcher, holder = patch_ydl(PLAYLIST)
        with patcher:
            youtube.list_playlist("https://youtube.com/playlist?list=x")

        opts = holder["ydl"].opts
        self.assertNotIn("force_generic_extractor", opts)
        self.assertEqual(opts["extract_flat"], "in_playlist")
        self.assertEqual(opts["socket_timeout"], youtube.SOCKET_TIMEOUT)
        self.assertTrue(opts["quiet"])
        self.assertEqual(holder["ydl"].extract_calls, [
            ("https://youtube.com/playlist?list=x", False)
        ])

    def test_errors_are_not_swallowed(self):
        """The old service turned "YouTube blocked us" into "empty playlist"."""
        with mock.patch.object(youtube, "_ydl", side_effect=RuntimeError("blocked")):
            with self.assertRaises(RuntimeError):
                youtube.list_playlist("https://youtube.com/playlist?list=x")

    def test_empty_url_raises(self):
        with self.assertRaises(ValueError):
            youtube.list_playlist("")


class SyncPlaylistTests(TestCase):
    def _sync(self, info=PLAYLIST):
        patcher, _ = patch_ydl(info)
        with patcher:
            return youtube.sync_playlist("https://youtube.com/playlist?list=x")

    def test_first_run_creates_rows(self):
        counts = self._sync()
        self.assertEqual(counts, {"seen": 4, "added": 4, "updated": 0})
        self.assertEqual(YoutubeVideo.objects.count(), 4)
        self.assertEqual(
            YoutubeVideo.objects.get(pk="vid_private").availability,
            Availability.PRIVATE,
        )
        self.assertEqual(YoutubeVideo.objects.get(pk="vid_private").duration, 0)

    def test_second_run_is_idempotent(self):
        self._sync()
        counts = self._sync()

        self.assertEqual(YoutubeVideo.objects.count(), 4)
        self.assertEqual(counts["added"], 0)
        # Nothing about the playlist changed, so nothing was rewritten.
        self.assertEqual(counts["updated"], 0)
        self.assertEqual(counts["seen"], 4)

    def test_last_seen_is_refreshed_even_when_nothing_changed(self):
        """Freshness is stamped by the bulk update, not by saving each row."""
        self._sync()
        YoutubeVideo.objects.all().update(last_seen_at=None)

        self._sync()

        self.assertEqual(
            YoutubeVideo.objects.filter(last_seen_at__isnull=True).count(), 0
        )

    def test_changed_title_counts_as_an_update(self):
        self._sync()
        changed = {"entries": [dict(PLAYLIST["entries"][0], title="Renamed Song")]}
        counts = self._sync(changed)

        self.assertEqual(counts, {"seen": 1, "added": 0, "updated": 1})
        self.assertEqual(
            YoutubeVideo.objects.get(pk="vid_public0").title, "Renamed Song"
        )

    def test_a_video_listed_twice_does_not_duplicate(self):
        entry = PLAYLIST["entries"][0]
        counts = self._sync({"entries": [entry, dict(entry)]})
        self.assertEqual(counts["seen"], 1)
        self.assertEqual(YoutubeVideo.objects.count(), 1)


@override_settings(AUDIO_QUALITY="192", FFMPEG_LOCATION="")
class DownloadTests(TestCase):
    def setUp(self):
        import tempfile

        self.dest = Path(tempfile.mkdtemp())
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.dest, ignore_errors=True)
        )
        self.video = YoutubeVideo.objects.create(
            video_id="vid_public0",
            title="Available Song",
            url="https://www.youtube.com/watch?v=vid_public0",
        )

    def _info_for(self, filename: str) -> dict:
        """yt-dlp's report, naming a file that is not the naive `<id>.mp3` guess."""
        written = self.dest / filename
        written.write_bytes(b"audio")
        return {
            "id": self.video.video_id,
            "requested_downloads": [{"filepath": str(written)}],
        }

    def test_returns_the_path_yt_dlp_reports_not_a_guessed_one(self):
        info = self._info_for("vid_public0 [remastered].mp3")
        patcher, _ = patch_ydl(info)
        with patcher:
            result = youtube.download_audio(self.video, self.dest)

        self.assertEqual(result, self.dest / "vid_public0 [remastered].mp3")
        self.assertNotEqual(result, self.dest / "vid_public0.mp3")
        self.assertTrue(result.exists())

    def test_a_different_extension_is_honoured(self):
        info = self._info_for("vid_public0.opus")
        patcher, _ = patch_ydl(info)
        with patcher:
            result = youtube.download_audio(self.video, self.dest)
        self.assertEqual(result.suffix, ".opus")

    def test_falls_back_to_the_directory_when_yt_dlp_reports_nothing(self):
        (self.dest / "vid_public0.mp3").write_bytes(b"audio")
        (self.dest / "vid_public0.mp3.part").write_bytes(b"partial")
        patcher, _ = patch_ydl({"id": "vid_public0"})
        with patcher:
            result = youtube.download_audio(self.video, self.dest)

        self.assertEqual(result, self.dest / "vid_public0.mp3")

    def test_missing_output_raises(self):
        patcher, _ = patch_ydl({"id": "vid_public0"})
        with patcher, self.assertRaises(FileNotFoundError):
            youtube.download_audio(self.video, self.dest)

    def test_options_are_quiet_and_timeout_bounded(self):
        info = self._info_for("vid_public0.mp3")
        patcher, holder = patch_ydl(info)
        with patcher:
            youtube.download_audio(self.video, self.dest)

        opts = holder["ydl"].opts
        # The old downloader set quiet=False and streamed the whole progress
        # bar into journald — thousands of SD-card writes per track.
        self.assertTrue(opts["quiet"])
        self.assertEqual(opts["socket_timeout"], youtube.SOCKET_TIMEOUT)
        self.assertNotIn("ffmpeg_location", opts)
        self.assertEqual(holder["ydl"].extract_calls, [(self.video.url, True)])

    def test_native_format_runs_no_postprocessor(self):
        """The default must not transcode.

        Re-encoding to MP3 is the most expensive step on a Pi — libmp3lame is
        effectively single-threaded, and on a 900MHz core a four-minute track
        can take longer to encode than to fetch. It is also a second lossy pass
        over YouTube's already-lossy Opus.
        """
        info = self._info_for("vid_public0.mp3")
        patcher, holder = patch_ydl(info)
        with patcher:
            youtube.download_audio(self.video, self.dest)
        self.assertEqual(holder["ydl"].opts["postprocessors"], [])

    @override_settings(AUDIO_FORMAT="mp3", AUDIO_QUALITY="192")
    def test_mp3_format_adds_the_extract_audio_postprocessor(self):
        info = self._info_for("vid_public0.mp3")
        patcher, holder = patch_ydl(info)
        with patcher:
            youtube.download_audio(self.video, self.dest)
        post = holder["ydl"].opts["postprocessors"]
        self.assertEqual(len(post), 1)
        self.assertEqual(post[0]["key"], "FFmpegExtractAudio")
        self.assertEqual(post[0]["preferredcodec"], "mp3")
        self.assertEqual(post[0]["preferredquality"], "192")

    def test_fragments_are_fetched_concurrently(self):
        info = self._info_for("vid_public0.mp3")
        patcher, holder = patch_ydl(info)
        with patcher:
            youtube.download_audio(self.video, self.dest)
        self.assertEqual(
            holder["ydl"].opts["concurrent_fragment_downloads"],
            settings.DOWNLOAD_CONCURRENT_FRAGMENTS,
        )

    @override_settings(FFMPEG_LOCATION="/usr/bin")
    def test_ffmpeg_location_is_passed_when_set(self):
        info = self._info_for("vid_public0.mp3")
        patcher, holder = patch_ydl(info)
        with patcher:
            youtube.download_audio(self.video, self.dest)
        self.assertEqual(holder["ydl"].opts["ffmpeg_location"], "/usr/bin")

    def test_heartbeat_is_throttled(self):
        beats = mock.Mock()

        def hammer_the_hook(ydl):
            for _ in range(50):
                ydl.opts["progress_hooks"][0]({"status": "downloading"})

        info = self._info_for("vid_public0.mp3")
        patcher, _ = patch_ydl(info, on_extract=hammer_the_hook)
        with patcher:
            youtube.download_audio(self.video, self.dest, heartbeat=beats)

        # 50 hook calls inside one interval must not become 50 database writes.
        self.assertEqual(beats.call_count, 1)

    def test_heartbeat_fires_again_after_the_interval(self):
        beats = mock.Mock()

        def tick_twice(ydl):
            for hook in (ydl.opts["progress_hooks"][0], ydl.opts["postprocessor_hooks"][0]):
                hook({"status": "downloading"})

        info = self._info_for("vid_public0.mp3")
        patcher, _ = patch_ydl(info, on_extract=tick_twice)
        with patcher, mock.patch.object(youtube, "HEARTBEAT_INTERVAL", 0.0):
            youtube.download_audio(self.video, self.dest, heartbeat=beats)

        self.assertEqual(beats.call_count, 2)

    def test_a_failing_heartbeat_does_not_abort_the_download(self):
        beats = mock.Mock(side_effect=RuntimeError("lease write failed"))

        def tick(ydl):
            ydl.opts["progress_hooks"][0]({"status": "downloading"})

        info = self._info_for("vid_public0.mp3")
        patcher, _ = patch_ydl(info, on_extract=tick)
        with patcher, self.assertLogs("music.ingest", "ERROR"):
            result = youtube.download_audio(self.video, self.dest, heartbeat=beats)

        self.assertTrue(result.exists())
        beats.assert_called_once()


@override_settings(PIP_PATH="/venv/bin/pip", YTDLP_PATH="/venv/bin/yt-dlp")
class UpgradeTests(SimpleTestCase):
    def test_pip_call_has_an_explicit_timeout(self):
        """A6 — the previous version's pip subprocess had no timeout at all."""
        runs = [
            mock.Mock(stdout="Successfully installed yt-dlp-2026.08.19\n", stderr=""),
            mock.Mock(stdout="2026.08.19\n", stderr=""),
        ]
        with mock.patch.object(subprocess, "run", side_effect=runs) as run:
            version = youtube.upgrade_ytdlp()

        install = run.call_args_list[0]
        self.assertEqual(
            install.args[0],
            [
                "/venv/bin/pip", "install", "--upgrade",
                "--no-cache-dir", "--disable-pip-version-check", "yt-dlp",
            ],
        )
        self.assertEqual(install.kwargs["timeout"], youtube.PIP_TIMEOUT)
        self.assertIs(install.kwargs["check"], True)
        self.assertNotIn("shell", install.kwargs)
        self.assertEqual(version, "2026.08.19")

    def test_every_subprocess_call_is_timed(self):
        runs = [
            mock.Mock(stdout="Successfully installed yt-dlp-2026.08.19\n", stderr=""),
            mock.Mock(stdout="2026.08.19\n", stderr=""),
        ]
        with mock.patch.object(subprocess, "run", side_effect=runs) as run:
            youtube.upgrade_ytdlp()

        for call in run.call_args_list:
            self.assertIn("timeout", call.kwargs)
            self.assertGreater(call.kwargs["timeout"], 0)

    def test_it_does_not_restart_the_service(self):
        """A7 — restarting from inside the job kills its own SUCCESS write."""
        runs = [
            mock.Mock(stdout="Successfully installed yt-dlp-2026.08.19\n", stderr=""),
            mock.Mock(stdout="2026.08.19\n", stderr=""),
        ]
        with mock.patch.object(subprocess, "run", side_effect=runs) as run:
            youtube.upgrade_ytdlp()

        for call in run.call_args_list:
            argv = " ".join(call.args[0])
            self.assertNotIn("systemctl", argv)
            self.assertNotIn("sudo", argv)

    def test_a_failed_upgrade_propagates(self):
        error = subprocess.CalledProcessError(1, ["pip"], stderr="no wheel")
        with mock.patch.object(subprocess, "run", side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError):
                youtube.upgrade_ytdlp()

    def test_version_falls_back_to_pip_output_when_the_binary_is_absent(self):
        runs = [
            mock.Mock(stdout="Successfully installed yt-dlp-2026.08.19\n", stderr=""),
            FileNotFoundError("/venv/bin/yt-dlp"),
        ]
        with mock.patch.object(subprocess, "run", side_effect=runs):
            self.assertEqual(youtube.upgrade_ytdlp(), "2026.08.19")

    def test_ytdlp_version_reports_the_imported_module(self):
        fake = mock.Mock()
        fake.version.__version__ = "2026.01.01"
        with mock.patch.object(youtube, "_ytdlp", return_value=fake):
            self.assertEqual(youtube.ytdlp_version(), "2026.01.01")


class PendingDownloadsTests(TestCase):
    def test_only_available_videos_without_a_track(self):
        available = YoutubeVideo.objects.create(
            video_id="available01", availability=Availability.AVAILABLE
        )
        YoutubeVideo.objects.create(
            video_id="private0001", availability=Availability.PRIVATE
        )
        track = Track.objects.create(path="/music/done.mp3")
        YoutubeVideo.objects.create(
            video_id="done0000001",
            availability=Availability.AVAILABLE,
            track=track,
        )

        self.assertEqual(
            [v.pk for v in youtube.pending_downloads()], [available.pk]
        )

    def test_a_null_retry_at_is_still_eligible(self):
        """A4 — filtering on `retry_at <= now` alone orphaned these forever."""
        YoutubeVideo.objects.create(
            video_id="nullretry01", availability=Availability.AVAILABLE, retry_at=None
        )
        self.assertEqual(youtube.pending_downloads().count(), 1)

    def test_a_future_retry_at_is_held_back(self):
        from datetime import timedelta

        from django.utils import timezone

        YoutubeVideo.objects.create(
            video_id="backoff0001",
            availability=Availability.AVAILABLE,
            retry_at=timezone.now() + timedelta(hours=1),
        )
        self.assertEqual(youtube.pending_downloads().count(), 0)
