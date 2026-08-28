"""
Tests for the library package: tag IO, scanning, organizing.

These run against **real files in a temporary directory**, not mocks. The
synthetic MP3s below are genuine MPEG-1 Layer III frames, so mutagen parses
them, writes ID3 into them and reads it back — which is the only way to catch
the kind of bug this package can actually have (a frame written twice, a tag
that does not survive a move, a size/mtime that stops a rescan working). The
one thing that is mocked is `mutagen.mp3.MP3.save`, and only to *count* calls
while still saving for real, because "exactly one save" is a requirement rather
than an implementation detail (see `tagio`).

Nothing here touches the network, the job queue or the real library.
"""

from __future__ import annotations

import struct
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from music.core.locks import track_locks
from music.identify.base import TrackMetadata
from music.library import organizer, scanner, tagio
from music.models import ScanRoot, Source, Track, TrackState

#: One MPEG-1 Layer III frame header (128 kbps, 44.1 kHz, no padding) plus its
#: 413 bytes of payload. Repeated, this is a file mutagen accepts as an MP3.
_MP3_FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 413

#: Enough of a PNG signature for the cover-art sniffer; ID3 does not validate.
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def write_mp3(path: Path, frames: int = 16) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_MP3_FRAME * frames)
    return path


def write_flac(path: Path) -> Path:
    """A FLAC file with a real STREAMINFO block and no audio frames.

    Enough for mutagen to parse, tag and re-save, which is what the Vorbis
    comment and picture-block code paths need — and FLAC is the format most
    likely to turn up in an existing library after MP3.
    """
    packed = (44100 << 44) | ((2 - 1) << 41) | ((16 - 1) << 36) | 44100
    streaminfo = (
        struct.pack(">H", 4096)  # minimum block size
        + struct.pack(">H", 4096)  # maximum block size
        + b"\x00\x00\x00"  # minimum frame size (unknown)
        + b"\x00\x00\x00"  # maximum frame size (unknown)
        + struct.pack(">Q", packed)  # rate, channels, bits, total samples
        + b"\x00" * 16  # md5 of the unencoded audio
    )
    header = bytes([0x80]) + len(streaminfo).to_bytes(3, "big")  # last block, type 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + header + streaminfo)
    return path


