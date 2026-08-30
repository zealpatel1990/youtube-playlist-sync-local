"""
Identification chain and providers, with every external boundary mocked.

Nothing here touches the network, spawns a subprocess, or needs a media file.
That is not only for speed: `test_tagger.py` in the previous version ran a live
Shazam pass, real ORM writes and real file renames at import time
(docs/CODE-AUDIT.md A10), and these tests are the replacement for it.

`SimpleTestCase` throughout — none of this package touches the ORM, and not
building a test database keeps the suite runnable on the Pi.
"""

from __future__ import annotations

import asyncio
import json
import time
import threading
import urllib.parse
from dataclasses import replace
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings

from music.identify import acoustid as acoustid_module
from music.identify import (
    base,
    gemini as gemini_module,
    matching,
    shazam as shazam_module,
    suggest,
    textsearch,
)
from music.identify.acoustid import AcoustidProvider
from music.identify.base import IdentifyContext, Provider, TrackMetadata
from music.identify.gemini import GeminiProvider
from music.identify.shazam import ShazamProvider
from music.identify.tags import TagsProvider


def make_context(**kwargs) -> IdentifyContext:
    kwargs.setdefault("path", Path("/library/staging/song.mp3"))
    return IdentifyContext(**kwargs)


# --- fake providers -----------------------------------------------------
#
# build_chain() instantiates classes, so behaviour is declared on the class and
# `make_provider` mints one per scenario. Each records the contexts it saw,
# which is how chain order and short-circuiting are asserted.


class _FakeProvider(Provider):
    name = "fake"
    result: TrackMetadata | None = None
    reason: str = ""
    error: Exception | None = None
    calls: list = []

    def unavailable_reason(self) -> str:
        return self.reason

    def identify(self, ctx):
        type(self).calls.append(ctx)
        if self.error is not None:
            raise self.error
        return self.result


def make_provider(name, *, result=None, reason="", error=None):
    return type(
        f"Fake_{name}",
        (_FakeProvider,),
        {"name": name, "result": result, "reason": reason, "error": error, "calls": []},
    )


@override_settings(IDENTIFY_ENRICH=False)
class ChainTestCase(SimpleTestCase):
    """Base for tests that install a synthetic provider set.

    Enrichment is off here because it is the one step in `identify()` that
    reaches the network on its own — these tests assert chain *order* and
    guard behaviour, and letting a real Apple lookup run for each one put 28
    seconds of live HTTP into a suite whose whole point is that it has none.
    `CatalogueEnrichmentTests` covers it with the calls mocked.
    """

    def setUp(self):
        base.reset_chain()
        self.addCleanup(base.reset_chain)

    def install(self, *classes):
        """Make `classes` the entire universe of providers, in the order given."""
        registry = {cls.name: cls for cls in classes}
        patcher = mock.patch.object(base, "_provider_classes", return_value=registry)
        patcher.start()
        self.addCleanup(patcher.stop)
        return override_settings(IDENTIFY_CHAIN=[cls.name for cls in classes])


class ChainOrderTests(ChainTestCase):
    def test_runs_in_order_and_stops_at_the_first_confident_result(self):
        first = make_provider("first", result=None)
        second = make_provider("second", result=TrackMetadata(
            title="Dreams", artist="Fleetwood Mac", confidence=0.9, provider="second"
        ))
        third = make_provider("third", result=TrackMetadata(
            title="Wrong", artist="Nobody", confidence=0.99, provider="third"
        ))

        with self.install(first, second, third):
            result = base.identify(make_context())

        self.assertIsNotNone(result)
        self.assertEqual(result.provider, "second")
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(second.calls), 1)
        # The whole point of the ordering: the expensive tier never ran.
        self.assertEqual(third.calls, [])

    def test_chain_follows_the_configured_order_not_the_registry_order(self):
        alpha = make_provider("alpha")
        beta = make_provider("beta")

        with self.install(alpha, beta), override_settings(
            IDENTIFY_CHAIN=["beta", "alpha"]
        ):
            chain = base.build_chain()

        self.assertEqual([p.name for p in chain], ["beta", "alpha"])

    def test_a_raising_provider_does_not_break_the_chain(self):
        broken = make_provider("broken", error=RuntimeError("upstream is on fire"))
        working = make_provider("working", result=TrackMetadata(
            title="Dreams", artist="Fleetwood Mac", confidence=0.8
        ))

        with self.install(broken, working):
            with self.assertLogs("music.identify", level="ERROR"):
                result = base.identify(make_context())

        self.assertIsNotNone(result)
        self.assertEqual(result.title, "Dreams")
        self.assertEqual(len(broken.calls), 1)

    def test_nothing_usable_returns_none(self):
        empty = make_provider("empty", result=None)

        with self.install(empty):
            self.assertIsNone(base.identify(make_context()))

    def test_an_empty_chain_is_survivable(self):
        with self.install():
            self.assertIsNone(base.identify(make_context()))


class ConfidenceThresholdTests(ChainTestCase):
    def test_a_result_below_the_threshold_is_discarded(self):
        weak = make_provider("weak", result=TrackMetadata(
            title="Maybe", artist="Someone", confidence=0.4
        ))
        strong = make_provider("strong", result=TrackMetadata(
            title="Dreams", artist="Fleetwood Mac", confidence=0.9
        ))

        with self.install(weak, strong), override_settings(IDENTIFY_MIN_CONFIDENCE=0.5):
            result = base.identify(make_context())

        self.assertEqual(result.provider, "strong")

    def test_only_weak_results_means_no_identification(self):
        weak = make_provider("weak", result=TrackMetadata(
            title="Maybe", artist="Someone", confidence=0.49
        ))

        with self.install(weak), override_settings(IDENTIFY_MIN_CONFIDENCE=0.5):
            self.assertIsNone(base.identify(make_context()))

    def test_raising_the_threshold_rejects_what_a_lower_one_accepted(self):
        provider = make_provider("provider", result=TrackMetadata(
            title="Dreams", artist="Fleetwood Mac", confidence=0.7
        ))

        with self.install(provider), override_settings(IDENTIFY_MIN_CONFIDENCE=0.9):
            self.assertIsNone(base.identify(make_context()))

    def test_an_unusable_result_is_skipped_however_confident(self):
        # Title but no artist of any kind: cannot be placed in the Plex tree,
        # so accepting it would only defer the failure to the organizer.
        titled = make_provider("titled", result=TrackMetadata(
            title="Dreams", confidence=1.0
        ))
        complete = make_provider("complete", result=TrackMetadata(
            title="Dreams", artist="Fleetwood Mac", confidence=0.6
        ))

        with self.install(titled, complete):
            result = base.identify(make_context())

        self.assertEqual(result.provider, "complete")

    def test_the_winning_provider_is_stamped_on_an_unstamped_result(self):
        provider = make_provider("provider", result=TrackMetadata(
            title="Dreams", artist="Fleetwood Mac", confidence=0.9
        ))

        with self.install(provider):
            result = base.identify(make_context())

        self.assertEqual(result.provider, "provider")


class AvailabilityTests(ChainTestCase):
    def test_unavailable_providers_are_skipped_with_a_reason(self):
        missing = make_provider("missing", reason="no key configured")
        present = make_provider("present", result=TrackMetadata(
            title="Dreams", artist="Fleetwood Mac", confidence=0.9
        ))

        with self.install(missing, present):
            with self.assertLogs("music.identify", level="INFO") as logs:
                chain = base.build_chain()

        self.assertEqual([p.name for p in chain], ["present"])
        self.assertTrue(any("no key configured" in line for line in logs.output))

    def test_a_skipped_provider_is_never_called(self):
        missing = make_provider("missing", reason="disabled")
        present = make_provider("present", result=TrackMetadata(
            title="Dreams", artist="Fleetwood Mac", confidence=0.9
        ))

        with self.install(missing, present):
            base.identify(make_context())

        self.assertEqual(missing.calls, [])

    def test_a_provider_that_fails_to_construct_is_dropped(self):
        class Exploding(Provider):
            name = "exploding"

            def __init__(self):
                raise RuntimeError("bad wheel for this architecture")

            def identify(self, ctx):
                return None

        survivor = make_provider("survivor")

        with self.install(Exploding, survivor):
            with self.assertLogs("music.identify", level="ERROR"):
                chain = base.build_chain()

        self.assertEqual([p.name for p in chain], ["survivor"])

    @override_settings(
        IDENTIFY_CHAIN=["tags", "acoustid", "shazam", "gemini"],
        ACOUSTID_API_KEY="",
        GEMINI_API_KEY="",
        SHAZAM_ENABLED=False,
    )
    def test_the_real_providers_drop_out_on_missing_configuration(self):
        chain = base.build_chain()
        self.assertEqual([p.name for p in chain], ["tags"])

    @override_settings(
        IDENTIFY_CHAIN=["acoustid"],
        ACOUSTID_API_KEY="a-real-looking-key",
        FPCALC_PATH="fpcalc-that-is-definitely-not-installed",
    )
    def test_acoustid_needs_the_binary_as_well_as_the_key(self):
        self.assertEqual(base.build_chain(), [])
        self.assertIn("fpcalc-that-is-definitely-not-installed",
                      AcoustidProvider().unavailable_reason())


