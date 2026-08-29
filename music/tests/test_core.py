"""Tests for the infrastructure primitives: events, locks, fileio, ratelimit, env.

Fully offline and deterministic. Where a test needs concurrency it synchronises
with events and barriers rather than sleeping and hoping; the only sleeps are
short, bounded pacing checks with loose lower bounds, so a slow machine makes
them pass harder, never flakier.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from music.core import events
from music.core.fileio import (
    atomic_write_text,
    hash_file,
    is_within,
    move_file,
    prune_empty_dirs,
    sanitize_component,
    unique_path,
)
from music.core.locks import KeyedLocks
from music.core.ratelimit import DailyBudget, RateLimiter
from music_manager import env

#: Generous ceiling for "this should not have blocked". Only ever an upper
#: bound on a code path that is supposed to return immediately.
PATIENCE = 10.0


# ==========================================================================
# events
# ==========================================================================


class EventsTests(SimpleTestCase):
    def setUp(self):
        events.reset_for_tests()
        self.addCleanup(events.reset_for_tests)

    def test_reset_clears_revision_and_topics(self):
        self.assertEqual(events.current(), 0)
        self.assertEqual(events.snapshot(), (0, frozenset()))

    def test_bump_increments_and_records_the_topic(self):
        self.assertEqual(events.bump("tracks"), 1)
        self.assertEqual(events.current(), 1)
        self.assertEqual(events.snapshot(), (1, frozenset({"tracks"})))

    def test_bump_accumulates_topics(self):
        events.bump("tracks")
        events.bump("jobs")
        events.bump("tracks")
        revision, topics = events.snapshot()
        self.assertEqual(revision, 3)
        self.assertEqual(topics, frozenset({"tracks", "jobs"}))

    def test_bump_defaults_to_the_state_topic(self):
        events.bump()
        self.assertEqual(events.snapshot()[1], frozenset({"state"}))

    def test_wait_returns_immediately_when_the_revision_already_moved(self):
        events.bump("jobs")
        started = time.monotonic()
        revision, topics = events.wait_for_change(since=0, timeout=PATIENCE)
        self.assertEqual(revision, 1)
        self.assertIn("jobs", topics)
        self.assertLess(time.monotonic() - started, PATIENCE)

    def test_wait_wakes_promptly_on_a_bump_from_another_thread(self):
        since = events.current()
        bumper = threading.Thread(target=events.bump, args=("tracks",), daemon=True)

        started = time.monotonic()
        bumper.start()
        revision, topics = events.wait_for_change(since=since, timeout=PATIENCE)
        elapsed = time.monotonic() - started
        bumper.join(timeout=PATIENCE)

        self.assertEqual(revision, since + 1)
        self.assertIn("tracks", topics)
        # Must not have waited out the timeout.
        self.assertLess(elapsed, PATIENCE / 2)

    def test_wait_returns_the_same_revision_on_timeout(self):
        # The keepalive case: nothing happened, so the caller emits a comment
        # frame and loops with the revision it already had.
        events.bump("jobs")
        since = events.current()

        started = time.monotonic()
        revision, topics = events.wait_for_change(since=since, timeout=0.1)
        elapsed = time.monotonic() - started

        self.assertEqual(revision, since)
        self.assertEqual(topics, frozenset({"jobs"}))
        self.assertGreaterEqual(elapsed, 0.05, "wait_for_change did not block")

    def test_one_bump_wakes_every_waiter(self):
        waiter_count = 5
        since = events.current()
        results: list[tuple[int, frozenset]] = []
        results_lock = threading.Lock()
        ready = threading.Barrier(waiter_count + 1, timeout=PATIENCE)

        def waiter():
            ready.wait()
            outcome = events.wait_for_change(since=since, timeout=PATIENCE)
            with results_lock:
                results.append(outcome)

        threads = [
            threading.Thread(target=waiter, daemon=True) for _ in range(waiter_count)
        ]
        for thread in threads:
            thread.start()
        ready.wait()

        events.bump("jobs")

        for thread in threads:
            thread.join(timeout=PATIENCE)
            self.assertFalse(thread.is_alive(), "a waiter was never woken")

        self.assertEqual(len(results), waiter_count)
        for revision, topics in results:
            self.assertEqual(revision, since + 1)
            self.assertIn("jobs", topics)

    def test_concurrent_bumps_are_not_lost(self):
        bump_count = 40
        threads = [
            threading.Thread(target=events.bump, args=("jobs",), daemon=True)
            for _ in range(bump_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=PATIENCE)
        self.assertEqual(events.current(), bump_count)


# ==========================================================================
# locks
# ==========================================================================


class KeyedLocksTests(SimpleTestCase):
    def setUp(self):
        self.locks = KeyedLocks()

    def test_uncontended_acquire_yields_true(self):
        with self.locks.acquire("track:1") as got:
            self.assertTrue(got)

    def test_two_threads_serialize_on_the_same_key(self):
        occupancy = 0
        peak = 0
        guard = threading.Lock()
        thread_count = 4
        ready = threading.Barrier(thread_count, timeout=PATIENCE)
        errors: list[BaseException] = []

        def worker():
            nonlocal occupancy, peak
            try:
                ready.wait()
                with self.locks.acquire("track:1") as got:
                    if not got:
                        raise AssertionError("blocking acquire returned False")
                    with guard:
                        occupancy += 1
                        peak = max(peak, occupancy)
                    time.sleep(0.02)
                    with guard:
                        occupancy -= 1
            except BaseException as exc:  # surfaced on the main thread below
                errors.append(exc)

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=PATIENCE)
            self.assertFalse(thread.is_alive())

        self.assertEqual(errors, [])
        self.assertEqual(peak, 1, "two holders were inside the same key at once")
        self.assertEqual(occupancy, 0)

    def test_different_keys_do_not_block_each_other(self):
        # If they blocked, the barrier inside the critical sections could never
        # be satisfied and would raise BrokenBarrierError on timeout.
        both_inside = threading.Barrier(2, timeout=2.0)
        results: dict[str, bool] = {}
        errors: list[BaseException] = []

        def worker(key):
            try:
                with self.locks.acquire(key) as got:
                    results[key] = got
                    both_inside.wait()
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(key,), daemon=True)
            for key in ("track:1", "track:2")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=PATIENCE)

        self.assertEqual(errors, [], "different keys blocked each other")
        self.assertEqual(results, {"track:1": True, "track:2": True})

    def test_timeout_zero_yields_false_while_held(self):
        held = threading.Event()
        release = threading.Event()
        outcome: list[bool] = []

        def holder():
            with self.locks.acquire("track:1"):
                held.set()
                release.wait(timeout=PATIENCE)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(timeout=PATIENCE))

        with self.locks.acquire("track:1", timeout=0) as got:
            outcome.append(got)

        release.set()
        thread.join(timeout=PATIENCE)

        self.assertEqual(outcome, [False])
        # And it is takeable again once the holder is gone.
        with self.locks.acquire("track:1", timeout=0) as got:
            self.assertTrue(got)

    def test_positive_timeout_expires_while_held(self):
        held = threading.Event()
        release = threading.Event()

        def holder():
            with self.locks.acquire("track:1"):
                held.set()
                release.wait(timeout=PATIENCE)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(timeout=PATIENCE))

        started = time.monotonic()
        with self.locks.acquire("track:1", timeout=0.1) as got:
            elapsed = time.monotonic() - started
            self.assertFalse(got)
        self.assertGreaterEqual(elapsed, 0.05, "the timeout was not honoured")

        release.set()
        thread.join(timeout=PATIENCE)

    def test_timeout_zero_succeeds_when_free(self):
        with self.locks.acquire("track:1", timeout=0) as got:
            self.assertTrue(got)

    def test_the_internal_dict_is_empty_again_after_release(self):
        self.assertEqual(self.locks.held_count(), 0)
        with self.locks.acquire("track:1"):
            self.assertEqual(self.locks.held_count(), 1)
        self.assertEqual(self.locks.held_count(), 0)

    def test_no_leak_across_many_keys(self):
        # Scanning a large library must not leave one lock object per track.
        for index in range(500):
            with self.locks.acquire(f"track:{index}") as got:
                self.assertTrue(got)
        self.assertEqual(self.locks.held_count(), 0)

    def test_no_leak_when_the_body_raises(self):
        with self.assertRaises(RuntimeError):
            with self.locks.acquire("track:1"):
                raise RuntimeError("boom")
        self.assertEqual(self.locks.held_count(), 0)
        with self.locks.acquire("track:1", timeout=0) as got:
            self.assertTrue(got)

    def test_no_leak_when_a_timed_acquire_fails(self):
        held = threading.Event()
        release = threading.Event()

        def holder():
            with self.locks.acquire("track:1"):
                held.set()
                release.wait(timeout=PATIENCE)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(timeout=PATIENCE))

        with self.locks.acquire("track:1", timeout=0) as got:
            self.assertFalse(got)
        # The holder still owns its entry, so exactly one remains.
        self.assertEqual(self.locks.held_count(), 1)

        release.set()
        thread.join(timeout=PATIENCE)
        self.assertEqual(self.locks.held_count(), 0)


# ==========================================================================
# fileio
# ==========================================================================


class SanitizeComponentTests(SimpleTestCase):
    def test_illegal_characters_become_underscores(self):
        for char in '<>:"/\\|?*':
            with self.subTest(char=char):
                self.assertEqual(sanitize_component(f"a{char}b"), "a_b")

    def test_nul_byte_is_replaced(self):
        self.assertEqual(sanitize_component("a\x00b"), "a_b")

    def test_control_characters_are_dropped(self):
        self.assertEqual(sanitize_component("a\x01\x02b\x1fc"), "abc")
        self.assertEqual(sanitize_component("line\nbreak\ttab"), "linebreaktab")

    def test_reserved_device_names_are_prefixed(self):
        for name in ("CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9"):
            with self.subTest(name=name):
                self.assertEqual(sanitize_component(name), f"_{name}")

    def test_reserved_check_is_case_insensitive_and_extension_aware(self):
        self.assertEqual(sanitize_component("con"), "_con")
        self.assertEqual(sanitize_component("NuL.mp3"), "_NuL.mp3")
        self.assertEqual(sanitize_component("aux.tar.gz"), "_aux.tar.gz")

    def test_names_containing_a_device_name_are_untouched(self):
        for name in ("CONCERT", "Console", "Auxiliary", "COM10", "LPT0", "PRNT"):
            with self.subTest(name=name):
                self.assertEqual(sanitize_component(name), name)

    def test_trailing_dots_and_spaces_are_stripped(self):
        self.assertEqual(sanitize_component("Album."), "Album")
        self.assertEqual(sanitize_component("Album..."), "Album")
        self.assertEqual(sanitize_component("Album   "), "Album")
        self.assertEqual(sanitize_component("Album . . "), "Album")
        self.assertEqual(sanitize_component("  Album  "), "Album")

    def test_internal_dots_are_kept(self):
        self.assertEqual(sanitize_component("Mr. Brightside"), "Mr. Brightside")
        self.assertEqual(sanitize_component("R.E.M"), "R.E.M")

    def test_unicode_is_truncated_on_a_character_boundary(self):
        name = "日本語のとても長いタイトル" * 40
        result = sanitize_component(name)
        self.assertLessEqual(len(result.encode("utf-8")), 200)
        self.assertTrue(name.startswith(result), "truncation split a codepoint")

    def test_ascii_is_truncated_to_the_byte_budget(self):
        result = sanitize_component("A" * 1000)
        self.assertEqual(len(result), 200)

    def test_short_names_are_not_truncated(self):
        self.assertEqual(sanitize_component("B" * 200), "B" * 200)

    def test_truncation_does_not_leave_a_trailing_dot(self):
        # Windows and exFAT drop a trailing dot at create time, so the file
        # would land at a path that never matches the computed one again.
        result = sanitize_component("A" * 199 + "." + "B" * 100)
        self.assertFalse(result.endswith("."))
        self.assertFalse(result.endswith(" "))

    def test_empty_and_blank_fall_back(self):
        self.assertEqual(sanitize_component(""), "Unknown")
        self.assertEqual(sanitize_component("   "), "Unknown")
        self.assertEqual(sanitize_component("..."), "Unknown")
        self.assertEqual(sanitize_component("\x01\x02"), "Unknown")
        self.assertEqual(sanitize_component(None), "Unknown")

    def test_custom_fallback(self):
        self.assertEqual(sanitize_component("", fallback="Unknown Artist"),
                         "Unknown Artist")

    # --- the regressions this sanitizer exists to avoid -----------------

    def test_parenthesised_text_is_preserved(self):
        # The old sanitizer stripped all bracketed text, losing "(Live)" and,
        # on an unmatched bracket, the whole rest of the title.
        self.assertEqual(sanitize_component("Hells Bells (Live)"),
                         "Hells Bells (Live)")
        self.assertEqual(sanitize_component("Song (Remastered 2011)"),
                         "Song (Remastered 2011)")
        self.assertEqual(sanitize_component("Track [Radio Edit]"),
                         "Track [Radio Edit]")
        self.assertEqual(sanitize_component("Unmatched (bracket"),
                         "Unmatched (bracket")

    def test_case_is_preserved(self):
        # The old sanitizer Title-Cased everything, turning ACDC into Acdc.
        self.assertEqual(sanitize_component("ACDC"), "ACDC")
        self.assertEqual(sanitize_component("AC/DC"), "AC_DC")
        self.assertEqual(sanitize_component("k.d. lang"), "k.d. lang")
        self.assertEqual(sanitize_component("MGMT"), "MGMT")
        self.assertEqual(sanitize_component("will.i.am"), "will.i.am")

    def test_ampersands_and_accents_survive(self):
        self.assertEqual(sanitize_component("Simon & Garfunkel"),
                         "Simon & Garfunkel")
        self.assertEqual(sanitize_component("Motörhead"), "Motörhead")
        self.assertEqual(sanitize_component("Sigur Rós"), "Sigur Rós")


class TempDirTestCase(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)


class HashFileTests(TempDirTestCase):
    def test_matches_hashlib(self):
        target = self.tmp / "audio.mp3"
        payload = b"ID3\x04\x00" + bytes(range(256)) * 4
        target.write_bytes(payload)
        self.assertEqual(hash_file(target), hashlib.sha1(payload).hexdigest())

    def test_supports_other_algorithms(self):
        target = self.tmp / "audio.mp3"
        payload = b"some bytes"
        target.write_bytes(payload)
        self.assertEqual(hash_file(target, algorithm="md5"),
                         hashlib.md5(payload).hexdigest())
        self.assertEqual(hash_file(target, algorithm="sha256"),
                         hashlib.sha256(payload).hexdigest())

    def test_empty_file(self):
        target = self.tmp / "empty.mp3"
        target.write_bytes(b"")
        self.assertEqual(hash_file(target), hashlib.sha1(b"").hexdigest())

    def test_reads_across_chunk_boundaries(self):
        target = self.tmp / "big.mp3"
        payload = (b"0123456789abcdef" * 65536) + b"tail"  # 1 MiB + 4 bytes
        target.write_bytes(payload)
        self.assertEqual(hash_file(target), hashlib.sha1(payload).hexdigest())

    def test_missing_file_returns_empty_string(self):
        self.assertEqual(hash_file(self.tmp / "nope.mp3"), "")

    def test_directory_returns_empty_string(self):
        self.assertEqual(hash_file(self.tmp), "")

    def test_accepts_a_string_path(self):
        target = self.tmp / "audio.mp3"
        target.write_bytes(b"x")
        self.assertEqual(hash_file(str(target)), hashlib.sha1(b"x").hexdigest())


class AtomicWriteTextTests(TempDirTestCase):
    def test_writes_content(self):
        target = self.tmp / "settings.env"
        atomic_write_text(target, "KEY=value\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "KEY=value\n")

    def test_replaces_existing_content_entirely(self):
        target = self.tmp / "settings.env"
        target.write_text("OLD=1\nSTALE=2\n", encoding="utf-8")
        atomic_write_text(target, "NEW=1\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "NEW=1\n")

    def test_leaves_no_temporary_files_behind(self):
        target = self.tmp / "settings.env"
        atomic_write_text(target, "A=1\n")
        atomic_write_text(target, "A=2\n")
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["settings.env"])

    def test_creates_missing_parent_directories(self):
        target = self.tmp / "deep" / "deeper" / "settings.env"
        atomic_write_text(target, "A=1\n")
        self.assertTrue(target.is_file())

    def test_unicode_round_trip(self):
        target = self.tmp / "notes.txt"
        text = "Motörhead — Ace of Spades\n日本語\n"
        atomic_write_text(target, text)
        self.assertEqual(target.read_text(encoding="utf-8"), text)

    def test_empty_write(self):
        target = self.tmp / "empty.txt"
        atomic_write_text(target, "")
        self.assertEqual(target.read_text(encoding="utf-8"), "")
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["empty.txt"])


class UniquePathTests(TempDirTestCase):
    def test_free_path_is_returned_unchanged(self):
        target = self.tmp / "song.mp3"
        self.assertEqual(unique_path(target), target)

    def test_taken_path_gets_a_two_suffix(self):
        target = self.tmp / "song.mp3"
        target.write_bytes(b"x")
        self.assertEqual(unique_path(target), self.tmp / "song (2).mp3")

    def test_counts_upwards(self):
        (self.tmp / "song.mp3").write_bytes(b"x")
        (self.tmp / "song (2).mp3").write_bytes(b"x")
        (self.tmp / "song (3).mp3").write_bytes(b"x")
        self.assertEqual(unique_path(self.tmp / "song.mp3"),
                         self.tmp / "song (4).mp3")

    def test_suffix_is_preserved(self):
        (self.tmp / "song.flac").write_bytes(b"x")
        self.assertEqual(unique_path(self.tmp / "song.flac").suffix, ".flac")

    def test_extensionless_name(self):
        (self.tmp / "song").write_bytes(b"x")
        self.assertEqual(unique_path(self.tmp / "song"), self.tmp / "song (2)")


class MoveFileTests(TempDirTestCase):
    def _source(self, name="source.mp3", payload=b"audio-bytes"):
        path = self.tmp / name
        path.write_bytes(payload)
        return path

    def test_same_directory_move(self):
        source = self._source()
        target = self.tmp / "moved.mp3"
        result = move_file(source, target)
        self.assertEqual(result, target)
        self.assertTrue(target.is_file())
        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), b"audio-bytes")

    def test_move_into_a_new_subdirectory(self):
        source = self._source()
        target = self.tmp / "Artist" / "Album" / "01 - Song.mp3"
        result = move_file(source, target)
        self.assertEqual(result, target)
        self.assertTrue(target.is_file())

    def test_concurrent_moves_to_one_destination_lose_nothing(self):
        """Two workers organizing tracks that compute the same path.

        `unique_path` checked `exists()` and the caller then called
        `os.replace`, which overwrites without complaint — so both threads
        picked the same free name and one file was destroyed silently. Measured
        before the fix: 12 moves in, 8 files out. The destination name is now
        reserved with O_CREAT|O_EXCL, which tests and creates in one syscall.
        """
        import threading

        count = 12
        sources = [self._source(f"src{i}.mp3", f"payload-{i}".encode())
                   for i in range(count)]
        destination = self.tmp / "collide" / "same.mp3"
        barrier = threading.Barrier(count)
        failures: list[BaseException] = []

        def move(source):
            barrier.wait()
            try:
                move_file(source, destination)
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = [threading.Thread(target=move, args=(s,)) for s in sources]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(failures, [])
        landed = sorted(p for p in (self.tmp / "collide").iterdir() if p.is_file())
        self.assertEqual(len(landed), count, "a concurrent move overwrote another")
        # Every payload survived exactly once: nothing was silently replaced.
        self.assertEqual(
            sorted(p.read_bytes() for p in landed),
            sorted(f"payload-{i}".encode() for i in range(count)),
        )
        for source in sources:
            self.assertFalse(source.exists())

    def test_does_not_clobber_an_existing_target(self):
        source = self._source(payload=b"new")
        target = self.tmp / "taken.mp3"
        target.write_bytes(b"original")

        result = move_file(source, target)

        self.assertEqual(result, self.tmp / "taken (2).mp3")
        self.assertEqual(target.read_bytes(), b"original", "the target was clobbered")
        self.assertEqual(result.read_bytes(), b"new")
        self.assertFalse(source.exists())

    def test_overwrite_replaces_the_target(self):
        source = self._source(payload=b"new")
        target = self.tmp / "taken.mp3"
        target.write_bytes(b"original")

        result = move_file(source, target, overwrite=True)

        self.assertEqual(result, target)
        self.assertEqual(target.read_bytes(), b"new")
        self.assertFalse(source.exists())

    def test_missing_source_raises(self):
        with self.assertRaises(FileNotFoundError):
            move_file(self.tmp / "nope.mp3", self.tmp / "target.mp3")

    def test_accepts_string_paths(self):
        source = self._source()
        target = self.tmp / "moved.mp3"
        self.assertEqual(move_file(str(source), str(target)), target)


class IsWithinTests(TempDirTestCase):
    def test_direct_child(self):
        self.assertTrue(is_within(self.tmp / "song.mp3", self.tmp))

    def test_nested_child(self):
        self.assertTrue(is_within(self.tmp / "a" / "b" / "song.mp3", self.tmp))

    def test_the_root_itself(self):
        self.assertTrue(is_within(self.tmp, self.tmp))

    def test_sibling_is_outside(self):
        sibling = self.tmp.parent / (self.tmp.name + "-sibling")
        self.assertFalse(is_within(sibling, self.tmp))

    def test_parent_is_outside(self):
        self.assertFalse(is_within(self.tmp.parent, self.tmp))

    def test_dot_dot_traversal_is_rejected(self):
        self.assertFalse(is_within(self.tmp / ".." / "escaped.mp3", self.tmp))
        self.assertFalse(is_within(self.tmp / "a" / ".." / ".." / "escaped.mp3",
                                   self.tmp))

    def test_traversal_that_stays_inside_is_accepted(self):
        self.assertTrue(is_within(self.tmp / "a" / ".." / "song.mp3", self.tmp))

    def test_accepts_string_paths(self):
        self.assertTrue(is_within(str(self.tmp / "song.mp3"), str(self.tmp)))


class PruneEmptyDirsTests(TempDirTestCase):
    def test_walks_up_removing_empties(self):
        deep = self.tmp / "Artist" / "Album" / "Disc 1"
        deep.mkdir(parents=True)

        removed = prune_empty_dirs(deep, self.tmp)

        self.assertEqual(removed, 3)
        self.assertFalse((self.tmp / "Artist").exists())
        self.assertTrue(self.tmp.is_dir(), "stop_at was deleted")

    def test_stops_at_the_first_non_empty_directory(self):
        deep = self.tmp / "Artist" / "Album" / "Disc 1"
        deep.mkdir(parents=True)
        (self.tmp / "Artist" / "keep.mp3").write_bytes(b"x")

        removed = prune_empty_dirs(deep, self.tmp)

        self.assertEqual(removed, 2)
        self.assertTrue((self.tmp / "Artist").is_dir())
        self.assertFalse((self.tmp / "Artist" / "Album").exists())

    def test_never_deletes_stop_at(self):
        removed = prune_empty_dirs(self.tmp, self.tmp)
        self.assertEqual(removed, 0)
        self.assertTrue(self.tmp.is_dir())

    def test_never_deletes_stop_at_even_when_it_is_empty_and_reached(self):
        child = self.tmp / "Only"
        child.mkdir()
        removed = prune_empty_dirs(child, self.tmp)
        self.assertEqual(removed, 1)
        self.assertFalse(child.exists())
        self.assertTrue(self.tmp.is_dir())

    def test_a_file_path_prunes_from_its_parent(self):
        deep = self.tmp / "Artist" / "Album"
        deep.mkdir(parents=True)
        removed = prune_empty_dirs(deep / "already-moved.mp3", self.tmp)
        self.assertEqual(removed, 2)
        self.assertTrue(self.tmp.is_dir())

    def test_a_directory_outside_stop_at_is_left_alone(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: outside.exists() and outside.rmdir())
        removed = prune_empty_dirs(outside, self.tmp)
        self.assertEqual(removed, 0)
        self.assertTrue(outside.is_dir(), "pruned outside the stop boundary")

    def test_a_non_empty_start_is_left_alone(self):
        deep = self.tmp / "Artist" / "Album"
        deep.mkdir(parents=True)
        (deep / "song.mp3").write_bytes(b"x")
        self.assertEqual(prune_empty_dirs(deep, self.tmp), 0)
        self.assertTrue(deep.is_dir())


# ==========================================================================
# ratelimit
# ==========================================================================


class RateLimiterTests(SimpleTestCase):
    def test_rejects_a_non_positive_rate(self):
        with self.assertRaises(ValueError):
            RateLimiter(0)
        with self.assertRaises(ValueError):
            RateLimiter(-1.0)

    def test_capacity_defaults_to_at_least_one(self):
        self.assertEqual(RateLimiter(0.05).capacity, 1.0)
        self.assertEqual(RateLimiter(3.0).capacity, 3.0)
        self.assertEqual(RateLimiter(3.0, burst=10).capacity, 10)

    def test_the_burst_is_available_immediately(self):
        limiter = RateLimiter(rate_per_sec=0.001, burst=5)
        started = time.monotonic()
        for _ in range(5):
            self.assertTrue(limiter.acquire(timeout=0))
        self.assertLess(time.monotonic() - started, 1.0, "the burst blocked")

    def test_non_blocking_acquire_is_false_once_the_bucket_is_empty(self):
        limiter = RateLimiter(rate_per_sec=0.001, burst=2)
        self.assertTrue(limiter.acquire(timeout=0))
        self.assertTrue(limiter.acquire(timeout=0))
        self.assertFalse(limiter.acquire(timeout=0))
        self.assertFalse(limiter.acquire(timeout=0))

    def test_a_short_timeout_expires_rather_than_hanging(self):
        limiter = RateLimiter(rate_per_sec=0.001, burst=1)
        self.assertTrue(limiter.acquire(timeout=0))
        started = time.monotonic()
        self.assertFalse(limiter.acquire(timeout=0.1))
        self.assertGreaterEqual(time.monotonic() - started, 0.05)

    def test_it_paces_once_the_burst_is_spent(self):
        # 25 tokens/sec, so three post-burst acquires cost ~0.12s of pacing.
        # Asserted with a loose lower bound: a slow machine only makes it
        # longer, never shorter.
        limiter = RateLimiter(rate_per_sec=25.0, burst=1)
        self.assertTrue(limiter.acquire(timeout=0))

        started = time.monotonic()
        for _ in range(3):
            self.assertTrue(limiter.acquire(timeout=PATIENCE))
        elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 0.06, "the limiter did not pace at all")

    def test_blocking_acquire_eventually_succeeds(self):
        limiter = RateLimiter(rate_per_sec=50.0, burst=1)
        self.assertTrue(limiter.acquire(timeout=0))
        self.assertTrue(limiter.acquire(timeout=PATIENCE))

    def test_tokens_are_not_double_spent_across_threads(self):
        # A negligible refill rate means the bucket cannot top up during the
        # run, so exactly `burst` acquires may succeed however they interleave.
        burst = 10
        limiter = RateLimiter(rate_per_sec=0.001, burst=burst)
        thread_count = 25
        ready = threading.Barrier(thread_count, timeout=PATIENCE)
        granted: list[bool] = []
        guard = threading.Lock()

        def worker():
            ready.wait()
            got = limiter.acquire(timeout=0)
            with guard:
                granted.append(got)

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=PATIENCE)

        self.assertEqual(len(granted), thread_count)
        self.assertEqual(sum(granted), burst)


class DailyBudgetTests(SimpleTestCase):
    def test_consumes_down_to_zero_then_refuses(self):
        budget = DailyBudget(3)
        self.assertTrue(budget.consume())
        self.assertTrue(budget.consume())
        self.assertTrue(budget.consume())
        self.assertFalse(budget.consume())
        self.assertFalse(budget.consume())

    def test_remaining_is_accurate(self):
        budget = DailyBudget(3)
        self.assertEqual(budget.remaining, 3)
        budget.consume()
        self.assertEqual(budget.remaining, 2)
        budget.consume()
        self.assertEqual(budget.remaining, 1)
        budget.consume()
        self.assertEqual(budget.remaining, 0)
        budget.consume()  # refused, must not go negative
        self.assertEqual(budget.remaining, 0)

    def test_limit_zero_always_refuses(self):
        budget = DailyBudget(0)
        self.assertFalse(budget.consume())
        self.assertFalse(budget.consume(1))
        self.assertEqual(budget.remaining, 0)

    def test_negative_limit_always_refuses(self):
        budget = DailyBudget(-5)
        self.assertFalse(budget.consume())
        self.assertEqual(budget.remaining, 0)

    def test_multi_unit_consumption_is_all_or_nothing(self):
        budget = DailyBudget(3)
        self.assertTrue(budget.consume(2))
        self.assertEqual(budget.remaining, 1)
        self.assertFalse(budget.consume(2), "a partial spend was allowed")
        self.assertEqual(budget.remaining, 1)
        self.assertTrue(budget.consume(1))
        self.assertEqual(budget.remaining, 0)

    def test_stats(self):
        budget = DailyBudget(5)
        budget.consume(2)
        self.assertEqual(budget.stats(), {"limit": 5, "used": 2, "remaining": 3})

    def test_a_new_day_resets_the_budget(self):
        budget = DailyBudget(2)
        self.assertTrue(budget.consume())
        self.assertTrue(budget.consume())
        self.assertFalse(budget.consume())

        # Deterministic stand-in for midnight: pretend the last use was
        # yesterday rather than waiting for the clock.
        budget._day = date.today() - timedelta(days=1)

        self.assertEqual(budget.remaining, 2)
        self.assertTrue(budget.consume())
        self.assertEqual(budget.remaining, 1)

    def test_concurrent_consumers_cannot_overspend(self):
        limit = 20
        budget = DailyBudget(limit)
        thread_count = 60
        ready = threading.Barrier(thread_count, timeout=PATIENCE)
        granted: list[bool] = []
        guard = threading.Lock()

        def worker():
            ready.wait()
            got = budget.consume()
            with guard:
                granted.append(got)

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=PATIENCE)

        self.assertEqual(sum(granted), limit)
        self.assertEqual(budget.remaining, 0)


# ==========================================================================
# music_manager.env
# ==========================================================================


@contextlib.contextmanager
def env_vars(**values):
    """Set the given variables for the block; a value of None unsets one."""
    missing = object()
    previous = {key: os.environ.get(key, missing) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, old in previous.items():
            if old is missing:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


NAME = "MM_TEST_VALUE"


class EnvBoolTests(SimpleTestCase):
    def test_truthy_spellings(self):
        for raw in ("1", "true", "TRUE", "True", "yes", "YES", "on", "ON", " true "):
            with self.subTest(raw=raw), env_vars(**{NAME: raw}):
                self.assertIs(env.env_bool(NAME), True)

    def test_falsey_spellings(self):
        for raw in ("0", "false", "FALSE", "no", "NO", "off", "OFF", "", "  "):
            with self.subTest(raw=raw), env_vars(**{NAME: raw}):
                self.assertIs(env.env_bool(NAME, default=True), False)

    def test_unset_uses_the_default(self):
        with env_vars(**{NAME: None}):
            self.assertIs(env.env_bool(NAME), False)
            self.assertIs(env.env_bool(NAME, default=True), True)

    def test_quoted_values_are_unquoted(self):
        for raw in ('"true"', "'true'"):
            with self.subTest(raw=raw), env_vars(**{NAME: raw}):
                self.assertIs(env.env_bool(NAME), True)

    def test_garbage_raises_with_a_helpful_message(self):
        with env_vars(**{NAME: "maybe"}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                env.env_bool(NAME)
        message = str(caught.exception)
        self.assertIn(NAME, message)
        self.assertIn("maybe", message)
        self.assertIn("not a boolean", message)
        self.assertIn("true", message)  # the message lists the accepted spellings

    def test_numeric_garbage_raises(self):
        with env_vars(**{NAME: "2"}):
            with self.assertRaises(ImproperlyConfigured):
                env.env_bool(NAME)


class EnvIntTests(SimpleTestCase):
    def test_plain_value(self):
        with env_vars(**{NAME: "7"}):
            self.assertEqual(env.env_int(NAME, default=1), 7)

    def test_a_float_spelling_is_accepted(self):
        with env_vars(**{NAME: "2.0"}):
            self.assertEqual(env.env_int(NAME, default=1), 2)
        with env_vars(**{NAME: "2.9"}):
            self.assertEqual(env.env_int(NAME, default=1), 2)

    def test_clamps_to_the_minimum_rather_than_raising(self):
        # A hand-edited WORKER_THREADS=0 must not be able to stop the pool.
        with env_vars(**{NAME: "0"}):
            self.assertEqual(env.env_int(NAME, default=1, minimum=1), 1)
        with env_vars(**{NAME: "-99"}):
            self.assertEqual(env.env_int(NAME, default=1, minimum=1), 1)

    def test_clamps_to_the_maximum_rather_than_raising(self):
        with env_vars(**{NAME: "999"}):
            self.assertEqual(env.env_int(NAME, default=1, maximum=8), 8)

    def test_the_default_is_clamped_too(self):
        with env_vars(**{NAME: None}):
            self.assertEqual(env.env_int(NAME, default=0, minimum=5), 5)

    def test_unset_and_empty_use_the_default(self):
        with env_vars(**{NAME: None}):
            self.assertEqual(env.env_int(NAME, default=42), 42)
        with env_vars(**{NAME: ""}):
            self.assertEqual(env.env_int(NAME, default=42), 42)

    def test_quoted_values_are_unquoted(self):
        with env_vars(**{NAME: '"5"'}):
            self.assertEqual(env.env_int(NAME, default=1), 5)

    def test_non_numeric_raises_with_a_helpful_message(self):
        with env_vars(**{NAME: "two"}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                env.env_int(NAME, default=1)
        message = str(caught.exception)
        self.assertIn(NAME, message)
        self.assertIn("two", message)
        self.assertIn("not a number", message)

    def test_non_finite_values_raise_rather_than_escaping(self):
        # int(float("inf")) raises OverflowError, not ValueError; letting it
        # escape would crash settings import with a bare traceback, which is
        # exactly what this module exists to prevent.
        for raw in ("inf", "-inf", "nan", "1e400"):
            with self.subTest(raw=raw), env_vars(**{NAME: raw}):
                with self.assertRaises(ImproperlyConfigured):
                    env.env_int(NAME, default=1, minimum=1, maximum=8)


class EnvFloatTests(SimpleTestCase):
    def test_plain_value(self):
        with env_vars(**{NAME: "2.5"}):
            self.assertEqual(env.env_float(NAME, default=1.0), 2.5)

    def test_clamps_to_the_minimum_rather_than_raising(self):
        # SSE_KEEPALIVE_SECONDS=0 must not be able to turn a wait into a spin.
        with env_vars(**{NAME: "0"}):
            self.assertEqual(env.env_float(NAME, default=25.0, minimum=1.0), 1.0)
        with env_vars(**{NAME: "-3"}):
            self.assertEqual(env.env_float(NAME, default=25.0, minimum=1.0), 1.0)

    def test_clamps_to_the_maximum_rather_than_raising(self):
        with env_vars(**{NAME: "5"}):
            self.assertEqual(env.env_float(NAME, default=0.5, maximum=1.0), 1.0)

    def test_unset_and_empty_use_the_default(self):
        with env_vars(**{NAME: None}):
            self.assertEqual(env.env_float(NAME, default=1.5), 1.5)
        with env_vars(**{NAME: ""}):
            self.assertEqual(env.env_float(NAME, default=1.5), 1.5)

    def test_non_numeric_raises_with_a_helpful_message(self):
        with env_vars(**{NAME: "fast"}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                env.env_float(NAME, default=1.0)
        message = str(caught.exception)
        self.assertIn(NAME, message)
        self.assertIn("fast", message)
        self.assertIn("not a number", message)

    def test_non_finite_values_raise(self):
        # nan defeats clamping outright: every comparison against it is False,
        # so a nan would sail past minimum= and end up inside a sleep().
        for raw in ("nan", "inf", "-inf", "1e400"):
            with self.subTest(raw=raw), env_vars(**{NAME: raw}):
                with self.assertRaises(ImproperlyConfigured):
                    env.env_float(NAME, default=1.0, minimum=0.5)


class EnvStrTests(SimpleTestCase):
    def test_plain_value_and_default(self):
        with env_vars(**{NAME: "hello"}):
            self.assertEqual(env.env_str(NAME, default="fallback"), "hello")
        with env_vars(**{NAME: None}):
            self.assertEqual(env.env_str(NAME, default="fallback"), "fallback")
        with env_vars(**{NAME: ""}):
            self.assertEqual(env.env_str(NAME, default="fallback"), "fallback")

    def test_no_default_gives_an_empty_string(self):
        with env_vars(**{NAME: None}):
            self.assertEqual(env.env_str(NAME), "")

    def test_quotes_and_whitespace_are_stripped(self):
        for raw in ('"value"', "'value'", "  value  ", '  "value"  '):
            with self.subTest(raw=raw), env_vars(**{NAME: raw}):
                self.assertEqual(env.env_str(NAME), "value")

    def test_an_unmatched_quote_is_left_alone(self):
        with env_vars(**{NAME: '"value'}):
            self.assertEqual(env.env_str(NAME), '"value')

    def test_choices_accepts_a_valid_value(self):
        with env_vars(**{NAME: "keep-both"}):
            self.assertEqual(
                env.env_str(NAME, choices=("report-only", "keep-best", "keep-both")),
                "keep-both",
            )

    def test_choices_rejects_an_invalid_value(self):
        with env_vars(**{NAME: "delete-everything"}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                env.env_str(NAME, choices=("report-only", "keep-best", "keep-both"))
        message = str(caught.exception)
        self.assertIn(NAME, message)
        self.assertIn("delete-everything", message)
        self.assertIn("report-only", message)
        self.assertIn("keep-both", message)

    def test_required_and_missing_raises_with_the_help_text(self):
        with env_vars(**{NAME: None}):
            with self.assertRaises(ImproperlyConfigured) as caught:
                env.env_str(NAME, required=True, help="Generate one with secrets.")
        message = str(caught.exception)
        self.assertIn(NAME, message)
        self.assertIn("required", message)
        self.assertIn(".env", message)
        self.assertIn("Generate one with secrets.", message)

    def test_required_and_empty_raises(self):
        with env_vars(**{NAME: ""}):
            with self.assertRaises(ImproperlyConfigured):
                env.env_str(NAME, required=True)


class EnvListTests(SimpleTestCase):
    def test_splits_on_commas(self):
        with env_vars(**{NAME: "a,b,c"}):
            self.assertEqual(env.env_list(NAME), ["a", "b", "c"])

    def test_trims_whitespace_and_drops_empties(self):
        with env_vars(**{NAME: " a , ,b, "}):
            self.assertEqual(env.env_list(NAME), ["a", "b"])

    def test_single_value(self):
        with env_vars(**{NAME: "only"}):
            self.assertEqual(env.env_list(NAME), ["only"])

    def test_unset_and_empty_use_the_default(self):
        with env_vars(**{NAME: None}):
            self.assertEqual(env.env_list(NAME, default=["x"]), ["x"])
            self.assertEqual(env.env_list(NAME), [])
        with env_vars(**{NAME: ""}):
            self.assertEqual(env.env_list(NAME, default=["x"]), ["x"])

    def test_the_default_is_copied_not_aliased(self):
        default = ["x"]
        with env_vars(**{NAME: None}):
            result = env.env_list(NAME, default=default)
        result.append("y")
        self.assertEqual(default, ["x"])

    def test_separator_none_splits_on_pathsep_and_commas(self):
        raw = f"/music/a{os.pathsep}/music/b,/music/c"
        with env_vars(**{NAME: raw}):
            self.assertEqual(
                env.env_list(NAME, separator=None),
                ["/music/a", "/music/b", "/music/c"],
            )

    def test_separator_none_still_handles_a_single_path(self):
        with env_vars(**{NAME: "/music/only"}):
            self.assertEqual(env.env_list(NAME, separator=None), ["/music/only"])

    def test_custom_separator(self):
        with env_vars(**{NAME: "a;b;c"}):
            self.assertEqual(env.env_list(NAME, separator=";"), ["a", "b", "c"])

    def test_quoted_values_are_unquoted(self):
        with env_vars(**{NAME: '"a,b"'}):
            self.assertEqual(env.env_list(NAME), ["a", "b"])