class LibraryTestCase(TestCase):
    """A temporary library root plus one incoming directory, per test."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Resolved once: everything under test compares resolved paths, and on
        # Windows the temp directory can arrive as an 8.3 short name.
        self.root = Path(temporary.name).resolve()
        self.library = self.root / "library"
        self.incoming = self.root / "incoming"
        self.library.mkdir()
        self.incoming.mkdir()

        overrides = override_settings(
            LIBRARY_ROOT=self.library,
            SCAN_ROOTS=[self.incoming],
            DOWNLOAD_STAGING=self.root / "staging",
            DUPLICATE_POLICY="report-only",
        )
        overrides.enable()
        self.addCleanup(overrides.disable)

    def make_track(self, path: Path, **fields) -> Track:
        stat = path.stat() if path.exists() else None
        values = {
            "title": "Comfortably Numb",
            "artist": "Pink Floyd",
            "album": "The Wall",
            "track_no": 6,
            "state": TrackState.IDENTIFIED,
            "source": Source.LIBRARY,
            "size_bytes": stat.st_size if stat else 0,
            "mtime": stat.st_mtime if stat else 0.0,
        }
        values.update(fields)
        return Track.objects.create(path=str(path), **values)


# --------------------------------------------------------------------------
# tagio
# --------------------------------------------------------------------------


class TagIOTests(LibraryTestCase):
    def test_round_trip_writes_every_field(self):
        path = write_mp3(self.incoming / "song.mp3")
        meta = TrackMetadata(
            title="Comfortably Numb",
            artist="Pink Floyd",
            album="The Wall",
            album_artist="Various Artists",
            track_no=6,
            disc_no=2,
            year=1979,
            genre="Progressive Rock",
            is_compilation=True,
            musicbrainz_recording_id="rec-123",
            musicbrainz_release_id="rel-456",
        )

        tagio.write_tags(path, meta)
        read_back = tagio.read_tags(path)

        self.assertEqual(read_back.title, "Comfortably Numb")
        self.assertEqual(read_back.artist, "Pink Floyd")
        self.assertEqual(read_back.album, "The Wall")
        self.assertEqual(read_back.album_artist, "Various Artists")
        self.assertEqual(read_back.track_no, 6)
        # The disc number matters more than the rest put together: Plex reads
        # it from the tag, not from the filename (music/plex.py).
        self.assertEqual(read_back.disc_no, 2)
        self.assertEqual(read_back.year, 1979)
        self.assertEqual(read_back.genre, "Progressive Rock")
        self.assertTrue(read_back.is_compilation)
        self.assertEqual(read_back.musicbrainz_recording_id, "rec-123")
        self.assertEqual(read_back.musicbrainz_release_id, "rel-456")

    def test_tags_and_cover_art_are_written_in_a_single_save(self):
        """The audit's RPi concern: the old tagger rewrote the file twice."""
        from mutagen.mp3 import MP3

        path = write_mp3(self.incoming / "song.mp3")
        original_save = MP3.save
        saves: list[str] = []

        def counting_save(self, *args, **kwargs):
            saves.append(self.filename)
            return original_save(self, *args, **kwargs)

        with mock.patch.object(MP3, "save", counting_save):
            tagio.write_tags(
                path,
                TrackMetadata(title="Song", artist="Artist", album="Album"),
                cover=_PNG,
            )

        self.assertEqual(
            len(saves), 1, "text tags and cover art must cost one file rewrite"
        )

        from mutagen.id3 import ID3

        pictures = ID3(str(path)).getall("APIC")
        self.assertEqual(len(pictures), 1)
        self.assertEqual(pictures[0].mime, "image/png")
        self.assertEqual(pictures[0].data, _PNG)
        self.assertEqual(tagio.read_tags(path).title, "Song")

    def test_compilation_flag_is_cleared_when_false(self):
        path = write_mp3(self.incoming / "song.mp3")
        tagio.write_tags(path, TrackMetadata(title="S", artist="A", is_compilation=True))
        self.assertTrue(tagio.read_tags(path).is_compilation)

        # A stale flag files the whole album under Various Artists in Plex, so
        # False has to actively remove it rather than merely not set it.
        tagio.write_tags(path, TrackMetadata(title="S", artist="A", is_compilation=False))
        self.assertFalse(tagio.read_tags(path).is_compilation)

    def test_unreadable_file_yields_empty_metadata_without_raising(self):
        path = self.incoming / "not-really-audio.mp3"
        path.write_text("<html>404 Not Found</html>", encoding="utf-8")

        metadata = tagio.read_tags(path)

        self.assertEqual(metadata, TrackMetadata())
        self.assertEqual(tagio.read_audio_properties(path), (0, 0))

    def test_audio_properties_are_integers_and_never_none(self):
        path = write_mp3(self.incoming / "song.mp3", frames=200)

        duration, bitrate = tagio.read_audio_properties(path)

        self.assertIsInstance(duration, int)
        self.assertIsInstance(bitrate, int)
        self.assertEqual(bitrate, 128, "bitrate is stored in kbps, not bits/second")

    def test_missing_file_is_not_an_exception(self):
        self.assertEqual(tagio.read_tags(self.incoming / "gone.mp3"), TrackMetadata())

    def test_flac_round_trips_through_vorbis_comments_in_one_save(self):
        from mutagen.flac import FLAC

        path = write_flac(self.incoming / "song.flac")
        original_save = FLAC.save
        saves: list[int] = []

        def counting_save(self, *args, **kwargs):
            saves.append(1)
            return original_save(self, *args, **kwargs)

        meta = TrackMetadata(
            title="Numb",
            artist="Floyd",
            album="Wall",
            album_artist="Various Artists",
            track_no=6,
            disc_no=2,
            year=1979,
            is_compilation=True,
            musicbrainz_recording_id="rec-1",
        )
        with mock.patch.object(FLAC, "save", counting_save):
            tagio.write_tags(path, meta, cover=_PNG)

        self.assertEqual(len(saves), 1)
        read_back = tagio.read_tags(path)
        self.assertEqual(read_back.title, "Numb")
        self.assertEqual(read_back.album_artist, "Various Artists")
        self.assertEqual(read_back.disc_no, 2)
        self.assertEqual(read_back.year, 1979)
        self.assertTrue(read_back.is_compilation)
        self.assertEqual(read_back.musicbrainz_recording_id, "rec-1")

        pictures = FLAC(str(path)).pictures
        self.assertEqual(len(pictures), 1)
        self.assertEqual(pictures[0].mime, "image/png")

        tagio.write_tags(path, TrackMetadata(title="Numb", artist="Floyd"))
        self.assertFalse(tagio.read_tags(path).is_compilation)

    def test_writing_to_a_corrupt_file_raises_rather_than_pretending(self):
        """The caller is about to move this file; it has to know."""
        path = self.incoming / "broken.mp3"
        path.write_text("this is not an MP3", encoding="utf-8")

        with self.assertRaises(tagio.TagWriteError):
            tagio.write_tags(path, TrackMetadata(title="Song", artist="Artist"))

    def test_an_unavailable_mutagen_disables_tagging_not_the_app(self):
        path = write_mp3(self.incoming / "song.mp3")

        with mock.patch.object(tagio, "_mutagen", return_value=None):
            self.assertFalse(tagio.tagging_available())
            self.assertEqual(tagio.read_tags(path), TrackMetadata())
            self.assertEqual(tagio.read_audio_properties(path), (0, 0))
            # A no-op, not an exception: an unusable optional dependency must
            # not stop the file being scanned, planned and organized.
            tagio.write_tags(path, TrackMetadata(title="Song", artist="Artist"))

        self.assertEqual(tagio.read_tags(path), TrackMetadata())