class TrackMetadataTests(SimpleTestCase):
    def test_is_usable_requires_a_title_and_some_artist(self):
        self.assertFalse(TrackMetadata().is_usable())
        self.assertFalse(TrackMetadata(title="Dreams").is_usable())
        self.assertFalse(TrackMetadata(artist="Fleetwood Mac").is_usable())
        self.assertTrue(TrackMetadata(title="Dreams", artist="Fleetwood Mac").is_usable())
        self.assertTrue(
            TrackMetadata(title="Dreams", album_artist="Various Artists").is_usable()
        )

    def test_merged_with_lets_self_win_on_every_set_field(self):
        primary = TrackMetadata(title="Dreams", artist="Fleetwood Mac", track_no=6)
        secondary = TrackMetadata(
            title="Ignored", artist="Ignored", album="Rumours", disc_no=1,
            genre="Rock", is_compilation=True,
        )

        merged = primary.merged_with(secondary)

        self.assertEqual(merged.title, "Dreams")
        self.assertEqual(merged.track_no, 6)
        # Gaps — "" and 0 and False — are filled from the other side.
        self.assertEqual(merged.album, "Rumours")
        self.assertEqual(merged.disc_no, 1)
        self.assertEqual(merged.genre, "Rock")
        self.assertTrue(merged.is_compilation)


class TagsProviderTests(SimpleTestCase):
    def test_full_tags_score_one(self):
        ctx = make_context(existing=TrackMetadata(
            title="Dreams", artist="Fleetwood Mac", album="Rumours", track_no=6
        ))
        result = TagsProvider().identify(ctx)

        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(result.provider, "tags")
        self.assertEqual(result.track_no, 6)

    def test_title_and_artist_only_score_below_one(self):
        ctx = make_context(existing=TrackMetadata(title="Dreams", artist="Fleetwood Mac"))
        self.assertEqual(TagsProvider().identify(ctx).confidence, 0.7)

    def test_partial_tags_still_clear_the_default_threshold(self):
        # The reason this provider runs first: a tagged library never reaches
        # the rate-limited tiers at all.
        ctx = make_context(existing=TrackMetadata(title="Dreams", artist="Fleetwood Mac"))
        self.assertGreaterEqual(TagsProvider().identify(ctx).confidence, 0.5)

    def test_unusable_tags_pass(self):
        self.assertIsNone(TagsProvider().identify(make_context()))
        self.assertIsNone(
            TagsProvider().identify(make_context(existing=TrackMetadata(album="Rumours")))
        )

    def test_always_available(self):
        self.assertTrue(TagsProvider().available())


# --- AcoustID -----------------------------------------------------------

#: Shaped like a real api.acoustid.org/v2/lookup response with
#: meta=recordings+releases+tracks: two results, several candidate recordings,
#: and a two-disc release carrying the medium/track positions that are the
#: entire reason this provider exists.
ACOUSTID_PAYLOAD = {
    "status": "ok",
    "results": [
        {
            "id": "a-weak-match",
            "score": 0.41,
            "recordings": [
                {"id": "rec-unrelated", "title": "Something Else",
                 "artists": [{"id": "x", "name": "Another Band"}]}
            ],
        },
        {
            "id": "0c2b1d3e-3f4a-4b5c-8d9e-0f1a2b3c4d5e",
            "score": 0.94,
            "recordings": [
                {
                    "id": "rec-live",
                    "title": "Comfortably Numb (live)",
                    "duration": 401,
                    "artists": [{"id": "83d91898", "name": "Pink Floyd"}],
                    "releases": [{"id": "rel-live", "title": "Delicate Sound of Thunder"}],
                },
                {
                    "id": "b1a2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
                    "title": "Comfortably Numb",
                    "duration": 383,
                    "artists": [{"id": "83d91898", "name": "Pink Floyd"}],
                    "releases": [
                        {
                            "id": "rel-remaster",
                            "title": "The Wall (2011 Remaster)",
                            "date": {"year": 2011, "month": 9},
                            "medium_count": 2,
                            "track_count": 26,
                            "artists": [{"id": "83d91898", "name": "Pink Floyd"}],
                            "mediums": [
                                {"format": "CD", "position": 2, "track_count": 13,
                                 "tracks": [{"id": "t-remaster", "position": 6,
                                             "title": "Comfortably Numb"}]}
                            ],
                        },
                        {
                            "id": "9d4d1b3f-2a5e-4c6b-9f8a-1c2d3e4f5a6b",
                            "title": "The Wall",
                            "date": {"year": 1979, "month": 11, "day": 30},
                            "country": "GB",
                            "medium_count": 2,
                            "track_count": 26,
                            "artists": [{"id": "83d91898", "name": "Pink Floyd"}],
                            "mediums": [
                                {"format": "12\" Vinyl", "position": 2, "track_count": 13,
                                 "tracks": [{"id": "t-original", "position": 6,
                                             "title": "Comfortably Numb"}]}
                            ],
                        },
                    ],
                },
            ],
        },
    ],
}

VARIOUS_ARTISTS_PAYLOAD = {
    "status": "ok",
    "results": [
        {
            "id": "compilation-match",
            "score": 0.88,
            "recordings": [
                {
                    "id": "rec-collab",
                    "title": "Under Pressure",
                    "duration": 248,
                    "artists": [
                        {"id": "queen", "name": "Queen", "joinphrase": " & "},
                        {"id": "bowie", "name": "David Bowie"},
                    ],
                    "releases": [
                        {
                            "id": "rel-comp",
                            "title": "Now That's What I Call Music! 1",
                            "date": {"year": 1983},
                            "medium_count": 1,
                            "artists": [
                                {"id": "89ad4ac3-39f7-470e-963a-56509c546377",
                                 "name": "Various Artists"}
                            ],
                            "mediums": [
                                {"format": "CD", "position": 1,
                                 "tracks": [{"id": "t", "position": 4,
                                             "title": "Under Pressure"}]}
                            ],
                        }
                    ],
                }
            ],
        }
    ],
}


