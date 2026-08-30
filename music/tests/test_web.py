"""
The HTTP surface: dashboard, fragments, SSE, actions, review, settings.

Three of these classes exist because of a specific audit finding, and are worth
keeping even if the rest of the file is trimmed:

* `SseStreamTests` — the stream must self-close and must not query per tick (A2).
* `ToastMarkupTests` — the toast must be built with textContent (A11).
* `SettingsValidationTests` — 0 and negative values must be rejected per field,
  not waved through by a blanket `float()` (A9).

Everything runs offline. No handler package needs to exist: `setUpModule`
registers a no-op job spec for any kind the views enqueue that the real handlers
have not claimed yet, so this file tests the web layer and only the web layer.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
import threading
import types
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from music import views
from music.core import envfile, events
from music.jobs import registry
from music.templatetags import music_extras
from music.models import Job, JobState, Track, TrackState, YoutubeVideo

#: kind -> the view that enqueues it. Kept explicit so a renamed job kind fails
#: here rather than silently 503-ing in the browser.
ACTION_KINDS = {
    "library.scan_all": "action_scan",
    "organize.plan_all": "action_plan",
    "organize.apply_all": "action_apply",
    "identify.track": "action_identify_track",
    "organize.track": "action_organize_track",
    "organize.revert": "action_revert_track",
    "youtube.sync": "action_sync_youtube",
    "youtube.download": "action_download_video",
    "maintenance.update_ytdlp": "action_update_ytdlp",
}

APP_JS = Path(__file__).resolve().parent.parent / "static" / "music" / "app.js"


def setUpModule() -> None:
    """Make every job kind the views use enqueueable.

    `engine.enqueue` refuses an unregistered kind, and the handler packages are
    imported by the worker bootstrap, which does not run under the test runner.
    Real handlers win when they are present; the stubs only fill the gaps.
    """
    quiet = logging.getLogger("music.jobs")
    previous = quiet.level
    quiet.setLevel(logging.CRITICAL)  # load_handlers logs a traceback per gap
    try:
        registry.load_handlers()
    finally:
        quiet.setLevel(previous)

    for kind in ACTION_KINDS:
        if registry.get(kind) is None:
            registry.job(kind, description="test stub")(lambda job: "")


def make_track(**kwargs) -> Track:
    defaults = {
        "path": "/music/incoming/song.mp3",
        "title": "Song",
        "artist": "Artist",
        "album": "Album",
        "duration": 225,
        "bitrate": 192,
        "state": TrackState.IDENTIFIED,
    }
    return Track.objects.create(**{**defaults, **kwargs})


# --------------------------------------------------------------------------
# Pages and fragments
# --------------------------------------------------------------------------


class DashboardTests(TestCase):
    def setUp(self):
        make_track()
        make_track(path="/music/incoming/second.mp3", title="Second",
                   state=TrackState.FAILED, last_error="boom", fail_count=2)

    def test_dashboard_renders(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "music/dashboard.html")
        self.assertContains(response, "Song")

    def test_state_counts_are_shown(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Identified: 1")
        self.assertContains(response, "Failed: 1")

    def test_search_matches_title_artist_and_album(self):
        for term in ("Song", "Artist", "Album"):
            with self.subTest(term=term):
                response = self.client.get(reverse("dashboard"), {"q": term})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Song")

        response = self.client.get(reverse("dashboard"), {"q": "no-such-track"})
        self.assertNotContains(response, "/music/incoming/song.mp3")

    def test_sort_is_whitelisted(self):
        # A value outside the whitelist must never reach order_by().
        for value in ("title", "-album", "state", "evil'; DROP TABLE", "path", ""):
            with self.subTest(sort=value):
                response = self.client.get(reverse("dashboard"), {"sort": value})
                self.assertEqual(response.status_code, 200)

    def test_out_of_range_pages_do_not_error(self):
        for page in ("2", "0", "not-a-number", "-1"):
            with self.subTest(page=page):
                self.assertEqual(
                    self.client.get(reverse("dashboard"), {"page": page}).status_code,
                    200,
                )


class StateFilterTests(TestCase):
    """`?state=` must honour every state the stats panel links to.

    `_stats.html` renders a badge per `TrackState` linking to `?state=<value>`,
    while the filter pills show a curated subset. When the validator accepted
    only the pill values, the badges for the states without one — SKIPPED,
    DISCOVERED, IDENTIFYING — silently fell back to "All": clicking
    "Skipped: 6" returned the whole library.
    """

    def setUp(self):
        # One track per state, so a filter returning the wrong set is visible.
        for index, state in enumerate(TrackState.values):
            make_track(path=f"/music/s{index}.mp3", title=f"T{index}", state=state)

    def test_every_state_is_accepted_by_the_filter(self):
        for state in TrackState.values:
            with self.subTest(state=state):
                self.assertIn(state, views.VALID_STATE_FILTERS)

    def test_every_state_filters_to_exactly_its_own_track(self):
        for state in TrackState.values:
            with self.subTest(state=state):
                response = self.client.get(reverse("dashboard"), {"state": state})
                self.assertEqual(response.status_code, 200)
                tracks = response.context["tracks"]
                self.assertEqual(
                    [t.state for t in tracks], [state],
                    f"?state={state} did not filter to that state alone",
                )

    def test_skipped_is_offered_as_a_pill(self):
        response = self.client.get(reverse("dashboard"))
        values = [pill["value"] for pill in response.context["state_filters"]]
        self.assertIn(TrackState.SKIPPED, values)

    def test_an_unknown_state_falls_back_to_all_rather_than_erroring(self):
        response = self.client.get(reverse("dashboard"), {"state": "nonsense"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["state"], "")
        self.assertEqual(
            len(response.context["tracks"]), len(TrackState.values)
        )


class FragmentTests(TestCase):
    def setUp(self):
        make_track()

    def test_fragments_return_200(self):
        for name in ("fragment_tracks", "fragment_stats", "fragment_jobs"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)

    def test_track_fragment_does_not_query_per_row(self):
        """The `.only()` list must cover every column the row template reads.

        A deferred field touched in `_track_row.html` costs one query per row;
        with the default page size that turns one SELECT into fifty on a Pi.
        """
        make_track(path="/music/incoming/b.mp3", planned_path="/music/lib/b.mp3")
        make_track(path="/music/incoming/c.mp3", previous_path="/old/c.mp3")

        # One COUNT for the paginator, one SELECT for the page. Nothing more,
        # regardless of how many rows that page holds.
        with self.assertNumQueries(2):
            response = self.client.get(reverse("fragment_tracks"))
        self.assertEqual(response.status_code, 200)


class ReviewTests(TestCase):
    def test_manifest_shows_source_and_destination(self):
        make_track(
            path="/music/incoming/song.mp3",
            planned_path="/music/library/Artist/Album/01 - Song.mp3",
            plan_note="from AcoustID",
        )
        response = self.client.get(reverse("library_review"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "music/review.html")
        self.assertContains(response, "song.mp3")
        self.assertContains(response, "01 - Song.mp3")
        self.assertContains(response, "from AcoustID")

    def test_destination_is_shown_relative_to_the_library_root(self):
        """What changes is the part worth reading; the root is noise on every row."""
        root = str(Path("/music/library"))
        make_track(
            path="/music/incoming/song.mp3",
            planned_path=str(Path("/music/library/Artist/Album/01 - Song.mp3")),
        )
        with override_settings(LIBRARY_ROOT=root):
            response = self.client.get(reverse("library_review"))
        self.assertContains(response, "&lt;library&gt;")
        self.assertContains(response, str(Path("Artist/Album")))

    def test_manifest_does_not_query_per_row(self):
        """Same `.only()` hazard as the library table, same guard."""
        for index in range(3):
            make_track(
                path=f"/music/incoming/{index}.mp3",
                planned_path=f"/music/library/A/B/{index}.mp3",
            )
        with self.assertNumQueries(2):
            self.client.get(reverse("library_review"))

    def test_tracks_already_in_place_are_not_listed(self):
        # planned_path == path is a no-op, not a move.
        make_track(path="/music/library/x.mp3", planned_path="/music/library/x.mp3")
        make_track(path="/music/library/y.mp3", title="Y")  # no plan at all
        response = self.client.get(reverse("library_review"))
        self.assertContains(response, "No moves are planned")


ORGANIZER = "music.library.organizer"


class DuplicatesTests(TestCase):
    def _replace_organizer(self, module) -> None:
        original = sys.modules.get(ORGANIZER)
        sys.modules[ORGANIZER] = module
        self.addCleanup(self._restore_organizer, original)

    @staticmethod
    def _restore_organizer(original) -> None:
        if original is None:
            sys.modules.pop(ORGANIZER, None)
        else:
            sys.modules[ORGANIZER] = original

    def _install_fake_organizer(self, find_duplicates) -> None:
        module = types.ModuleType(ORGANIZER)
        module.find_duplicates = find_duplicates
        self._replace_organizer(module)

    def test_real_detector_renders_an_empty_state_when_nothing_matches(self):
        make_track()
        response = self.client.get(reverse("duplicates"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "music/duplicates.html")
        self.assertContains(response, "No duplicates found")

    def test_an_unimportable_detector_renders_an_empty_state(self):
        """The page is in the navbar, so a broken import must not 500 it.

        `None` in `sys.modules` is the standard way to make an import fail — it
        is what the machinery itself leaves behind for a failed import.
        """
        self._replace_organizer(None)
        response = self.client.get(reverse("duplicates"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not available in this build")

    def test_groups_render_whether_they_are_sequences_or_mappings(self):
        first = make_track(path="/a/song.mp3", bitrate=320)
        second = make_track(path="/b/song.mp3", bitrate=128)
        for shape in ([[first, second]], [{"tracks": [first, second], "reason": "same hash"}]):
            with self.subTest(shape=type(shape[0]).__name__):
                self._install_fake_organizer(lambda: shape)
                response = self.client.get(reverse("duplicates"))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "/a/song.mp3")
                self.assertContains(response, "/b/song.mp3")
                self.assertContains(response, "320 kbps")

    def test_a_failing_detector_is_reported_not_raised(self):
        def explode():
            raise RuntimeError("no")

        self._install_fake_organizer(explode)
        with self.assertLogs("music.web", level="ERROR"):
            response = self.client.get(reverse("duplicates"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Duplicate detection failed")


class UrlMountTests(TestCase):
    def test_urlconf_is_mounted_once(self):
        """The old project included its urlconf at both '' and 'api/', so every
        `reverse()` resolved to the `api/` copy."""
        self.assertEqual(reverse("dashboard"), "/")
        self.assertEqual(reverse("stream_events"), "/events/")


# --------------------------------------------------------------------------
# SSE  (docs/CODE-AUDIT.md A2)
# --------------------------------------------------------------------------


class SseStreamTests(TestCase):
    def setUp(self):
        events.reset_for_tests()

    def open_stream(self):
        """Open `/events/` and guarantee the view's generator is closed after.

        `response.streaming_content` builds a fresh `map()` on every access and
        a map has no `close()`. The test client's wrapper does have one — and
        closing it is what actually unblocks the view's `wait_for_change` — but
        it is reachable only as `response._iterator`. Going through the public
        `response.close()` instead would fire `request_finished` with
        `close_old_connections` still attached, which drops the connection out
        from under this test's own transaction.
        """
        response = self.client.get(reverse("stream_events"))
        self.addCleanup(response._iterator.close)
        return response, response.streaming_content

    def test_stream_headers_and_retry_directive(self):
        response, stream = self.open_stream()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")
        self.assertEqual(response["Cache-Control"], "no-cache")
        self.assertEqual(response["X-Accel-Buffering"], "no")
        self.assertIn(b"retry:", next(stream))

    @override_settings(SSE_KEEPALIVE_SECONDS=0.05, SSE_MAX_STREAM_SECONDS=30)
    def test_idle_stream_emits_keepalive_without_querying(self):
        _, stream = self.open_stream()
        next(stream)  # retry:
        with self.assertNumQueries(0):  # the whole point of the A2 fix
            chunk = next(stream)
        self.assertEqual(chunk, b": keepalive\n\n")

    @override_settings(SSE_KEEPALIVE_SECONDS=10, SSE_MAX_STREAM_SECONDS=30)
    def test_bump_wakes_the_stream_with_the_changed_topics(self):
        _, stream = self.open_stream()
        next(stream)  # retry:
        timer = threading.Timer(0.05, lambda: events.bump("tracks"))
        self.addCleanup(timer.cancel)
        timer.start()

        chunk = next(stream)
        self.assertIn(b"event: update", chunk)
        self.assertIn(b"tracks", chunk)

    @override_settings(SSE_KEEPALIVE_SECONDS=0.01, SSE_MAX_STREAM_SECONDS=0.05)
    def test_stream_closes_itself_so_threads_recycle(self):
        """The A2 thread fix: a stream must never hold its worker indefinitely."""
        _, stream = self.open_stream()
        chunks = []
        for _ in range(20):
            try:
                chunks.append(next(stream))
            except StopIteration:
                break
        else:
            self.fail("the stream did not close itself")

        self.assertIn(b"retry:", chunks[0])
        self.assertIn(b"event: bye", chunks[-1])


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


class ActionTests(TestCase):
    def setUp(self):
        self.track = make_track()
        self.video = YoutubeVideo.objects.create(
            video_id="abc123", title="A Video", url="https://youtu.be/abc123"
        )

    def _post(self, name, *args):
        return self.client.post(reverse(name, args=args))

    def assert_queued(self, response, kind):
        self.assertEqual(response.status_code, 204)
        self.assertIn("HX-Trigger", response)
        self.assertIn("notify", json.loads(response["HX-Trigger"]))
        self.assertTrue(
            Job.objects.filter(kind=kind, state=JobState.QUEUED).exists(),
            f"no {kind} job was created",
        )

    def test_library_actions_enqueue(self):
        for name, kind in (
            ("action_scan", "library.scan_all"),
            ("action_plan", "organize.plan_all"),
            ("action_apply", "organize.apply_all"),
            ("action_update_ytdlp", "maintenance.update_ytdlp"),
        ):
            with self.subTest(name=name):
                self.assert_queued(self._post(name), kind)

    def test_per_track_actions_enqueue_with_the_track_id(self):
        for name, kind, payload in (
            ("action_identify_track", "identify.track", {}),
            # `apply` is what makes this a move rather than a dry run.
            ("action_organize_track", "organize.track", {"apply": True}),
            ("action_revert_track", "organize.revert", {}),
        ):
            with self.subTest(name=name):
                self.assert_queued(self._post(name, self.track.pk), kind)
                job = Job.objects.get(kind=kind)
                self.assertEqual(job.payload, {"track_id": self.track.pk, **payload})

    @override_settings(PLAYLIST_URL="https://youtube.com/playlist?list=X")
    def test_sync_youtube_enqueues_with_the_configured_url(self):
        self.assert_queued(self._post("action_sync_youtube"), "youtube.sync")
        self.assertEqual(
            Job.objects.get(kind="youtube.sync").payload,
            {"url": "https://youtube.com/playlist?list=X"},
        )

    @override_settings(PLAYLIST_URL="")
    def test_sync_youtube_without_a_url_says_so_instead_of_queueing(self):
        response = self._post("action_sync_youtube")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Job.objects.filter(kind="youtube.sync").exists())

    def test_download_video_enqueues(self):
        self.assert_queued(
            self._post("action_download_video", self.video.pk), "youtube.download"
        )
        self.assertEqual(
            Job.objects.get(kind="youtube.download").payload,
            {"video_id": "abc123"},
        )

    def test_actions_reject_get(self):
        for name in ("action_scan", "action_plan", "action_apply",
                     "action_sync_youtube", "action_update_ytdlp"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 405)
        for name in ("action_identify_track", "action_organize_track",
                     "action_revert_track"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name, args=[self.track.pk]))
                self.assertEqual(response.status_code, 405)

    def test_unknown_ids_are_404(self):
        self.assertEqual(self._post("action_identify_track", 99999).status_code, 404)
        self.assertEqual(self._post("action_download_video", "nope").status_code, 404)

    def test_identify_with_one_named_provider(self):
        """The row menu asks for a single provider; it reaches the job payload."""
        # Patch the package attribute, not `base`: the view calls
        # `identify_module.available_names()`, and `music/identify/__init__.py`
        # bound that name at import time.
        from music import identify as identify_pkg

        with mock.patch.object(identify_pkg, "available_names",
                               return_value=["shazam", "gemini"]):
            response = self.client.post(
                reverse("action_identify_track", args=[self.track.pk]),
                {"provider": "shazam"},
            )
        self.assertEqual(response.status_code, 204)
        job = Job.objects.get(kind="identify.track")
        self.assertEqual(job.payload["provider"], "shazam")
        self.assertEqual(job.payload["track_id"], self.track.pk)
        # A distinct key, or asking for one provider could be handed a queued
        # whole-chain job and the toast would name a provider that never ran.
        self.assertEqual(job.dedup_key, f"identify.track:{self.track.pk}:shazam")

    def test_identify_rejects_a_provider_that_cannot_run(self):
        """Never queue a job to ask a provider with no key or no binary."""
        # Patch the package attribute, not `base`: the view calls
        # `identify_module.available_names()`, and `music/identify/__init__.py`
        # bound that name at import time.
        from music import identify as identify_pkg

        with mock.patch.object(identify_pkg, "available_names",
                               return_value=["shazam"]):
            response = self.client.post(
                reverse("action_identify_track", args=[self.track.pk]),
                {"provider": "acoustid"},
            )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Job.objects.filter(kind="identify.track").exists())

    def test_identify_without_a_provider_still_runs_the_chain(self):
        response = self._post("action_identify_track", self.track.pk)
        self.assertEqual(response.status_code, 204)
        job = Job.objects.get(kind="identify.track")
        self.assertNotIn("provider", job.payload)
        self.assertEqual(job.dedup_key, f"identify.track:{self.track.pk}")

    def test_repeated_action_reuses_the_active_job(self):
        """Dedup lives in the engine; the view must not create a second row."""
        self._post("action_scan")
        self._post("action_scan")
        self.assertEqual(Job.objects.filter(kind="library.scan_all").count(), 1)


# --------------------------------------------------------------------------
# Toast markup  (docs/CODE-AUDIT.md A11)
# --------------------------------------------------------------------------


class ToastMarkupTests(TestCase):
    """The stored-XSS fix is in the client, so this is where it is guarded.

    Titles come from YouTube verbatim and travel to the browser inside an
    `HX-Trigger` header, which JSON-escapes but does not HTML-escape. That is
    only safe while the toast body is written with `textContent`.
    """

    def test_uploader_title_reaches_the_header_unescaped(self):
        payload = '<img src=x onerror="alert(1)">'
        YoutubeVideo.objects.create(video_id="evil01", title=payload)
        response = self.client.post(
            reverse("action_download_video", args=["evil01"])
        )
        self.assertEqual(response.status_code, 204)
        message = json.loads(response["HX-Trigger"])["notify"]["message"]
        self.assertIn(payload, message)

    def test_toast_body_is_written_with_textContent(self):
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn("body.textContent = message", source)
        # Any of these would reopen the hole. (The file's header comment names
        # `.innerHTML` while explaining the fix, so match on the write, not the
        # bare word.)
        for parser in ("innerHTML =", "innerHTML+=", "innerHTML +=",
                       "insertAdjacentHTML", "outerHTML ="):
            with self.subTest(parser=parser):
                self.assertNotIn(parser, source)


# --------------------------------------------------------------------------
# Settings  (docs/CODE-AUDIT.md A8 + A9)
# --------------------------------------------------------------------------


NUMERIC_KEYS = [key for key, f in views.ENV_FIELDS.items() if f.is_numeric]


def temp_dir(case: TestCase, prefix: str) -> Path:
    directory = tempfile.mkdtemp(prefix=prefix)
    case.addCleanup(shutil.rmtree, directory, True)
    return Path(directory)


class SettingsFormTestCase(TestCase):
    """Base: every test writes to a throwaway `.env`, never the real one."""

    def setUp(self):
        self.env_file = temp_dir(self, "mm-env-") / ".env"
        self.env_file.write_text(
            "# tuning\nPAGE_SIZE=50\nWORKER_THREADS=1\nGEMINI_API_KEY=existing-key\n",
            encoding="utf-8",
        )
        patcher = override_settings(ENV_FILE_PATH=str(self.env_file))
        patcher.enable()
        self.addCleanup(patcher.disable)

    def valid_payload(self, **overrides) -> dict:
        """A submission the form accepts, so a test can spoil one field at a time."""
        payload = {}
        for key, f in views.ENV_FIELDS.items():
            if f.kind == "int":
                payload[key] = str(int(f.minimum if f.minimum is not None else 1))
            elif f.kind == "number":
                payload[key] = str(f.minimum if f.minimum is not None else 1)
            elif f.kind == "bool":
                payload[key] = "0"
            elif f.kind == "choice":
                payload[key] = f.choices[0]
            else:
                payload[key] = "value"
        payload.update(overrides)
        return payload


class SettingsFormRenderTests(SettingsFormTestCase):
    def test_form_renders_without_leaking_a_stored_secret(self):
        response = self.client.get(reverse("settings_form"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "music/_settings_form.html")
        self.assertContains(response, "GEMINI_API_KEY")
        self.assertContains(response, '<span class="badge text-bg-success ms-1">set</span>')
        # Write-only means write-only: not the value, not a mask of it.
        self.assertNotContains(response, "existing-key")

    def test_unset_secret_is_reported_as_not_set(self):
        self.env_file.write_text("PAGE_SIZE=50\n", encoding="utf-8")
        with override_settings(ACOUSTID_API_KEY="", GEMINI_API_KEY=""):
            response = self.client.get(reverse("settings_form"))
        self.assertContains(
            response, '<span class="badge text-bg-secondary ms-1">not set</span>'
        )


class SettingsValidationTests(SettingsFormTestCase):
    """A9: per-field bounds, not one blanket `float()`."""

    def post(self, **overrides):
        return self.client.post(reverse("update_settings"), self.valid_payload(**overrides))

    def test_zero_is_rejected_for_every_field_with_a_positive_floor(self):
        before = envfile.read_values(self.env_file)
        checked = 0
        for key in NUMERIC_KEYS:
            if not views.ENV_FIELDS[key].minimum:
                continue  # 0 is a legitimate value for these (it means "off")
            checked += 1
            with self.subTest(key=key):
                response = self.post(**{key: "0"})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Nothing was saved")
                # Nothing at all was written, not even the fields that were fine.
                self.assertEqual(envfile.read_values(self.env_file), before)
        self.assertGreater(checked, 0)

    def test_negative_is_rejected_for_every_numeric_field(self):
        before = envfile.read_values(self.env_file)
        for key in NUMERIC_KEYS:
            with self.subTest(key=key):
                response = self.post(**{key: "-1"})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Nothing was saved")
                self.assertEqual(envfile.read_values(self.env_file), before)

    def test_sse_keepalive_of_zero_is_rejected(self):
        """The confirmed A9 hazard: a zero cadence turned each stream into a spin."""
        response = self.post(SSE_KEEPALIVE_SECONDS="0")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must be 1 or more")
        self.assertNotIn("SSE_KEEPALIVE_SECONDS", envfile.read_values(self.env_file))

    def test_fractional_worker_threads_is_rejected(self):
        """`float()` accepted '2.5' and settings then died at int() on import."""
        response = self.post(WORKER_THREADS="2.5")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(envfile.read_values(self.env_file)["WORKER_THREADS"], "1")

    def test_maximum_is_enforced(self):
        response = self.post(WORKER_THREADS="99")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(envfile.read_values(self.env_file)["WORKER_THREADS"], "1")

    def test_choice_field_rejects_anything_off_the_list(self):
        response = self.post(DUPLICATE_POLICY="delete-everything")
        self.assertEqual(response.status_code, 200)

    def test_a_line_break_cannot_inject_a_second_assignment(self):
        response = self.post(PLAYLIST_URL="https://x/\nYTDLP_PATH=/tmp/evil")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("YTDLP_PATH", envfile.read_values(self.env_file))

    def test_one_bad_field_saves_nothing(self):
        response = self.post(PAGE_SIZE="0", PLAYLIST_URL="https://example.test/list")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("PLAYLIST_URL", envfile.read_values(self.env_file))


class SettingsSaveTests(SettingsFormTestCase):
    def test_valid_submission_is_written_and_closes_the_panel(self):
        response = self.client.post(
            reverse("update_settings"),
            self.valid_payload(PAGE_SIZE="25", PLAYLIST_URL="https://example.test/list"),
        )
        self.assertEqual(response.status_code, 204)
        trigger = json.loads(response["HX-Trigger"])
        self.assertTrue(trigger["closeSettings"])

        stored = envfile.read_values(self.env_file)
        self.assertEqual(stored["PAGE_SIZE"], "25")
        self.assertEqual(stored["PLAYLIST_URL"], "https://example.test/list")

    def test_blank_secret_keeps_the_stored_key(self):
        self.client.post(reverse("update_settings"), self.valid_payload(GEMINI_API_KEY=""))
        self.assertEqual(
            envfile.read_values(self.env_file)["GEMINI_API_KEY"], "existing-key"
        )

    def test_supplied_secret_replaces_the_stored_key(self):
        self.client.post(
            reverse("update_settings"), self.valid_payload(GEMINI_API_KEY="new-key")
        )
        self.assertEqual(
            envfile.read_values(self.env_file)["GEMINI_API_KEY"], "new-key"
        )

    def test_comments_and_ordering_survive_a_save(self):
        self.client.post(reverse("update_settings"), self.valid_payload(PAGE_SIZE="30"))
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("# tuning", text)
        self.assertLess(text.index("PAGE_SIZE"), text.index("WORKER_THREADS"))


class EnvFileTests(TestCase):
    """A8: the writer itself, independently of the form."""

    def setUp(self):
        self.path = temp_dir(self, "mm-envfile-") / ".env"

    def test_every_duplicate_assignment_is_replaced(self):
        """The old writer rewrote the first line while readers took the last."""
        self.path.write_text("PAGE_SIZE=10\n# note\nPAGE_SIZE=20\n", encoding="utf-8")
        envfile.set_values({"PAGE_SIZE": "50"}, self.path)

        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(text.count("PAGE_SIZE"), 1)
        self.assertEqual(envfile.read_values(self.path)["PAGE_SIZE"], "50")
        self.assertIn("# note", text)

    def test_a_value_containing_a_hash_survives_a_round_trip(self):
        """The old writer split at ' #' and silently truncated the value.

        The value carries a space as well as a hash: both are legal in an
        unquoted .env path and both broke the previous writer.
        """
        value = "/media/pi/My Music/Library # main"
        envfile.set_values({"LIBRARY_ROOT": value}, self.path)
        self.assertEqual(envfile.read_values(self.path)["LIBRARY_ROOT"], value)

    def test_inline_comments_on_unquoted_values_are_ignored_when_reading(self):
        self.path.write_text("WORKER_THREADS=2   # two threads\n", encoding="utf-8")
        self.assertEqual(envfile.read_values(self.path)["WORKER_THREADS"], "2")

    def test_new_keys_are_appended_and_missing_files_created(self):
        envfile.set_values({"PLAYLIST_URL": "https://example.test/list"}, self.path)
        self.assertEqual(
            envfile.read_values(self.path)["PLAYLIST_URL"], "https://example.test/list"
        )

    def test_a_line_break_in_a_value_is_refused(self):
        with self.assertRaises(ValueError):
            envfile.set_values({"PLAYLIST_URL": "a\nYTDLP_PATH=/evil"}, self.path)


class DurationDeltaFilterTests(SimpleTestCase):
    """How a candidate's running time is shown against the file's.

    This is the panel's main evidence: a remix and the album cut share a title
    and an artist, and the length is what separates them. The screenshot that
    prompted these had four "Hookah Bar" rows and no way to tell them apart.
    """

    def test_a_matching_length_reads_as_exact(self):
        self.assertEqual(music_extras.duration_delta(195, 195), "exact")

    def test_a_second_either_way_still_reads_as_exact(self):
        # DURATION_BANDS scores +/-2s as the top band; the panel must agree.
        self.assertEqual(music_extras.duration_delta(383, 382), "exact")
        self.assertEqual(music_extras.duration_delta(293, 295), "exact")

    def test_a_longer_candidate_is_signed(self):
        self.assertEqual(music_extras.duration_delta(222, 195), "+27s")

    def test_a_shorter_candidate_is_signed(self):
        self.assertEqual(music_extras.duration_delta(230, 257), "-27s")

    def test_a_gap_over_a_minute_is_shown_as_minutes(self):
        self.assertEqual(music_extras.duration_delta(295, 3372), "-51:17")

    def test_an_unknown_length_on_either_side_shows_nothing(self):
        # Shazam reports no duration at all; a bare 0 must not read as a match.
        self.assertEqual(music_extras.duration_delta(0, 195), "")
        self.assertEqual(music_extras.duration_delta(195, 0), "")

    def test_junk_is_survivable(self):
        self.assertEqual(music_extras.duration_delta("nonsense", 195), "")
        self.assertEqual(music_extras.duration_delta(None, None), "")

    def test_the_class_tracks_how_far_off_it_is(self):
        self.assertEqual(
            music_extras.duration_delta_class(195, 195), "text-success-emphasis"
        )
        self.assertEqual(
            music_extras.duration_delta_class(205, 195), "text-body-secondary"
        )
        self.assertEqual(
            music_extras.duration_delta_class(295, 3372), "text-warning-emphasis"
        )

    def test_an_unknown_length_is_never_styled_as_agreement(self):
        self.assertEqual(
            music_extras.duration_delta_class(0, 195), "text-body-tertiary"
        )

    def test_exact_matches_the_scorers_tightest_band(self):
        from music.identify import textsearch

        tightest_seconds, _ = textsearch.DURATION_BANDS[0]
        self.assertEqual(music_extras.DURATION_EXACT_SECONDS, tightest_seconds)


class SuggestionPanelDurationTests(SimpleTestCase):
    """The panel renders both lengths so a person can judge a row."""

    def test_the_metadata_carries_the_providers_own_length(self):
        from music.identify.base import TrackMetadata

        meta = TrackMetadata(title="T", artist="A", duration=195)
        self.assertEqual(meta.duration, 195)

    def test_duration_is_not_written_to_the_track(self):
        # Track.duration is measured from the file; a catalogue's idea of the
        # song's length must never overwrite it.
        from music.jobs.handlers.identify import _IDENTIFIED_FIELDS

        self.assertNotIn("duration", _IDENTIFIED_FIELDS)