# --------------------------------------------------------------------------
# scanner
# --------------------------------------------------------------------------


class ScannerTests(LibraryTestCase):
    def test_adds_new_audio_files_and_ignores_everything_else(self):
        write_mp3(self.incoming / "one.mp3")
        write_mp3(self.incoming / "nested" / "two.mp3")
        (self.incoming / "cover.jpg").write_bytes(b"not audio")
        (self.incoming / "notes.txt").write_text("hello", encoding="utf-8")

        result = scanner.scan_root(self.incoming)

        self.assertEqual(result.seen, 2)
        self.assertEqual(result.added, 2)
        self.assertEqual(result.errors, 0)
        self.assertEqual(Track.objects.count(), 2)

        track = Track.objects.get(path=str(self.incoming / "one.mp3"))
        self.assertEqual(track.state, TrackState.DISCOVERED)
        self.assertEqual(track.source, Source.LIBRARY)
        self.assertGreater(track.size_bytes, 0)
        self.assertGreater(track.mtime, 0)
        self.assertEqual(track.bitrate, 128)

    def test_reads_tags_into_the_row(self):
        path = write_mp3(self.incoming / "one.mp3")
        tagio.write_tags(
            path,
            TrackMetadata(title="Numb", artist="Floyd", album="Wall", track_no=6, disc_no=2),
        )

        scanner.scan_root(self.incoming)

        track = Track.objects.get(path=str(path))
        self.assertEqual(track.title, "Numb")
        self.assertEqual(track.artist, "Floyd")
        self.assertEqual(track.album, "Wall")
        self.assertEqual(track.track_no, 6)
        self.assertEqual(track.disc_no, 2)

    def test_rescan_of_an_unchanged_library_does_no_tag_io(self):
        """The property that makes a scheduled rescan affordable on a Pi."""
        write_mp3(self.incoming / "one.mp3")
        write_mp3(self.incoming / "two.mp3")
        scanner.scan_root(self.incoming)

        with mock.patch.object(
            scanner.tagio, "read_metadata", wraps=scanner.tagio.read_metadata
        ) as reader:
            result = scanner.scan_root(self.incoming)

        self.assertEqual(result.skipped, 2)
        self.assertEqual(result.added, 0)
        self.assertEqual(result.updated, 0)
        reader.assert_not_called()
        self.assertEqual(Track.objects.count(), 2)

    def test_a_changed_file_is_re_read_and_returned_to_the_pipeline(self):
        path = write_mp3(self.incoming / "one.mp3")
        scanner.scan_root(self.incoming)
        Track.objects.filter(path=str(path)).update(state=TrackState.ORGANIZED)

        write_mp3(path, frames=64)  # different size => genuinely changed
        result = scanner.scan_root(self.incoming)

        self.assertEqual(result.updated, 1)
        self.assertEqual(result.added, 0)
        track = Track.objects.get(path=str(path))
        self.assertEqual(track.state, TrackState.DISCOVERED)
        self.assertEqual(track.size_bytes, path.stat().st_size)

    def test_a_vanished_file_is_marked_missing_and_not_deleted(self):
        path = write_mp3(self.incoming / "one.mp3")
        scanner.scan_root(self.incoming)
        path.unlink()

        result = scanner.scan_root(self.incoming)

        self.assertEqual(result.seen, 0)
        self.assertEqual(result.missing, 1)
        track = Track.objects.get(path=str(path))
        self.assertEqual(track.state, TrackState.MISSING)

    def test_a_file_that_comes_back_is_picked_up_again(self):
        path = write_mp3(self.incoming / "one.mp3")
        scanner.scan_root(self.incoming)
        Track.objects.filter(path=str(path)).update(state=TrackState.MISSING)

        result = scanner.scan_root(self.incoming)

        self.assertEqual(result.updated, 1)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(
            Track.objects.get(path=str(path)).state, TrackState.DISCOVERED
        )

    def test_hidden_and_duplicate_directories_are_skipped(self):
        write_mp3(self.incoming / "keep.mp3")
        write_mp3(self.incoming / organizer.DUPLICATES_DIRNAME / "parked.mp3")
        write_mp3(self.incoming / ".Trash-1000" / "deleted.mp3")
        write_mp3(self.incoming / "._appledouble.mp3")

        result = scanner.scan_root(self.incoming)

        self.assertEqual(result.seen, 1)
        self.assertEqual(
            list(Track.objects.values_list("path", flat=True)),
            [str(self.incoming / "keep.mp3")],
        )

    def test_heartbeat_is_called_so_a_long_scan_can_extend_its_lease(self):
        for index in range(5):
            write_mp3(self.incoming / f"track{index}.mp3")
        beats: list[int] = []

        with mock.patch.object(scanner, "HEARTBEAT_EVERY", 2):
            scanner.scan_root(self.incoming, heartbeat=lambda: beats.append(1))

        self.assertGreaterEqual(len(beats), 2)

    def test_scan_all_records_bookkeeping_on_every_enabled_root(self):
        write_mp3(self.incoming / "one.mp3")
        ScanRoot.objects.create(path=str(self.incoming), enabled=True)
        ScanRoot.objects.create(path=str(self.root / "disabled"), enabled=False)

        results = scanner.scan_all()

        self.assertEqual(list(results), [str(self.incoming)])
        row = ScanRoot.objects.get(path=str(self.incoming))
        self.assertEqual(row.files_seen, 1)
        self.assertEqual(row.files_added, 1)
        self.assertEqual(row.last_error, "")
        self.assertIsNotNone(row.last_scan_started_at)
        self.assertIsNotNone(row.last_scan_finished_at)

    def test_a_missing_root_is_an_error_not_a_crash(self):
        result = scanner.scan_root(self.root / "not-there")

        self.assertEqual(result.errors, 1)
        self.assertEqual(result.seen, 0)