class FakeHTTPResponse:
    """The slice of an http.client.HTTPResponse that `_read_body` uses."""

    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        return self._body if size is None or size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@override_settings(
    ACOUSTID_API_KEY="test-key",
    FPCALC_PATH="fpcalc",
    ACOUSTID_RATE_PER_SEC=100.0,
    PROVIDER_TIMEOUT_SECONDS=5.0,
)
class AcoustidProviderTests(SimpleTestCase):
    def setUp(self):
        acoustid_module.reset_for_tests()
        self.addCleanup(acoustid_module.reset_for_tests)

    def _fpcalc(self, fingerprint="AQADtEmSREkSJUmSJEmS", duration=383.14):
        return mock.patch.object(
            acoustid_module.subprocess,
            "run",
            return_value=mock.Mock(
                returncode=0,
                stdout=json.dumps({"duration": duration, "fingerprint": fingerprint}),
                stderr="",
            ),
        )

    def _lookup(self, payload, headers=None):
        return mock.patch(
            "urllib.request.urlopen",
            return_value=FakeHTTPResponse(json.dumps(payload).encode(), headers),
        )

    def test_parses_track_and_disc_numbers_from_the_release(self):
        ctx = make_context()

        with self._fpcalc(), self._lookup(ACOUSTID_PAYLOAD):
            result = AcoustidProvider().identify(ctx)

        self.assertIsNotNone(result)
        self.assertEqual(result.title, "Comfortably Numb")
        self.assertEqual(result.artist, "Pink Floyd")
        self.assertEqual(result.album, "The Wall")
        self.assertEqual(result.album_artist, "Pink Floyd")
        # The fields Shazam cannot supply and the Plex layout requires.
        self.assertEqual(result.disc_no, 2)
        self.assertEqual(result.track_no, 6)
        self.assertEqual(result.year, 1979)
        self.assertFalse(result.is_compilation)
        self.assertEqual(result.confidence, 0.94)
        self.assertEqual(result.provider, "acoustid")
        self.assertEqual(
            result.musicbrainz_recording_id, "b1a2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
        )
        self.assertEqual(
            result.musicbrainz_release_id, "9d4d1b3f-2a5e-4c6b-9f8a-1c2d3e4f5a6b"
        )
        self.assertIn(result.musicbrainz_release_id, result.cover_url)

    def test_the_highest_scoring_result_wins(self):
        with self._fpcalc(), self._lookup(ACOUSTID_PAYLOAD):
            result = AcoustidProvider().identify(make_context())

        self.assertNotEqual(result.title, "Something Else")

    def test_the_recording_matching_the_files_duration_is_chosen(self):
        # 401s live cut versus the 383s studio take, and the file is 383s.
        with self._fpcalc(duration=383.0), self._lookup(ACOUSTID_PAYLOAD):
            result = AcoustidProvider().identify(make_context())

        self.assertEqual(result.title, "Comfortably Numb")

    def test_various_artists_release_is_detected_as_a_compilation(self):
        with self._fpcalc(duration=248.0), self._lookup(VARIOUS_ARTISTS_PAYLOAD):
            result = AcoustidProvider().identify(make_context())

        self.assertTrue(result.is_compilation)
        self.assertEqual(result.album_artist, "Various Artists")
        # The join phrase is honoured, so the credit reads as written.
        self.assertEqual(result.artist, "Queen & David Bowie")
        self.assertEqual(result.track_no, 4)

    def test_fingerprint_and_duration_are_written_back_onto_the_context(self):
        ctx = make_context()

        with self._fpcalc(fingerprint="AQADtEmS", duration=383.6), \
                self._lookup(ACOUSTID_PAYLOAD):
            AcoustidProvider().identify(ctx)

        self.assertEqual(ctx.fingerprint, "AQADtEmS")
        self.assertEqual(ctx.duration, 384)  # rounded to whole seconds

    def test_a_stored_fingerprint_is_reused_instead_of_recomputed(self):
        ctx = make_context(fingerprint="AQADtEmS-already-computed", duration=383)

        with self._fpcalc() as run, self._lookup(ACOUSTID_PAYLOAD):
            result = AcoustidProvider().identify(ctx)

        run.assert_not_called()
        self.assertIsNotNone(result)

    def test_a_cached_fingerprint_without_a_duration_still_runs_fpcalc(self):
        # The lookup needs both values, so a duration of 0 is not skippable.
        ctx = make_context(fingerprint="AQADtEmS", duration=0)

        with self._fpcalc() as run, self._lookup(ACOUSTID_PAYLOAD):
            AcoustidProvider().identify(ctx)

        run.assert_called_once()

    def test_fpcalc_is_bounded_in_both_time_and_audio_length(self):
        with self._fpcalc() as run, self._lookup(ACOUSTID_PAYLOAD):
            AcoustidProvider().identify(make_context())

        command = run.call_args.args[0]
        self.assertEqual(command[0], "fpcalc")
        self.assertIn("-length", command)
        # Its own budget, not PROVIDER_TIMEOUT_SECONDS: fingerprinting is CPU
        # work whose duration depends on how loaded the box is, and bounding it
        # by a network timeout silently disabled AcoustID whenever the queue was
        # busy — measured at 8s idle and over 30s under load.
        self.assertEqual(
            run.call_args.kwargs["timeout"], acoustid_module.FPCALC_TIMEOUT_SECONDS
        )
        self.assertGreater(
            acoustid_module.FPCALC_TIMEOUT_SECONDS, 60,
            "a fingerprint must survive a loaded queue",
        )

    def test_the_lookup_carries_an_explicit_timeout(self):
        with self._fpcalc(), self._lookup(ACOUSTID_PAYLOAD) as urlopen:
            AcoustidProvider().identify(make_context())

        self.assertEqual(urlopen.call_args.kwargs["timeout"], 5.0)

    def test_the_fingerprint_is_posted_not_put_in_the_url(self):
        with self._fpcalc(), self._lookup(ACOUSTID_PAYLOAD) as urlopen:
            AcoustidProvider().identify(make_context())

        request = urlopen.call_args.args[0]
        self.assertIsNotNone(request.data)
        self.assertIn(b"fingerprint=", request.data)
        self.assertIn(b"tracks", request.data)

    def test_a_gzipped_body_is_decompressed(self):
        import gzip

        body = gzip.compress(json.dumps(ACOUSTID_PAYLOAD).encode())
        with self._fpcalc(), mock.patch(
            "urllib.request.urlopen",
            return_value=FakeHTTPResponse(body, {"Content-Encoding": "gzip"}),
        ):
            result = AcoustidProvider().identify(make_context())

        self.assertEqual(result.title, "Comfortably Numb")

    def test_no_match_returns_none(self):
        with self._fpcalc(), self._lookup({"status": "ok", "results": []}):
            self.assertIsNone(AcoustidProvider().identify(make_context()))

    def test_an_error_status_returns_none(self):
        payload = {"status": "error", "error": {"message": "invalid API key"}}
        with self._fpcalc(), self._lookup(payload):
            with self.assertLogs("music.identify", level="WARNING"):
                self.assertIsNone(AcoustidProvider().identify(make_context()))

    def test_a_match_with_no_recording_returns_none(self):
        payload = {"status": "ok", "results": [{"id": "x", "score": 0.9}]}
        with self._fpcalc(), self._lookup(payload):
            self.assertIsNone(AcoustidProvider().identify(make_context()))

    def test_a_failed_fpcalc_never_reaches_the_network(self):
        run = mock.patch.object(
            acoustid_module.subprocess,
            "run",
            return_value=mock.Mock(returncode=1, stdout="", stderr="ERROR: no such file"),
        )
        with run, mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertLogs("music.identify", level="WARNING"):
                self.assertIsNone(AcoustidProvider().identify(make_context()))

        urlopen.assert_not_called()

    def test_a_timed_out_fpcalc_returns_none(self):
        run = mock.patch.object(
            acoustid_module.subprocess,
            "run",
            side_effect=acoustid_module.subprocess.TimeoutExpired("fpcalc", 5.0),
        )
        with run, mock.patch("urllib.request.urlopen"):
            with self.assertLogs("music.identify", level="WARNING"):
                self.assertIsNone(AcoustidProvider().identify(make_context()))

    def test_a_network_failure_returns_none_rather_than_raising(self):
        with self._fpcalc(), mock.patch(
            "urllib.request.urlopen",
            side_effect=acoustid_module.urllib.error.URLError("no route to host"),
        ):
            with self.assertLogs("music.identify", level="WARNING"):
                self.assertIsNone(AcoustidProvider().identify(make_context()))


# --- Shazam -------------------------------------------------------------

SHAZAM_RESPONSE = {
    "matches": [{"id": "123", "offset": 12.3}],
    "track": {
        "key": "40333609",
        "title": "Dreams",
        "subtitle": "Fleetwood Mac",
        "images": {
            "coverart": "https://example.invalid/cover-200.jpg",
            "coverarthq": "https://example.invalid/cover-800.jpg",
        },
        "genres": {"primary": "Rock"},
        "sections": [
            {
                "type": "SONG",
                "metadata": [
                    {"title": "Album", "text": "Rumours"},
                    {"title": "Label", "text": "Warner Records"},
                    {"title": "Released", "text": "1977"},
                ],
            },
            {"type": "LYRICS", "text": ["Now here you go again"]},
        ],
    },
}


@override_settings(
    SHAZAM_ENABLED=True, SHAZAM_RATE_PER_MIN=6000.0, PROVIDER_TIMEOUT_SECONDS=5.0
)
class ShazamProviderTests(SimpleTestCase):
    def setUp(self):
        shazam_module.reset_for_tests()
        self.addCleanup(shazam_module.reset_for_tests)

    def test_parses_a_recognition_response(self):
        result = shazam_module.parse_recognition(SHAZAM_RESPONSE)

        self.assertEqual(result.title, "Dreams")
        self.assertEqual(result.artist, "Fleetwood Mac")
        self.assertEqual(result.album, "Rumours")
        self.assertEqual(result.year, 1977)
        self.assertEqual(result.genre, "Rock")
        self.assertEqual(result.cover_url, "https://example.invalid/cover-800.jpg")
        # No `matches` in this fixture, so the skew is unknown and the score
        # falls back rather than being rewarded — see test_confidence_* below.
        self.assertEqual(result.confidence, shazam_module.CONFIDENCE_UNKNOWN)
        self.assertEqual(result.provider, "shazam")
        # Shazam knows nothing about album artists or track positions.
        self.assertEqual(result.album_artist, "")
        self.assertEqual(result.track_no, 0)

    def test_a_full_date_still_yields_a_year(self):
        response = json.loads(json.dumps(SHAZAM_RESPONSE))
        response["track"]["sections"][0]["metadata"][2]["text"] = "4 February 1977"
        self.assertEqual(shazam_module.parse_recognition(response).year, 1977)

    def test_no_match_returns_none(self):
        self.assertIsNone(shazam_module.parse_recognition({}))
        self.assertIsNone(shazam_module.parse_recognition({"matches": []}))
        self.assertIsNone(shazam_module.parse_recognition(None))

    def test_a_match_missing_an_artist_returns_none(self):
        self.assertIsNone(shazam_module.parse_recognition({"track": {"title": "Dreams"}}))

    def test_identify_uses_the_recognizer_and_parses_it(self):
        with mock.patch.object(
            shazam_module, "_recognize", return_value=SHAZAM_RESPONSE
        ) as recognize:
            result = ShazamProvider().identify(make_context())

        recognize.assert_called_once()
        self.assertEqual(result.title, "Dreams")

    def test_a_recognition_timeout_returns_none(self):
        with mock.patch.object(
            shazam_module, "_recognize", side_effect=asyncio.TimeoutError()
        ):
            with self.assertLogs("music.identify", level="WARNING"):
                self.assertIsNone(ShazamProvider().identify(make_context()))

    def test_a_missing_dependency_returns_none_rather_than_raising(self):
        with mock.patch.object(
            shazam_module, "_recognize", side_effect=ImportError("no module shazamio")
        ):
            with self.assertLogs("music.identify", level="WARNING"):
                self.assertIsNone(ShazamProvider().identify(make_context()))

    @override_settings(SHAZAM_ENABLED=False)
    def test_disabled_by_setting(self):
        self.assertFalse(ShazamProvider().available())
        self.assertIn("SHAZAM_ENABLED", ShazamProvider().unavailable_reason())


# --- Gemini -------------------------------------------------------------