# --------------------------------------------------------------------------
# organizer — planning
# --------------------------------------------------------------------------


class OrganizerPlanTests(LibraryTestCase):
    def test_plan_computes_the_plex_path(self):
        path = write_mp3(self.incoming / "whatever.mp3")
        track = self.make_track(path)

        note = organizer.plan_track(track)

        expected = self.library / "Pink Floyd" / "The Wall" / "06 - Comfortably Numb.mp3"
        self.assertEqual(Path(track.planned_path), expected)
        self.assertIn("move to", note)
        self.assertTrue(track.needs_move)

    def test_plan_refuses_a_track_with_no_metadata(self):
        path = write_mp3(self.incoming / "whatever.mp3")
        track = self.make_track(path, title="", artist="", album_artist="")

        note = organizer.plan_track(track)

        self.assertEqual(track.planned_path, "")
        self.assertIn("metadata", note)

    def test_plan_marks_an_already_organized_file_as_in_place(self):
        destination = self.library / "Pink Floyd" / "The Wall" / "06 - Comfortably Numb.mp3"
        write_mp3(destination)
        track = self.make_track(destination)

        note = organizer.plan_track(track)

        self.assertEqual(note, "already in place")
        self.assertEqual(Path(track.planned_path), destination)
        self.assertFalse(track.needs_move)

    def test_hostile_metadata_cannot_escape_the_library_root(self):
        path = write_mp3(self.incoming / "whatever.mp3")
        track = self.make_track(path, artist="../../../etc", album="../..", title="x")

        organizer.plan_track(track)

        self.assertTrue(
            organizer.is_within(Path(track.planned_path), self.library),
            f"{track.planned_path} escaped {self.library}",
        )

    def test_plan_all_counts_each_outcome(self):
        movable = self.make_track(write_mp3(self.incoming / "a.mp3"))
        self.make_track(
            write_mp3(self.incoming / "b.mp3"), title="", artist="", album_artist=""
        )
        in_place = self.library / "Pink Floyd" / "The Wall" / "06 - Comfortably Numb.mp3"
        write_mp3(in_place)
        self.make_track(in_place, title="Comfortably Numb")

        stats = organizer.plan_all()

        self.assertEqual(stats["planned"], 1)
        self.assertEqual(stats["in_place"], 1)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["errors"], 0)
        movable.refresh_from_db()
        self.assertTrue(movable.planned_path)


# --------------------------------------------------------------------------
# organizer — applying
# --------------------------------------------------------------------------


class OrganizerApplyTests(LibraryTestCase):
    def test_apply_moves_the_file_and_records_where_it_came_from(self):
        source = write_mp3(self.incoming / "artist - song.mp3")
        track = self.make_track(source)
        organizer.plan_track(track)

        destination = organizer.apply_track(track)

        expected = self.library / "Pink Floyd" / "The Wall" / "06 - Comfortably Numb.mp3"
        self.assertEqual(destination, expected)
        self.assertTrue(expected.is_file())
        self.assertFalse(source.exists())

        track.refresh_from_db()
        self.assertEqual(track.path, str(expected))
        self.assertEqual(track.previous_path, str(source))
        self.assertEqual(track.state, TrackState.ORGANIZED)
        self.assertIsNotNone(track.organized_at)
        self.assertEqual(track.planned_path, "")

    def test_apply_refuses_a_destination_outside_the_library_root(self):
        source = write_mp3(self.incoming / "song.mp3")
        escape = self.root / "elsewhere" / "song.mp3"
        track = self.make_track(source, planned_path=str(escape))

        with self.assertRaises(organizer.OrganizeError) as caught:
            organizer.apply_track(track)

        self.assertIn("outside", str(caught.exception))
        self.assertTrue(source.is_file(), "the file must not have moved")
        self.assertFalse(escape.exists())
        track.refresh_from_db()
        self.assertNotEqual(track.state, TrackState.ORGANIZED)

    def test_apply_refuses_a_track_with_no_plan_and_no_metadata(self):
        source = write_mp3(self.incoming / "song.mp3")
        track = self.make_track(source, title="", artist="", album_artist="")

        with self.assertRaises(organizer.OrganizeError):
            organizer.apply_track(track)

        self.assertTrue(source.is_file())

    def test_an_already_organized_file_is_not_moved(self):
        destination = self.library / "Pink Floyd" / "The Wall" / "06 - Comfortably Numb.mp3"
        write_mp3(destination)
        track = self.make_track(destination)
        before = destination.stat().st_mtime_ns

        result = organizer.apply_track(track)

        self.assertEqual(result, destination)
        self.assertEqual(destination.stat().st_mtime_ns, before, "file was rewritten")
        track.refresh_from_db()
        self.assertEqual(track.state, TrackState.ORGANIZED)
        self.assertEqual(track.previous_path, "")

    def test_apply_marks_a_vanished_file_missing_instead_of_moving_it(self):
        source = self.incoming / "gone.mp3"
        track = self.make_track(source)

        with self.assertRaises(organizer.OrganizeError):
            organizer.apply_track(track)

        track.refresh_from_db()
        self.assertEqual(track.state, TrackState.MISSING)

    def test_apply_writes_the_tags_before_it_moves_the_file(self):
        source = write_mp3(self.incoming / "song.mp3")
        track = self.make_track(source, disc_no=2, is_compilation=True)
        organizer.plan_track(track)
        seen: list[tuple[str, bool]] = []

        real_write = organizer.tagio.write_tags

        def recording_write(path, meta, **kwargs):
            seen.append((str(path), Path(path).is_file()))
            return real_write(path, meta, **kwargs)

        with mock.patch.object(organizer.tagio, "write_tags", recording_write):
            destination = organizer.apply_track(track)

        self.assertEqual(seen, [(str(source), True)])
        written = tagio.read_tags(destination)
        self.assertEqual(written.disc_no, 2)
        # A compilation is filed under Various Artists, and the tag has to say
        # so for Plex to agree with the folder.
        self.assertEqual(written.album_artist, "Various Artists")

    def test_apply_can_be_called_with_the_track_lock_already_held(self):
        """The job handler takes the lock, then calls in here — see core/locks."""
        source = write_mp3(self.incoming / "song.mp3")
        track = self.make_track(source)
        organizer.plan_track(track)

        with track_locks.acquire(f"track:{track.pk}") as outer:
            self.assertTrue(outer)
            # Checked without blocking first, so a non-reentrant lock fails this
            # assertion instead of wedging the test run forever.
            with track_locks.acquire(f"track:{track.pk}", timeout=0) as nested:
                self.assertTrue(
                    nested,
                    "track locks must be re-entrant or apply_track deadlocks "
                    "under the organize.track handler",
                )
            destination = organizer.apply_track(track)

        self.assertTrue(destination.is_file())

    def test_apply_leaves_the_row_matching_the_file_so_a_rescan_skips_it(self):
        """Without this the library re-identifies itself on every scan."""
        source = write_mp3(self.incoming / "song.mp3")
        track = self.make_track(source)
        organizer.plan_track(track)
        destination = organizer.apply_track(track)

        track.refresh_from_db()
        self.assertEqual(track.size_bytes, destination.stat().st_size)
        self.assertAlmostEqual(track.mtime, destination.stat().st_mtime, places=3)

        ScanRoot.objects.create(path=str(self.library))
        result = scanner.scan_root(self.library)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.updated, 0)
        self.assertEqual(result.added, 0)

    def test_apply_prunes_the_directory_the_move_emptied(self):
        source = write_mp3(self.incoming / "some album" / "song.mp3")
        track = self.make_track(source)
        organizer.plan_track(track)

        organizer.apply_track(track)

        self.assertFalse((self.incoming / "some album").exists())
        self.assertTrue(self.incoming.is_dir(), "must never prune past the scan root")

    def test_apply_all_moves_every_planned_track(self):
        for index in range(3):
            track = self.make_track(
                write_mp3(self.incoming / f"song{index}.mp3"),
                title=f"Song {index}",
                track_no=index + 1,
            )
            organizer.plan_track(track)

        stats = organizer.apply_all()

        self.assertEqual(stats["moved"], 3)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(
            Track.objects.filter(state=TrackState.ORGANIZED).count(), 3
        )