@override_settings(
    GEMINI_API_KEY="test-key",
    GEMINI_MODEL="gemini-flash-latest",
    GEMINI_RATE_PER_MIN=6000.0,
    GEMINI_DAILY_BUDGET=5,
    PROVIDER_TIMEOUT_SECONDS=5.0,
)
class GeminiProviderTests(SimpleTestCase):
    ANSWER = json.dumps({
        "title": "Dreams",
        "artist": "Fleetwood Mac",
        "album": "Rumours",
        "year": 1977,
        "is_compilation": False,
    })

    def setUp(self):
        gemini_module.reset_for_tests()
        self.addCleanup(gemini_module.reset_for_tests)

    def _generate(self, *responses):
        return mock.patch.object(
            GeminiProvider, "_generate", side_effect=list(responses)
        )

    def test_infers_metadata_from_the_video_title(self):
        ctx = make_context(hint_title="Fleetwood Mac - Dreams (HQ Audio)")

        with self._generate(self.ANSWER):
            result = GeminiProvider().identify(ctx)

        self.assertEqual(result.title, "Dreams")
        self.assertEqual(result.artist, "Fleetwood Mac")
        self.assertEqual(result.album, "Rumours")
        self.assertEqual(result.year, 1977)
        self.assertEqual(result.confidence, 0.55)
        self.assertEqual(result.provider, "gemini")

    def test_the_prompt_carries_the_title_and_no_media(self):
        ctx = make_context(
            hint_title="Fleetwood Mac - Dreams", hint_url="https://youtu.be/abc"
        )

        with self._generate(self.ANSWER) as generate:
            GeminiProvider().identify(ctx)

        prompt = generate.call_args.args[0]
        self.assertIn("Fleetwood Mac - Dreams", prompt)
        self.assertIn("https://youtu.be/abc", prompt)
        self.assertIsInstance(prompt, str)  # text only — never a file upload

    @override_settings(GEMINI_DAILY_BUDGET=1)
    def test_budget_exhaustion_returns_none_without_calling_the_model(self):
        ctx = make_context(hint_title="Fleetwood Mac - Dreams")

        with self._generate(self.ANSWER) as generate:
            first = GeminiProvider().identify(ctx)
            with self.assertLogs("music.identify", level="WARNING"):
                second = GeminiProvider().identify(ctx)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        # The whole point: the second track costs no quota, and does not block.
        self.assertEqual(generate.call_count, 1)

    @override_settings(GEMINI_DAILY_BUDGET=1)
    def test_budget_exhaustion_is_logged_once_not_once_per_track(self):
        ctx = make_context(hint_title="Fleetwood Mac - Dreams")

        with self._generate(self.ANSWER, self.ANSWER, self.ANSWER):
            GeminiProvider().identify(ctx)
            with self.assertLogs("music.identify", level="WARNING") as logs:
                GeminiProvider().identify(ctx)
                GeminiProvider().identify(ctx)

        exhausted = [line for line in logs.output if "daily budget" in line]
        self.assertEqual(len(exhausted), 1)

    @override_settings(GEMINI_DAILY_BUDGET=0)
    def test_a_zero_budget_disables_the_provider_entirely(self):
        self.assertFalse(GeminiProvider().available())
        with self._generate(self.ANSWER) as generate:
            self.assertIsNone(
                GeminiProvider().identify(make_context(hint_title="Anything"))
            )
        generate.assert_not_called()

    def test_an_opaque_filename_is_not_worth_a_call(self):
        # What a yt-dlp download is named. A video id tells the model nothing,
        # and asking anyway spends a call from a very small daily allowance.
        ctx = make_context(path=Path("/staging/dQw4w9WgXcQ.mp3"))

        with self._generate(self.ANSWER) as generate:
            self.assertIsNone(GeminiProvider().identify(ctx))

        generate.assert_not_called()

    def test_a_descriptive_filename_is_used_when_nothing_better_exists(self):
        # What a scanned library file is usually named, and the case this
        # provider is actually good at.
        ctx = make_context(path=Path("/music/Fleetwood Mac - Dreams.mp3"))

        with self._generate(self.ANSWER) as generate:
            result = GeminiProvider().identify(ctx)

        self.assertIn("Fleetwood Mac - Dreams", generate.call_args.args[0])
        self.assertEqual(result.title, "Dreams")

    def test_a_request_failure_returns_none_rather_than_raising(self):
        with mock.patch.object(
            GeminiProvider, "_generate", side_effect=RuntimeError("429 quota exceeded")
        ):
            with self.assertLogs("music.identify", level="WARNING"):
                result = GeminiProvider().identify(
                    make_context(hint_title="Fleetwood Mac - Dreams")
                )
        self.assertIsNone(result)

    def test_missing_key_makes_the_provider_unavailable(self):
        with override_settings(GEMINI_API_KEY=""):
            self.assertFalse(GeminiProvider().available())
            self.assertIn("GEMINI_API_KEY", GeminiProvider().unavailable_reason())

    # --- response parsing, which is where a model misbehaves ---

    def test_parse_response_strips_a_markdown_fence(self):
        raw = f"```json\n{self.ANSWER}\n```"
        self.assertEqual(gemini_module.parse_response(raw)["title"], "Dreams")

    def test_parse_response_recovers_an_object_from_surrounding_prose(self):
        raw = f"Sure! Here is the metadata:\n{self.ANSWER}\nHope that helps."
        self.assertEqual(gemini_module.parse_response(raw)["artist"], "Fleetwood Mac")

    def test_parse_response_rejects_junk(self):
        for raw in ("", "   ", "I don't know", "[1, 2, 3]", "{not json}", None):
            self.assertIsNone(gemini_module.parse_response(raw), raw)

    def test_placeholder_answers_are_treated_as_unknown(self):
        answer = json.dumps({"title": "Dreams", "artist": "Unknown Artist"})
        with self._generate(answer):
            self.assertIsNone(
                GeminiProvider().identify(make_context(hint_title="something"))
            )

    def test_a_compilation_answer_sets_the_various_artists_album_artist(self):
        answer = json.dumps({
            "title": "Under Pressure",
            "artist": "Queen & David Bowie",
            "album": "Now That's What I Call Music! 1",
            "year": "1983",
            "is_compilation": "true",
        })
        with self._generate(answer):
            result = GeminiProvider().identify(make_context(hint_title="Under Pressure"))

        self.assertTrue(result.is_compilation)
        self.assertEqual(result.album_artist, "Various Artists")
        self.assertEqual(result.year, 1983)  # coerced from the string the model sent

    def test_an_impossible_year_is_dropped(self):
        answer = json.dumps({"title": "Dreams", "artist": "Fleetwood Mac", "year": 77})
        with self._generate(answer):
            self.assertEqual(
                GeminiProvider().identify(make_context(hint_title="x")).year, 0
            )


class ShazamConfidenceTests(SimpleTestCase):
    """Shazam returns no score, so it is derived from how far the audio was bent.

    The numbers here are real measurements, not invented: the two rejected
    cases are matches this provider actually got wrong on the test library,
    where a flat 0.80 let them beat a later provider that was right.
    """

    def _confidence(self, timeskew: float, freqskew: float) -> float:
        return shazam_module._confidence_from_skew(
            {"matches": [{"timeskew": timeskew, "frequencyskew": freqskew}]}
        )

    def test_a_clean_match_scores_high(self):
        # Chatte Batte and Dilliwaali Girlfriend, both correct.
        self.assertGreater(self._confidence(0.00010, 0.00000), 0.8)
        self.assertGreater(self._confidence(0.00003, -0.00004), 0.8)

    def test_a_slightly_skewed_match_still_clears_the_threshold(self):
        # Hookah Bar: correct, but not a pristine match.
        self.assertGreater(self._confidence(0.00119, 0.00097), 0.5)

    def test_a_badly_skewed_match_is_rejected(self):
        # Rinku Bhabhi -> "Lil Muillet - *Horror Sounds*", and the Carnatic
        # Shape of You -> a generic radio instrumental. Both wrong.
        self.assertLess(self._confidence(-0.00394, 0.00452), 0.5)
        self.assertLess(self._confidence(-0.00254, 0.00981), 0.5)

    def test_the_best_match_wins_when_several_are_returned(self):
        score = shazam_module._confidence_from_skew({
            "matches": [
                {"timeskew": 0.009, "frequencyskew": 0.009},
                {"timeskew": 0.00001, "frequencyskew": 0.00001},
            ]
        })
        self.assertGreater(score, 0.8)

    def test_missing_or_malformed_skew_falls_back(self):
        for response in ({}, {"matches": []}, {"matches": [{}]},
                         {"matches": ["nonsense"]}, {"matches": [{"timeskew": "x"}]}):
            with self.subTest(response=response):
                self.assertEqual(
                    shazam_module._confidence_from_skew(response),
                    shazam_module.CONFIDENCE_UNKNOWN,
                )

    def test_confidence_never_leaves_zero_to_one(self):
        self.assertEqual(self._confidence(10.0, 10.0), 0.0)
        self.assertLessEqual(self._confidence(0.0, 0.0), 1.0)


class TitleCrossCheckTests(SimpleTestCase):
    """The answer is compared against the title we already know.

    Every case here is a real misfiling from the test library.
    """

    def test_a_cover_is_rejected_when_the_file_is_not_one(self):
        # AcoustID filed a Billie Eilish download under Poté at 0.97: the
        # cover's title contains "Billie Eilish Cover", so word overlap
        # endorses it and only the cover marker separates them.
        self.assertTrue(matching.looks_like_a_different_recording(
            "when the party's over (Billie Eilish Cover)", "Poté",
            "Billie Eilish - when the party's over (Audio)",
        ))

    def test_a_cover_is_kept_when_the_file_asked_for_one(self):
        self.assertFalse(matching.looks_like_a_different_recording(
            "Shape Of You (Radio Instrumental)", "Kar Vogue",
            "Shape of You - Carnatic Instrumental Cover",
        ))

    def test_the_real_recording_is_not_rejected(self):
        self.assertFalse(matching.looks_like_a_different_recording(
            "when the party's over", "Billie Eilish",
            "Billie Eilish - when the party's over (Audio)",
        ))

    def test_an_unrelated_answer_is_rejected(self):
        # Shazam answered this for a Sunil Grover comedy track.
        self.assertTrue(matching.is_unrelated(
            "*Horror Sounds*", "Lil Muillet",
            "Rinku Bhabhi : Mere Husband Mujhko Piyar Nahin Karte | Sunil Grover",
        ))

    def test_a_transliterated_title_survives_on_the_artist_alone(self):
        # "Jai Adhyashakti" and "Jay Aadhya Shakti" share no token at all;
        # only the credited artist keeps this correct answer alive.
        self.assertFalse(matching.is_unrelated(
            "Jay Aadhya Shakti", "Ratansinh Vaghela & Damyanti Barot",
            "Ambe Maa Aarti | Jai Adhyashakti | Ratansinh Vaghela, Damyanti Barot",
        ))

    def test_noise_words_alone_do_not_count_as_a_match(self):
        self.assertTrue(matching.is_unrelated(
            "Official Video", "Some Artist",
            "Totally Different Song | Official Video | Full Audio",
        ))

    def test_nothing_is_rejected_when_there_is_no_hint(self):
        self.assertFalse(matching.is_unrelated("Anything", "Anyone", ""))
        self.assertFalse(matching.looks_like_a_different_recording(
            "Anything (Cover)", "Anyone", ""))


class PlaceholderHintTests(SimpleTestCase):
    """A dead video's title is not a title.

    Observed on the real library: a file whose playlist entry had decayed to
    "[Deleted video]" was answered "DrINsaNE - JUST A BOY" by AcoustID, Shazam,
    Gemini *and* its own embedded tags, and every one was discarded for sharing
    no word with the placeholder. "[Deleted video]" tokenises to {"deleted"} —
    "video" is noise — which overlaps nothing, so the guard fired exactly as
    written on an input that carries no information.
    """

    def test_placeholders_are_not_usable_hints(self):
        for placeholder in ("[Private video]", "[Deleted video]", "[private video]"):
            with self.subTest(placeholder=placeholder):
                self.assertEqual(matching.usable_hint(placeholder), "")

    def test_a_real_title_survives_unchanged(self):
        self.assertEqual(
            matching.usable_hint("  Kabira | Yeh Jawaani Hai Deewani  "),
            "Kabira | Yeh Jawaani Hai Deewani",
        )

    def test_a_correct_answer_is_not_unrelated_to_a_placeholder(self):
        self.assertFalse(matching.is_unrelated(
            "Just a Boy", "DrINsaNE", "[Deleted video]"))
        self.assertFalse(matching.is_unrelated(
            "Zara Zara", "Bombay Jayashri", "[Private video]"))

    def test_a_placeholder_names_no_artist_to_contradict(self):
        self.assertFalse(
            matching.contradicts_hint_artist("DrINsaNE", "[Deleted video]")
        )

    def test_a_placeholder_does_not_make_a_cover_look_wrong(self):
        self.assertFalse(matching.looks_like_a_different_recording(
            "Something (Cover)", "Someone", "[Private video]"))


class GuardDemotionTests(ChainTestCase):
    """A guard demotes; it never vetoes.

    Preferring an answer that agrees with the upload title is right. Returning
    nothing when every provider agreed with each other is not — the track ends
    up FAILED and unorganized, and re-fingerprints on every retry.
    """

    def _answer(self, **kwargs) -> TrackMetadata:
        kwargs.setdefault("title", "Just a Boy")
        kwargs.setdefault("artist", "DrINsaNE")
        kwargs.setdefault("confidence", 0.94)
        return TrackMetadata(**kwargs)

    def test_an_unrelated_answer_is_kept_when_nothing_else_answers(self):
        provider = make_provider("acoustid", result=self._answer())
        with self.install(provider):
            result = base.identify(make_context(hint_title="Totally Other Song"))
        self.assertIsNotNone(result)
        self.assertEqual(result.title, "Just a Boy")
        self.assertEqual(result.provider, "acoustid")

    def test_a_title_matching_answer_still_beats_a_demoted_one(self):
        """The Poté protection: a later provider that agrees with the title wins."""
        demoted = make_provider("acoustid", result=self._answer(
            title="when the party's over (Billie Eilish Cover)", artist="Poté",
            confidence=0.97,
        ))
        agreeing = make_provider("shazam", result=self._answer(
            title="when the party's over", artist="Billie Eilish", confidence=0.80,
        ))
        hint = "Billie Eilish - when the party's over (Audio)"
        with self.install(demoted, agreeing):
            result = base.identify(make_context(hint_title=hint))
        self.assertEqual(result.artist, "Billie Eilish")
        self.assertEqual(result.provider, "shazam")

    def test_a_below_threshold_answer_is_never_kept_as_a_fallback(self):
        weak = make_provider("acoustid", result=self._answer(confidence=0.20))
        with self.install(weak):
            result = base.identify(make_context(hint_title="Totally Other Song"))
        self.assertIsNone(result)

    def test_an_artist_mismatch_is_preferred_over_a_cover(self):
        cover = make_provider("acoustid", result=self._answer(
            title="Dreams (Fleetwood Mac Cover)", artist="Somebody Else",
        ))
        mismatch = make_provider("shazam", result=self._answer(
            title="Dreams", artist="Not Fleetwood",
        ))
        with self.install(cover, mismatch):
            result = base.identify(
                make_context(hint_title="Fleetwood Mac - Dreams")
            )
        self.assertEqual(result.artist, "Not Fleetwood")

    def test_a_kept_fallback_records_which_provider_gave_it(self):
        provider = make_provider("gemini", result=self._answer())
        with self.install(provider):
            result = base.identify(make_context(hint_title="Nothing In Common"))
        self.assertEqual(result.provider, "gemini")

    def test_a_placeholder_hint_no_longer_costs_the_whole_chain(self):
        """The exact failure from the library, end to end."""
        chain = [
            make_provider(name, result=self._answer())
            for name in ("acoustid", "shazam", "gemini", "tags")
        ]
        with self.install(*chain):
            result = base.identify(make_context(hint_title="[Deleted video]"))
        self.assertIsNotNone(result)
        self.assertEqual(result.artist, "DrINsaNE")
        # First provider wins outright: with no usable hint there is nothing to
        # demote it against, so it is not a fallback at all.
        self.assertEqual(result.provider, "acoustid")


class ShazamExcerptWindowTests(SimpleTestCase):
    """Which seconds get sent matters more than how many."""

    def test_the_sample_skips_the_intro(self):
        start = shazam_module._excerpt_start(200)
        self.assertGreater(start, 30, "an intro or spoken opening carries nothing")
        self.assertLess(start, 100)

    def test_a_short_file_is_taken_from_the_start(self):
        self.assertEqual(shazam_module._excerpt_start(20), 0.0)
        self.assertEqual(shazam_module._excerpt_start(0), 0.0)

    def test_a_long_mix_does_not_seek_arbitrarily_far(self):
        self.assertLessEqual(
            shazam_module._excerpt_start(3600), shazam_module.EXCERPT_MAX_START_SECONDS
        )


# --- catalogue search (iTunes / Deezer) ---------------------------------


ITUNES_PAYLOAD = {
    "resultCount": 2,
    "results": [
        {
            "trackName": "Zara Zara (Jhankar Beats)",
            "artistName": "Bombay Jayashri, Harris Jayaraj & Sameer",
            "collectionName": "Zara Zara (Jhankar Beats) - Single",
            "trackNumber": 1,
            "discNumber": 1,
            "releaseDate": "2024-03-15T12:00:00Z",
            "trackTimeMillis": 295000,
            "artworkUrl100": "https://is1.mzstatic.com/image/thumb/x/100x100bb.jpg",
        },
        {
            "trackName": "Zara Zara (Deep House Mix)",
            "artistName": "Bombay Jayashri, Harris Jayaraj & Sameer",
            "collectionName": "Zara Zara (Deep House Mix) - Single",
            "trackNumber": 1,
            "discNumber": 1,
            "releaseDate": "2023-01-01T12:00:00Z",
            "trackTimeMillis": 322000,
        },
    ],
}

DEEZER_PAYLOAD = {
    "data": [
        {
            "title": "Just a Boy",
            "artist": {"name": "DrINsaNE"},
            "album": {
                "title": "Just a Boy",
                "cover_medium": "https://e-cdn.dzcdn.net/250.jpg",
                "cover_big": "https://e-cdn.dzcdn.net/500.jpg",
            },
            "duration": 195,
            "release_date": "2025-11-28",
        }
    ]
}