class OrganizerRevertTests(LibraryTestCase):
    def test_revert_restores_the_original_path(self):
        source = write_mp3(self.incoming / "original name.mp3")
        track = self.make_track(source)
        organizer.plan_track(track)
        destination = organizer.apply_track(track)

        restored = organizer.revert_track(track)

        self.assertEqual(restored, source)
        self.assertTrue(source.is_file())
        self.assertFalse(destination.exists())

        track.refresh_from_db()
        self.assertEqual(track.path, str(source))
        self.assertEqual(track.previous_path, "")
        self.assertIsNone(track.organized_at)
        self.assertNotEqual(track.state, TrackState.ORGANIZED)
        # The plan is left pointing at where it was, so re-applying is one step.
        self.assertEqual(Path(track.planned_path), destination)

    def test_revert_without_a_previous_path_is_refused(self):
        track = self.make_track(write_mp3(self.incoming / "song.mp3"))

        with self.assertRaises(organizer.OrganizeError):
            organizer.revert_track(track)


# --------------------------------------------------------------------------
# organizer — duplicates
# --------------------------------------------------------------------------


class DuplicatePolicyTests(LibraryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.destination = (
            self.library / "Pink Floyd" / "The Wall" / "06 - Comfortably Numb.mp3"
        )
        write_mp3(self.destination, frames=8)
        self.incumbent = self.make_track(
            self.destination, bitrate=128, state=TrackState.ORGANIZED
        )
        self.source = write_mp3(self.incoming / "song.mp3", frames=32)
        self.track = self.make_track(self.source, bitrate=320)
        organizer.plan_track(self.track)

    @override_settings(DUPLICATE_POLICY="report-only")
    def test_report_only_changes_nothing(self):
        result = organizer.apply_track(self.track)

        self.assertEqual(result, self.source)
        self.assertTrue(self.source.is_file())
        self.assertTrue(self.destination.is_file())
        self.track.refresh_from_db()
        self.assertNotEqual(self.track.state, TrackState.ORGANIZED)
        self.assertIn("report-only", self.track.plan_note)

    @override_settings(DUPLICATE_POLICY="keep-both")
    def test_keep_both_disambiguates_the_new_file(self):
        result = organizer.apply_track(self.track)

        self.assertEqual(result.name, "06 - Comfortably Numb (2).mp3")
        self.assertTrue(self.destination.is_file(), "the incumbent must be untouched")
        self.assertTrue(result.is_file())

    @override_settings(DUPLICATE_POLICY="keep-best")
    def test_keep_best_parks_the_lower_bitrate_copy(self):
        result = organizer.apply_track(self.track)

        self.assertEqual(result, self.destination)

        parked = list(
            (self.library / organizer.DUPLICATES_DIRNAME).rglob("*.mp3")
        )
        self.assertEqual(len(parked), 1, "the loser is moved aside, never deleted")
        # The parked file is the incumbent, byte for byte: it was moved, not
        # rewritten. The winner is larger because apply wrote its tags.
        self.assertEqual(parked[0].stat().st_size, 8 * len(_MP3_FRAME))
        self.assertGreater(result.stat().st_size, parked[0].stat().st_size)

        self.incumbent.refresh_from_db()
        self.assertEqual(self.incumbent.path, str(parked[0]))
        self.assertEqual(self.incumbent.state, TrackState.SKIPPED)
        self.assertEqual(self.incumbent.previous_path, str(self.destination))

    @override_settings(DUPLICATE_POLICY="keep-best")
    def test_keep_best_parks_our_own_copy_when_it_loses(self):
        Track.objects.filter(pk=self.track.pk).update(bitrate=64)
        self.track.refresh_from_db()

        result = organizer.apply_track(self.track)

        self.assertTrue(result.is_file())
        self.assertIn(organizer.DUPLICATES_DIRNAME, str(result))
        self.assertTrue(self.destination.is_file())
        self.incumbent.refresh_from_db()
        self.assertEqual(self.incumbent.path, str(self.destination))
        self.track.refresh_from_db()
        self.assertEqual(self.track.state, TrackState.SKIPPED)


class FindDuplicatesTests(LibraryTestCase):
    def test_groups_by_content_hash(self):
        # Distinct metadata on purpose, so only the hash can group these.
        first = self.make_track(
            self.incoming / "a.mp3", title="A", track_no=1, content_hash="deadbeef"
        )
        second = self.make_track(
            self.incoming / "b.mp3", title="B", track_no=2, content_hash="deadbeef"
        )
        self.make_track(
            self.incoming / "c.mp3", title="C", track_no=3, content_hash="other"
        )

        groups = organizer.find_duplicates()

        self.assertEqual(len(groups), 1)
        self.assertEqual(
            {track.pk for track in groups[0]}, {first.pk, second.pk}
        )

    def test_groups_by_album_artist_album_track_and_title(self):
        """Two rips of one track never share a hash, only their metadata."""
        first = self.make_track(
            self.incoming / "rip1.mp3", album_artist="Pink Floyd", bitrate=320
        )
        second = self.make_track(
            self.incoming / "rip2.mp3", album_artist="Pink Floyd", bitrate=128
        )
        self.make_track(self.incoming / "other.mp3", title="Different", track_no=7)

        groups = organizer.find_duplicates()

        self.assertEqual(len(groups), 1)
        self.assertEqual([track.pk for track in groups[0]], [first.pk, second.pk])

    def test_the_same_set_is_not_reported_twice(self):
        self.make_track(
            self.incoming / "a.mp3", album_artist="Pink Floyd", content_hash="same"
        )
        self.make_track(
            self.incoming / "b.mp3", album_artist="Pink Floyd", content_hash="same"
        )

        groups = organizer.find_duplicates()

        self.assertEqual(len(groups), 1)

    def test_unidentified_tracks_are_not_all_one_duplicate_group(self):
        for index in range(3):
            self.make_track(
                self.incoming / f"unknown{index}.mp3",
                title="",
                album="",
                track_no=0,
            )

        self.assertEqual(organizer.find_duplicates(), [])

    def test_missing_tracks_are_left_out(self):
        self.make_track(
            self.incoming / "a.mp3", content_hash="x", state=TrackState.MISSING
        )
        self.make_track(
            self.incoming / "b.mp3", content_hash="x", state=TrackState.MISSING
        )

        self.assertEqual(organizer.find_duplicates(), [])


class ContentHashTests(LibraryTestCase):
    def test_hash_is_computed_once_and_stored(self):
        path = write_mp3(self.incoming / "song.mp3")
        track = self.make_track(path)

        digest = organizer.ensure_content_hash(track)

        self.assertTrue(digest)
        track.refresh_from_db()
        self.assertEqual(track.content_hash, digest)

        with mock.patch.object(organizer, "hash_file") as hasher:
            self.assertEqual(organizer.ensure_content_hash(track), digest)
        hasher.assert_not_called()