def _json_response(payload) -> FakeHTTPResponse:
    return FakeHTTPResponse(json.dumps(payload).encode())


@override_settings(
    ITUNES_ENABLED=True,
    ITUNES_RATE_PER_MIN=6000.0,
    ITUNES_COUNTRY="US",
    DEEZER_ENABLED=True,
    DEEZER_RATE_PER_MIN=6000.0,
    PROVIDER_TIMEOUT_SECONDS=5.0,
)
class CatalogueSearchTests(SimpleTestCase):
    """The keyless text-search providers. Every HTTP call is mocked."""

    def setUp(self):
        textsearch.reset_for_tests()
        self.addCleanup(textsearch.reset_for_tests)

    def _respond(self, payload):
        return mock.patch(
            "urllib.request.urlopen", return_value=_json_response(payload)
        )

    # -- query building --

    def test_tags_are_preferred_over_the_upload_title(self):
        ctx = make_context(
            existing=TrackMetadata(title="Zara Zara", artist="Bombay Jayashri"),
            hint_title="Zara Zara Full Video Song | RHTDM",
        )
        self.assertEqual(textsearch.queries_for(ctx)[0], "Zara Zara Bombay Jayashri")

    def test_the_album_artist_stands_in_when_there_is_no_artist(self):
        ctx = make_context(
            existing=TrackMetadata(title="Iktara", album_artist="Amit Trivedi")
        )
        self.assertEqual(textsearch.queries_for(ctx)[0], "Iktara Amit Trivedi")

    def test_a_placeholder_upload_title_is_not_searched(self):
        ctx = make_context(hint_title="[Deleted video]")
        lowered = [q.lower() for q in textsearch.queries_for(ctx)]
        self.assertNotIn("[deleted video]", lowered)

    def test_the_filename_is_the_last_resort(self):
        ctx = make_context(path=Path("/library/Some_Song.mp3"))
        self.assertEqual(textsearch.queries_for(ctx), ["Some Song"])

    def test_queries_are_deduplicated(self):
        ctx = make_context(
            path=Path("/library/Iktara.mp3"),
            existing=TrackMetadata(title="Iktara"),
            hint_title="iktara",
        )
        self.assertEqual(len(textsearch.queries_for(ctx)), 1)

    # -- scoring --

    def test_an_exact_duration_scores_highest(self):
        self.assertEqual(textsearch._confidence(195, 195), 0.90)

    def test_confidence_falls_as_the_duration_drifts(self):
        scores = [textsearch._confidence(d, 200) for d in (200, 204, 210, 240, 400)]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_a_wildly_different_duration_lands_below_the_threshold(self):
        # A 56-minute jukebox against a 5-minute catalogue entry.
        self.assertLess(textsearch._confidence(293, 3372), 0.5)

    def test_an_unknown_duration_cannot_score_highly(self):
        self.assertEqual(
            textsearch._confidence(0, 195), textsearch.CONFIDENCE_UNVERIFIED
        )
        self.assertLess(textsearch.CONFIDENCE_UNVERIFIED, 0.5)

    # -- relevance --
    #
    # Both rejections below were real false positives, at 0.82 and 0.52, before
    # MIN_QUERY_COVERAGE replaced a single-shared-word test.

    def test_a_row_sharing_only_an_incidental_word_is_rejected(self):
        meta = TrackMetadata(
            title="Haqiqi (feat. Aditi Paul & Kiran Kamath) [Remix]",
            artist="Justin-Uday Duo",
        )
        self.assertFalse(
            textsearch._is_relevant(meta, "Diwali Mela Final Alex & Kiran")
        )

    def test_the_right_artist_with_the_wrong_title_is_rejected(self):
        meta = TrackMetadata(
            title="Edvard Grieg In The Hall Of The Mountain King",
            artist="Daniel B. George",
        )
        self.assertFalse(
            textsearch._is_relevant(meta, "Merry Christmas Daniel B. George")
        )

    def test_a_genuine_match_survives(self):
        meta = TrackMetadata(title="JUST A BOY", artist="DrINsaNE")
        self.assertTrue(textsearch._is_relevant(meta, "JUST A BOY DrINsaNE"))

    def test_a_transliterated_variant_survives(self):
        meta = TrackMetadata(
            title="Main Zindagi Ka Saath Nibhata Chala Gaya", artist="Mohd. Rafi"
        )
        self.assertTrue(
            textsearch._is_relevant(meta, "Main Zindagi Ka Saath Mohd Rafi")
        )

    def test_an_uncomparable_query_defers_rather_than_guessing(self):
        meta = TrackMetadata(title="Anything", artist="Anyone")
        self.assertTrue(textsearch._is_relevant(meta, "07 08 09"))

    # -- mapping --

    def test_itunes_supplies_track_and_disc_numbers(self):
        ctx = make_context(
            duration=295,
            existing=TrackMetadata(
                title="Zara Zara (Jhankar Beats)", artist="Bombay Jayashri"
            ),
        )
        with self._respond(ITUNES_PAYLOAD):
            result = textsearch.ItunesProvider().identify(ctx)

        self.assertEqual(result.title, "Zara Zara (Jhankar Beats)")
        self.assertEqual(result.track_no, 1)
        self.assertEqual(result.disc_no, 1)
        self.assertEqual(result.year, 2024)
        self.assertEqual(result.provider, "itunes")
        self.assertEqual(result.confidence, 0.90)

    def test_the_row_whose_duration_fits_wins(self):
        # The 322s Deep House mix is a real alternative; the file is 295s.
        ctx = make_context(
            duration=295,
            existing=TrackMetadata(title="Zara Zara", artist="Bombay Jayashri"),
        )
        with self._respond(ITUNES_PAYLOAD):
            result = textsearch.ItunesProvider().identify(ctx)

        self.assertIn("Jhankar", result.title)

    def test_deezer_maps_its_nested_artist_and_album(self):
        ctx = make_context(
            duration=195,
            existing=TrackMetadata(title="Just a Boy", artist="DrINsaNE"),
        )
        with self._respond(DEEZER_PAYLOAD):
            result = textsearch.DeezerProvider().identify(ctx)

        self.assertEqual(result.artist, "DrINsaNE")
        self.assertEqual(result.album, "Just a Boy")
        self.assertEqual(result.year, 2025)
        self.assertEqual(result.provider, "deezer")

    def test_a_deezer_row_carries_no_track_number(self):
        ctx = make_context(
            duration=195,
            existing=TrackMetadata(title="Just a Boy", artist="DrINsaNE"),
        )
        with self._respond(DEEZER_PAYLOAD):
            result = textsearch.DeezerProvider().identify(ctx)

        self.assertEqual(result.track_no, 0)

    # -- artwork --

    def test_itunes_artwork_is_upscaled_from_the_thumbnail_url(self):
        ctx = make_context(
            duration=295,
            existing=TrackMetadata(
                title="Zara Zara (Jhankar Beats)", artist="Bombay Jayashri"
            ),
        )
        with self._respond(ITUNES_PAYLOAD):
            result = textsearch.ItunesProvider().identify(ctx)

        self.assertIn(f"{textsearch.ARTWORK_SIZE}x{textsearch.ARTWORK_SIZE}",
                      result.cover_url)
        self.assertNotIn("100x100", result.cover_url)

    def test_a_row_without_artwork_reports_no_cover(self):
        self.assertEqual(textsearch._itunes_artwork({}), "")

    def test_an_unexpected_artwork_url_is_kept_rather_than_mangled(self):
        url = "https://example.test/cover.jpg"
        self.assertEqual(textsearch._itunes_artwork({"artworkUrl100": url}), url)

    def test_deezer_prefers_the_larger_published_cover(self):
        ctx = make_context(
            duration=195,
            existing=TrackMetadata(title="Just a Boy", artist="DrINsaNE"),
        )
        with self._respond(DEEZER_PAYLOAD):
            result = textsearch.DeezerProvider().identify(ctx)

        self.assertEqual(result.cover_url, "https://e-cdn.dzcdn.net/500.jpg")

    # -- request shape --

    def test_the_configured_storefront_reaches_the_url(self):
        ctx = make_context(existing=TrackMetadata(title="X", artist="Y"))
        with override_settings(ITUNES_COUNTRY="IN"), self._respond(
            {"results": []}
        ) as urlopen:
            textsearch.ItunesProvider().identify(ctx)

        self.assertIn("country=IN", urlopen.call_args.args[0].full_url)

    def test_every_request_carries_the_configured_timeout(self):
        ctx = make_context(existing=TrackMetadata(title="X", artist="Y"))
        with self._respond({"results": []}) as urlopen:
            textsearch.ItunesProvider().identify(ctx)

        self.assertEqual(urlopen.call_args.kwargs["timeout"], 5.0)

    # -- failure --

    def test_no_rows_returns_none(self):
        ctx = make_context(existing=TrackMetadata(title="X", artist="Y"))
        with self._respond({"results": []}):
            self.assertIsNone(textsearch.ItunesProvider().identify(ctx))

    def test_a_network_failure_is_a_miss_not_a_raise(self):
        ctx = make_context(existing=TrackMetadata(title="X", artist="Y"))
        with mock.patch("urllib.request.urlopen", side_effect=OSError("reset")):
            self.assertIsNone(textsearch.ItunesProvider().identify(ctx))

    def test_a_body_that_is_not_json_is_a_miss(self):
        ctx = make_context(existing=TrackMetadata(title="X", artist="Y"))
        with mock.patch(
            "urllib.request.urlopen",
            return_value=FakeHTTPResponse(b"<html>rate limited</html>"),
        ):
            self.assertIsNone(textsearch.ItunesProvider().identify(ctx))

    def test_an_oversized_body_is_discarded(self):
        ctx = make_context(existing=TrackMetadata(title="X", artist="Y"))
        huge = b"x" * (textsearch.MAX_RESPONSE_BYTES + 10)
        with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse(huge)):
            self.assertIsNone(textsearch.ItunesProvider().identify(ctx))

    def test_a_disabled_provider_reports_why(self):
        with override_settings(ITUNES_ENABLED=False):
            reason = textsearch.ItunesProvider().unavailable_reason()
            self.assertIn("ITUNES_ENABLED", reason)
        with override_settings(DEEZER_ENABLED=False):
            reason = textsearch.DeezerProvider().unavailable_reason()
            self.assertIn("DEEZER_ENABLED", reason)

    # -- seeding --

    def test_a_seed_is_searched_before_the_files_own_text(self):
        ctx = make_context(existing=TrackMetadata(title="Tane Joyi Me Jyaarthi"))
        with self._respond({"results": []}) as urlopen:
            textsearch.ItunesProvider().candidates(
                ctx, seeds=["Lagyo Prityu No Rang Umesh Barot"]
            )

        first = urllib.parse.unquote_plus(urlopen.call_args_list[0].args[0].full_url)
        self.assertIn("Lagyo Prityu No Rang", first)

    def test_seeds_are_bounded(self):
        ctx = make_context()
        seeds = [f"seed {n}" for n in range(20)]
        with self._respond({"results": []}) as urlopen:
            textsearch.ItunesProvider().candidates(ctx, seeds=seeds)

        ceiling = textsearch.MAX_SEED_QUERIES + len(textsearch.queries_for(ctx))
        self.assertLessEqual(len(urlopen.call_args_list), ceiling)


class CatalogueChainRegistrationTests(SimpleTestCase):
    """Both providers are nameable in IDENTIFY_CHAIN."""

    def test_both_are_known_providers(self):
        classes = base._provider_classes()
        self.assertIn("itunes", classes)
        self.assertIn("deezer", classes)

    @override_settings(
        IDENTIFY_CHAIN=["itunes", "deezer"],
        ITUNES_ENABLED=True,
        DEEZER_ENABLED=True,
    )
    def test_they_build_into_the_chain_in_order(self):
        base.reset_chain()
        self.addCleanup(base.reset_chain)
        self.assertEqual([p.name for p in base.get_chain()], ["itunes", "deezer"])


class SuggestionSeedingTests(SimpleTestCase):
    """The panel hands catalogue search what the audio providers already found."""

    def test_seed_queries_are_title_and_artist_best_first(self):
        found = [
            TrackMetadata(title="Weak", artist="Nobody", confidence=0.2),
            TrackMetadata(
                title="Lagyo Prityu No Rang", artist="Umesh Barot", confidence=0.85
            ),
        ]
        self.assertEqual(
            suggest._seed_queries(found)[0], "Lagyo Prityu No Rang Umesh Barot"
        )

    def test_the_same_answer_from_two_providers_seeds_once(self):
        found = [
            TrackMetadata(
                title="Same", artist="Artist", confidence=0.9, provider="shazam"
            ),
            TrackMetadata(
                title="Same", artist="Artist", confidence=0.8, provider="tags"
            ),
        ]
        self.assertEqual(len(suggest._seed_queries(found)), 1)

    def test_an_answer_with_no_text_seeds_nothing(self):
        self.assertEqual(suggest._seed_queries([TrackMetadata()]), [])

    @override_settings(ITUNES_ENABLED=True)
    def test_a_catalogue_provider_is_offered_even_when_the_chain_omits_it(self):
        # The point of SUGGEST_STANDALONE: try it by hand before promoting it
        # to automatic use.
        self.assertIsNotNone(suggest._standalone("itunes"))

    @override_settings(ITUNES_ENABLED=False)
    def test_a_switched_off_provider_is_not_offered(self):
        self.assertIsNone(suggest._standalone("itunes"))

    def test_only_catalogue_providers_may_be_built_standalone(self):
        self.assertIsNone(suggest._standalone("acoustid"))
        self.assertIsNone(suggest._standalone("gemini"))


@override_settings(
    IDENTIFY_ENRICH=True,
    ITUNES_ENABLED=True,
    ITUNES_RATE_PER_MIN=6000.0,
    ITUNES_COUNTRY="US",
    DEEZER_ENABLED=True,
    DEEZER_RATE_PER_MIN=6000.0,
    PROVIDER_TIMEOUT_SECONDS=5.0,
)
class CatalogueEnrichmentTests(SimpleTestCase):
    """Filling a bare answer's blanks from a catalogue. HTTP is mocked."""

    def setUp(self):
        textsearch.reset_for_tests()
        self.addCleanup(textsearch.reset_for_tests)

    def _respond(self, payload):
        return mock.patch(
            "urllib.request.urlopen", return_value=_json_response(payload)
        )

    def _shazam_answer(self):
        # What Shazam actually returns: a name, and none of the rest.
        return TrackMetadata(
            title="Zara Zara (Jhankar Beats)",
            artist="Bombay Jayashri, Harris Jayaraj & Sameer",
            confidence=0.86,
            provider="shazam",
        )

    def test_the_blanks_are_filled(self):
        ctx = make_context(duration=295)
        with self._respond(ITUNES_PAYLOAD):
            result = textsearch.enrich(self._shazam_answer(), ctx)

        self.assertEqual(result.album, "Zara Zara (Jhankar Beats) - Single")
        self.assertEqual(result.track_no, 1)
        self.assertEqual(result.disc_no, 1)
        self.assertEqual(result.year, 2024)
        self.assertTrue(result.cover_url)

    def test_the_identifying_provider_and_score_are_preserved(self):
        ctx = make_context(duration=295)
        with self._respond(ITUNES_PAYLOAD):
            result = textsearch.enrich(self._shazam_answer(), ctx)

        # The dashboard credits whoever recognised the audio, not whoever
        # supplied the album art.
        self.assertEqual(result.provider, "shazam")
        self.assertEqual(result.confidence, 0.86)

    def test_the_title_and_artist_are_never_overwritten(self):
        answer = replace(self._shazam_answer(), title="Zara Zara", artist="Somebody")
        ctx = make_context(duration=295)
        with self._respond(ITUNES_PAYLOAD):
            result = textsearch.enrich(answer, ctx)

        self.assertEqual(result.title, "Zara Zara")
        self.assertEqual(result.artist, "Somebody")

    def test_a_field_the_chain_already_set_survives(self):
        answer = replace(self._shazam_answer(), album="The Album I Already Had")
        ctx = make_context(duration=295)
        with self._respond(ITUNES_PAYLOAD):
            result = textsearch.enrich(answer, ctx)

        self.assertEqual(result.album, "The Album I Already Had")

    def test_a_loose_duration_match_is_not_trusted_to_enrich(self):
        # 295s catalogue row against a 260s file: offered in the panel at a low
        # score, but never merged in silently.
        ctx = make_context(duration=260)
        with self._respond(ITUNES_PAYLOAD):
            result = textsearch.enrich(self._shazam_answer(), ctx)

        self.assertEqual(result.album, "")
        self.assertEqual(result.track_no, 0)

    def test_an_answer_with_nothing_missing_makes_no_request(self):
        complete = TrackMetadata(
            title="T", artist="A", album="Al", album_artist="A", track_no=1,
            disc_no=1, year=2020, genre="Pop", cover_url="http://x/y.jpg",
            confidence=0.9, provider="shazam",
        )
        with mock.patch("urllib.request.urlopen") as urlopen:
            textsearch.enrich(complete, make_context(duration=295))

        urlopen.assert_not_called()

    def test_a_catalogue_answer_is_not_enriched_from_the_other_catalogue(self):
        answer = TrackMetadata(
            title="T", artist="A", confidence=0.9, provider="itunes"
        )
        with mock.patch("urllib.request.urlopen") as urlopen:
            textsearch.enrich(answer, make_context(duration=295))

        urlopen.assert_not_called()

    def test_an_unusable_answer_is_returned_untouched(self):
        bare = TrackMetadata(confidence=0.9, provider="shazam")
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertIs(textsearch.enrich(bare, make_context()), bare)

        urlopen.assert_not_called()

    @override_settings(IDENTIFY_ENRICH=False)
    def test_the_setting_switches_it_off(self):
        answer = self._shazam_answer()
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertIs(textsearch.enrich(answer, make_context(duration=295)), answer)

        urlopen.assert_not_called()

    def test_a_network_failure_leaves_the_answer_intact(self):
        answer = self._shazam_answer()
        with mock.patch("urllib.request.urlopen", side_effect=OSError("reset")):
            self.assertEqual(
                textsearch.enrich(answer, make_context(duration=295)), answer
            )

    def test_the_chain_enriches_what_it_returns(self):
        provider = make_provider(
            "shazam",
            result=TrackMetadata(
                title="Zara Zara (Jhankar Beats)",
                artist="Bombay Jayashri, Harris Jayaraj & Sameer",
                confidence=0.86,
            ),
        )
        registry = {"shazam": provider}
        with mock.patch.object(base, "_provider_classes", return_value=registry), \
                override_settings(IDENTIFY_CHAIN=["shazam"]), \
                self._respond(ITUNES_PAYLOAD):
            base.reset_chain()
            self.addCleanup(base.reset_chain)
            result = base.identify(make_context(duration=295))

        self.assertEqual(result.track_no, 1)
        self.assertEqual(result.provider, "shazam")

    def test_a_raising_enrichment_cannot_lose_the_identification(self):
        answer = TrackMetadata(title="T", artist="A", confidence=0.9, provider="shazam")
        with mock.patch.object(
            textsearch, "enrich", side_effect=RuntimeError("boom")
        ):
            self.assertIs(base._enriched(answer, make_context()), answer)


@override_settings(ITUNES_ENABLED=True, DEEZER_ENABLED=True)
class GeminiSeedTests(SimpleTestCase):
    """Gemini proposes a spelling; only what a catalogue confirms is shown."""

    def _gemini(self, result):
        provider = mock.Mock()
        provider.identify.return_value = result
        return provider

    def test_a_guess_becomes_a_query(self):
        gemini = self._gemini(
            TrackMetadata(title="Dhaal Bhaat Shaak Rotli", artist="Parle Patel")
        )
        seeds = suggest._ask_for_seeds({"gemini": gemini}, make_context(), [])
        self.assertEqual(seeds, ["Dhaal Bhaat Shaak Rotli Parle Patel"])

    def test_nothing_is_asked_when_the_audio_already_answered(self):
        gemini = self._gemini(TrackMetadata(title="X", artist="Y"))
        found = [TrackMetadata(title="Known", artist="Artist", confidence=0.9)]
        self.assertEqual(
            suggest._ask_for_seeds({"gemini": gemini}, make_context(), found), []
        )
        gemini.identify.assert_not_called()

    def test_an_unusable_guess_seeds_nothing(self):
        gemini = self._gemini(TrackMetadata(title="Only a title"))
        self.assertEqual(
            suggest._ask_for_seeds({"gemini": gemini}, make_context(), []), []
        )

    def test_a_raising_seed_provider_is_survivable(self):
        gemini = mock.Mock()
        gemini.identify.side_effect = RuntimeError("quota")
        self.assertEqual(
            suggest._ask_for_seeds({"gemini": gemini}, make_context(), []), []
        )

    def test_gemini_is_never_offered_as_a_candidate(self):
        self.assertNotIn("gemini", suggest.SUGGEST_PROVIDERS)
        self.assertIn("gemini", suggest.SUGGEST_SEED_ONLY)


class SuggestionRankingTests(SimpleTestCase):
    """Ordering the panel offers, and which row gets the highlighted button."""

    def _hookah(self):
        # The real case: same title, same artist, same scoring band, and the
        # remix inserted first. The file is the 4:14 album cut.
        return [
            TrackMetadata(title="Hookah Bar (Remix)", artist="Himesh Reshammiya",
                          track_no=9, duration=202, confidence=0.52,
                          provider="itunes"),
            TrackMetadata(title="Hookah Bar", artist="Himesh Reshammiya",
                          track_no=5, duration=254, confidence=0.52,
                          provider="itunes"),
        ]

    def test_the_closer_running_time_wins_a_tie(self):
        ranked = suggest._rank(self._hookah(), 254)
        self.assertEqual(ranked[0].title, "Hookah Bar")

    def test_without_a_file_duration_the_order_is_left_alone(self):
        ranked = suggest._rank(self._hookah(), 0)
        self.assertEqual(ranked[0].title, "Hookah Bar (Remix)")

    def test_confidence_still_outranks_running_time(self):
        candidates = [
            TrackMetadata(title="Close but unsure", artist="A", track_no=1,
                          duration=195, confidence=0.40, provider="itunes"),
            TrackMetadata(title="Further but sure", artist="A", track_no=1,
                          duration=210, confidence=0.90, provider="itunes"),
        ]
        self.assertEqual(suggest._rank(candidates, 195)[0].title, "Further but sure")

    def test_a_track_number_still_outranks_everything(self):
        candidates = [
            TrackMetadata(title="No track number", artist="A", duration=195,
                          confidence=0.90, provider="shazam"),
            TrackMetadata(title="Has one", artist="A", track_no=3, duration=260,
                          confidence=0.52, provider="itunes"),
        ]
        self.assertEqual(suggest._rank(candidates, 195)[0].title, "Has one")

    def test_a_candidate_with_no_duration_does_not_pose_as_a_match(self):
        candidates = [
            TrackMetadata(title="Unknown length", artist="A", track_no=1,
                          confidence=0.52, provider="shazam"),
            TrackMetadata(title="Exactly right", artist="A", track_no=1,
                          duration=195, confidence=0.52, provider="itunes"),
        ]
        self.assertEqual(suggest._rank(candidates, 195)[0].title, "Exactly right")


class CatalogueConcurrencyTests(SimpleTestCase):
    """Stage two runs the catalogues together without becoming unpredictable."""

    def _catalogue(self, name, rows, delay=0.0, error=None):
        """A stand-in catalogue provider that records when it was called."""
        provider = mock.Mock()
        provider.name = name

        def candidates(ctx, seeds=None):
            provider.seen_seeds = list(seeds or [])
            provider.started = time.monotonic()
            if delay:
                time.sleep(delay)
            provider.finished = time.monotonic()
            if error is not None:
                raise error
            return rows

        provider.candidates.side_effect = candidates
        return provider

    def test_both_catalogues_run_at_once(self):
        slow = self._catalogue("itunes", [], delay=0.30)
        also_slow = self._catalogue("deezer", [], delay=0.30)

        start = time.monotonic()
        suggest._from_catalogues(
            {"itunes": slow, "deezer": also_slow}, make_context(), []
        )
        elapsed = time.monotonic() - start

        # Sequentially this is 0.60s. Generous ceiling so a loaded CI box does
        # not fail it, but far below the sequential cost.
        self.assertLess(elapsed, 0.50)

    def test_the_order_shown_does_not_depend_on_which_finished_first(self):
        # Deezer returns immediately, iTunes dawdles. iTunes must still lead,
        # because SUGGEST_PROVIDERS says so.
        slow = self._catalogue(
            "itunes",
            [TrackMetadata(title="From iTunes", artist="A", provider="itunes")],
            delay=0.20,
        )
        fast = self._catalogue(
            "deezer",
            [TrackMetadata(title="From Deezer", artist="A", provider="deezer")],
        )

        rows = suggest._from_catalogues(
            {"itunes": slow, "deezer": fast}, make_context(), []
        )

        self.assertEqual([r.provider for r in rows], ["itunes", "deezer"])

    def test_one_catalogue_failing_does_not_cost_the_other(self):
        broken = self._catalogue("itunes", [], error=RuntimeError("503"))
        working = self._catalogue(
            "deezer",
            [TrackMetadata(title="Survived", artist="A", provider="deezer")],
        )

        rows = suggest._from_catalogues(
            {"itunes": broken, "deezer": working}, make_context(), []
        )

        self.assertEqual([r.title for r in rows], ["Survived"])

    def test_both_are_given_the_same_seeds(self):
        # Not "whatever the other one had found by then": under concurrency
        # that would differ between runs of the same search.
        first = self._catalogue("itunes", [])
        second = self._catalogue("deezer", [])
        seeds = ["Lagyo Prityu No Rang Umesh Barot"]

        suggest._from_catalogues(
            {"itunes": first, "deezer": second}, make_context(), seeds
        )

        self.assertEqual(first.seen_seeds, seeds)
        self.assertEqual(second.seen_seeds, seeds)

    def test_a_single_catalogue_needs_no_pool(self):
        only = self._catalogue(
            "itunes", [TrackMetadata(title="Alone", artist="A", provider="itunes")]
        )
        with mock.patch.object(suggest.futures, "ThreadPoolExecutor") as pool:
            rows = suggest._from_catalogues({"itunes": only}, make_context(), [])

        pool.assert_not_called()
        self.assertEqual([r.title for r in rows], ["Alone"])

    @override_settings(ITUNES_ENABLED=False, DEEZER_ENABLED=False)
    def test_no_catalogues_available_is_not_an_error(self):
        self.assertEqual(suggest._from_catalogues({}, make_context(), []), [])

    def test_no_worker_threads_outlive_the_search(self):
        # The pool is a context manager, so nothing may still be running when
        # collect returns — this app keeps no background threads.
        before = {t.name for t in threading.enumerate()}
        suggest._from_catalogues(
            {"itunes": self._catalogue("itunes", []),
             "deezer": self._catalogue("deezer", [])},
            make_context(),
            [],
        )
        after = {t.name for t in threading.enumerate()}
        self.assertEqual({n for n in after - before if n.startswith("suggest")}, set())
