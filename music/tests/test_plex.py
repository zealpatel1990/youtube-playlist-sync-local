"""Tests for `music.plex` — the Plex naming policy.

Exhaustive on purpose. This module decides where every file in the library
physically lands, so a regression here does not raise, it silently moves
thousands of files into the wrong shape (and, in the multi-disc case, into
paths that look plausible while being wrong).

Pure functions, no IO, no DB — `SimpleTestCase` throughout.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

from music import plex

from music.plex import (
    SINGLES_ALBUM,
    UNKNOWN_ALBUM,
    UNKNOWN_ARTIST,
    VARIOUS_ARTISTS,
    TrackNaming,
    build_filename,
    build_path,
    build_relative_path,
    format_track_number,
    is_already_organized,
    naming_from_track,
    resolve_album,
    resolve_album_artist,
    resolve_title,
)

#: Characters that must never survive into a path component.
ILLEGAL = '<>:"/\\|?*\0'


class FormatTrackNumberTests(SimpleTestCase):
    """`NN`, or `<disc><NN>` — but only from disc 2 upwards."""

    def test_zero_track_number_is_empty(self):
        self.assertEqual(format_track_number(0, 0), "")

    def test_zero_track_number_is_empty_even_on_a_later_disc(self):
        # An unknown track number stays unknown; a disc number alone must not
        # invent one, or every untagged file on disc 2 becomes "2".
        self.assertEqual(format_track_number(0, 2), "")
        self.assertEqual(format_track_number(0, 9), "")

    def test_negative_track_number_is_empty(self):
        self.assertEqual(format_track_number(-1, 0), "")

    def test_single_digit_is_zero_padded(self):
        self.assertEqual(format_track_number(1, 0), "01")
        self.assertEqual(format_track_number(5, 0), "05")
        self.assertEqual(format_track_number(9, 0), "09")

    def test_two_digits_are_unpadded(self):
        self.assertEqual(format_track_number(12, 0), "12")
        self.assertEqual(format_track_number(99, 0), "99")

    def test_disc_one_does_not_prefix(self):
        # The regression this whole function exists for: disc 1 track 2 is
        # "02", never "102". A single-disc rip that happens to tag disc=1 is
        # the common case, and "101 - ..." is both ugly and wrong.
        self.assertEqual(format_track_number(2, 1), "02")
        self.assertEqual(format_track_number(11, 1), "11")

    def test_disc_two_and_up_prefixes(self):
        self.assertEqual(format_track_number(2, 3), "302")
        self.assertEqual(format_track_number(15, 2), "215")
        self.assertEqual(format_track_number(1, 2), "201")
        self.assertEqual(format_track_number(9, 4), "409")

    def test_three_digit_track_number_is_not_truncated(self):
        self.assertEqual(format_track_number(100, 0), "100")
        self.assertEqual(format_track_number(100, 2), "2100")


class ResolveAlbumArtistTests(SimpleTestCase):
    def test_compilation_goes_to_various_artists(self):
        naming = TrackNaming(artist="Some Guy", album_artist="Some Other Guy",
                             is_compilation=True)
        self.assertEqual(resolve_album_artist(naming), VARIOUS_ARTISTS)

    def test_compilation_wins_even_with_no_other_metadata(self):
        self.assertEqual(
            resolve_album_artist(TrackNaming(is_compilation=True)), VARIOUS_ARTISTS
        )

    def test_album_artist_wins_over_artist(self):
        naming = TrackNaming(artist="Freddie Mercury", album_artist="Queen")
        self.assertEqual(resolve_album_artist(naming), "Queen")

    def test_falls_back_to_artist(self):
        self.assertEqual(resolve_album_artist(TrackNaming(artist="Queen")), "Queen")

    def test_whitespace_only_album_artist_falls_through_to_artist(self):
        naming = TrackNaming(artist="Queen", album_artist="   ")
        self.assertEqual(resolve_album_artist(naming), "Queen")

    def test_empty_gives_unknown_artist(self):
        self.assertEqual(resolve_album_artist(TrackNaming()), UNKNOWN_ARTIST)
        self.assertEqual(
            resolve_album_artist(TrackNaming(artist="  ", album_artist="\t")),
            UNKNOWN_ARTIST,
        )

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(
            resolve_album_artist(TrackNaming(album_artist="  Queen  ")), "Queen"
        )


class ResolveAlbumAndTitleTests(SimpleTestCase):
    def test_album_is_used_when_present(self):
        self.assertEqual(resolve_album(TrackNaming(album="  Kind of Blue ")),
                         "Kind of Blue")

    def test_missing_album_with_a_title_becomes_singles(self):
        self.assertEqual(resolve_album(TrackNaming(title="Loose Track")),
                         SINGLES_ALBUM)

    def test_missing_album_with_only_a_fallback_stem_becomes_singles(self):
        self.assertEqual(resolve_album(TrackNaming(fallback_stem="dQw4w9WgXcQ")),
                         SINGLES_ALBUM)

    def test_nothing_at_all_becomes_unknown_album(self):
        self.assertEqual(resolve_album(TrackNaming()), UNKNOWN_ALBUM)

    def test_title_falls_back_to_stem_then_to_unknown(self):
        self.assertEqual(resolve_title(TrackNaming(title=" Song ")), "Song")
        self.assertEqual(resolve_title(TrackNaming(fallback_stem="raw-file")),
                         "raw-file")
        self.assertEqual(resolve_title(TrackNaming()), "Unknown Track")
        self.assertEqual(resolve_title(TrackNaming(title="   ")), "Unknown Track")


class BuildFilenameTests(SimpleTestCase):
    def test_numbered(self):
        naming = TrackNaming(title="Bohemian Rhapsody", track_no=11)
        self.assertEqual(build_filename(naming), "11 - Bohemian Rhapsody.mp3")

    def test_unnumbered_omits_the_separator(self):
        naming = TrackNaming(title="Bohemian Rhapsody")
        self.assertEqual(build_filename(naming), "Bohemian Rhapsody.mp3")

    def test_extension_is_normalised_and_lowercased(self):
        self.assertEqual(
            build_filename(TrackNaming(title="X", extension="FLAC")), "X.flac"
        )
        self.assertEqual(
            build_filename(TrackNaming(title="X", extension=".M4A")), "X.m4a"
        )

    def test_multi_disc_filename(self):
        naming = TrackNaming(title="Song", track_no=2, disc_no=3)
        self.assertEqual(build_filename(naming), "302 - Song.mp3")


class BuildRelativePathTests(SimpleTestCase):
    def test_normal_track(self):
        naming = TrackNaming(
            title="Bohemian Rhapsody",
            artist="Queen",
            album="A Night at the Opera",
            track_no=11,
        )
        self.assertEqual(
            build_relative_path(naming),
            Path("Queen") / "A Night at the Opera" / "11 - Bohemian Rhapsody.mp3",
        )

    def test_album_artist_is_the_folder_not_the_track_artist(self):
        naming = TrackNaming(
            title="Under Pressure",
            artist="Queen & David Bowie",
            album_artist="Queen",
            album="Hot Space",
            track_no=1,
        )
        self.assertEqual(build_relative_path(naming).parts[0], "Queen")

    def test_compilation_goes_under_various_artists(self):
        naming = TrackNaming(
            title="Song",
            artist="Real Performer",
            album_artist="Ignored",
            album="Now That's What I Call Music 42",
            track_no=3,
            is_compilation=True,
        )
        self.assertEqual(
            build_relative_path(naming),
            Path(VARIOUS_ARTISTS)
            / "Now That's What I Call Music 42"
            / "03 - Song.mp3",
        )

    def test_multi_disc_track(self):
        naming = TrackNaming(
            title="Cygnus X-1",
            artist="Rush",
            album="A Farewell to Kings",
            track_no=2,
            disc_no=3,
        )
        self.assertEqual(
            build_relative_path(naming),
            Path("Rush") / "A Farewell to Kings" / "302 - Cygnus X-1.mp3",
        )

    def test_no_album_lands_in_singles(self):
        naming = TrackNaming(title="One Off", artist="Nobody")
        self.assertEqual(
            build_relative_path(naming),
            Path("Nobody") / SINGLES_ALBUM / "One Off.mp3",
        )

    def test_no_title_uses_the_fallback_stem(self):
        naming = TrackNaming(
            artist="Nobody", album="Demos", track_no=2, fallback_stem="dQw4w9WgXcQ"
        )
        self.assertEqual(
            build_relative_path(naming),
            Path("Nobody") / "Demos" / "02 - dQw4w9WgXcQ.mp3",
        )

    def test_nothing_known_at_all(self):
        self.assertEqual(
            build_relative_path(TrackNaming()),
            Path(UNKNOWN_ARTIST) / UNKNOWN_ALBUM / "Unknown Track.mp3",
        )

    # --- illegal characters -------------------------------------------

    def test_slash_in_the_artist_does_not_create_a_directory(self):
        naming = TrackNaming(title="Back in Black", artist="AC/DC", album="Back in Black")
        path = build_relative_path(naming)
        self.assertEqual(path.parts[0], "AC_DC")
        self.assertEqual(len(path.parts), 3)

    def test_each_illegal_character_is_replaced(self):
        naming = TrackNaming(
            title='Who? What: Why/When \\ "Quoted" <tag> |pipe| *star*',
            artist="Q:A",
            album="Back: In Black",
            track_no=1,
        )
        path = build_relative_path(naming)
        self.assertEqual(path.parts[0], "Q_A")
        self.assertEqual(path.parts[1], "Back_ In Black")
        for part in path.parts:
            for char in ILLEGAL:
                self.assertNotIn(char, part, f"{char!r} survived into {part!r}")
        self.assertEqual(len(path.parts), 3)

    def test_backslash_in_a_title_does_not_split_the_path(self):
        naming = TrackNaming(title="AC\\DC Tribute", artist="Various", album="Live")
        path = build_relative_path(naming)
        self.assertEqual(len(path.parts), 3)
        self.assertEqual(path.name, "AC_DC Tribute.mp3")

    def test_traversal_cannot_escape_the_library(self):
        naming = TrackNaming(title="../../../etc/passwd", artist="..", album="..")
        path = build_relative_path(naming)
        self.assertFalse(path.is_absolute())
        self.assertEqual(len(path.parts), 3)
        self.assertNotIn("..", path.parts)
        # ".." sanitizes to nothing, so both folders take their fallbacks.
        self.assertEqual(path.parts[0], UNKNOWN_ARTIST)
        self.assertEqual(path.parts[1], UNKNOWN_ALBUM)
        # The separators in the title collapse into one flat component.
        self.assertEqual(path.name, ".._.._.._etc_passwd.mp3")

    def test_a_dotted_traversal_artist_stays_one_component(self):
        naming = TrackNaming(title="Song", artist="../..", album="Album")
        path = build_relative_path(naming)
        self.assertEqual(len(path.parts), 3)
        self.assertNotIn("/", path.parts[0])
        self.assertNotIn("\\", path.parts[0])

    # --- Windows reserved names ---------------------------------------

    def test_reserved_device_names_are_escaped(self):
        naming = TrackNaming(title="NUL", artist="CON", album="PRN")
        self.assertEqual(
            build_relative_path(naming), Path("_CON") / "_PRN" / "_NUL.mp3"
        )

    def test_reserved_names_are_matched_case_insensitively(self):
        naming = TrackNaming(title="aux", artist="con", album="Lpt9")
        self.assertEqual(
            build_relative_path(naming), Path("_con") / "_Lpt9" / "_aux.mp3"
        )

    def test_reserved_name_with_an_extension_is_still_reserved(self):
        # "NUL.mp3" is as unusable as "NUL" on Windows.
        naming = TrackNaming(title="Song", artist="NUL.txt", album="Album")
        self.assertEqual(build_relative_path(naming).parts[0], "_NUL.txt")

    def test_names_merely_starting_with_a_device_name_are_left_alone(self):
        naming = TrackNaming(title="Console Wars", artist="Concrete", album="Auxiliary")
        self.assertEqual(
            build_relative_path(naming),
            Path("Concrete") / "Auxiliary" / "Console Wars.mp3",
        )

    # --- length --------------------------------------------------------

    def test_long_unicode_stays_under_the_component_byte_limit(self):
        long_title = "日本語のとても長いタイトル" * 40  # ~1.5 KiB encoded
        naming = TrackNaming(
            title=long_title, artist=long_title, album=long_title, track_no=7
        )
        path = build_relative_path(naming)
        self.assertEqual(len(path.parts), 3)
        for part in path.parts:
            self.assertLess(
                len(part.encode("utf-8")),
                255,
                f"component is {len(part.encode('utf-8'))} bytes: {part[:40]}…",
            )
        # Truncation must land on a character boundary, never mid-codepoint.
        self.assertTrue(long_title.startswith(path.parts[0]))
        self.assertTrue(path.name.startswith("07 - "))

    def test_long_ascii_stays_under_the_component_byte_limit(self):
        naming = TrackNaming(title="A" * 600, artist="B" * 600, album="C" * 600)
        for part in build_relative_path(naming).parts:
            self.assertLess(len(part.encode("utf-8")), 255)

    def test_truncation_never_leaves_a_trailing_dot(self):
        # Windows and exFAT silently drop a trailing dot when the file is
        # created, so the file would land somewhere the app can never
        # recognise again and would be "organized" on every single pass.
        title = "A" * 199 + "." + "B" * 100
        naming = TrackNaming(title=title, artist=title, album=title)
        for part in build_relative_path(naming).parts:
            stem = part[:-4] if part.endswith(".mp3") else part
            self.assertFalse(stem.endswith("."), f"trailing dot in {part!r}")
            self.assertFalse(stem.endswith(" "), f"trailing space in {part!r}")

    # --- trailing dots and spaces --------------------------------------

    def test_trailing_dots_and_spaces_are_trimmed(self):
        naming = TrackNaming(
            title="Trailing dots...", artist="Artist ", album="Album Name. "
        )
        self.assertEqual(
            build_relative_path(naming),
            Path("Artist") / "Album Name" / "Trailing dots.mp3",
        )

    def test_internal_dots_survive(self):
        naming = TrackNaming(title="Mr. Brightside", artist="Dr. Dre", album="2001 A.D.")
        path = build_relative_path(naming)
        self.assertEqual(path.parts[0], "Dr. Dre")
        self.assertEqual(path.parts[1], "2001 A.D")  # trailing dot trimmed
        self.assertEqual(path.name, "Mr. Brightside.mp3")

    def test_a_title_of_only_dots_falls_back(self):
        naming = TrackNaming(title="...", artist="...", album="...")
        self.assertEqual(
            build_relative_path(naming),
            Path(UNKNOWN_ARTIST) / UNKNOWN_ALBUM / "Unknown Track.mp3",
        )

    # --- structural invariants ----------------------------------------

    def test_every_result_is_a_relative_three_part_path(self):
        namings = [
            TrackNaming(),
            TrackNaming(title="t", artist="a", album="b", track_no=1),
            TrackNaming(title="/", artist="\\", album=":"),
            TrackNaming(title="\x00\x01\x02", artist="\n", album="\t"),
            TrackNaming(title="CON", artist="NUL", album="COM1"),
            TrackNaming(title="x" * 900, artist="y" * 900, album="z" * 900),
            TrackNaming(title="Ünïcödé ☃", artist="Ǆ", album="🎵"),
            TrackNaming(title="t", is_compilation=True),
            TrackNaming(fallback_stem="stem-only"),
        ]
        for naming in namings:
            with self.subTest(naming=naming):
                path = build_relative_path(naming)
                self.assertFalse(path.is_absolute())
                self.assertEqual(len(path.parts), 3)
                for part in path.parts:
                    self.assertNotIn(part, (".", ".."))
                    self.assertTrue(part.strip())
                    for char in ILLEGAL:
                        self.assertNotIn(char, part)


class BuildPathTests(SimpleTestCase):
    def test_build_path_joins_the_root(self):
        naming = TrackNaming(title="Song", artist="Artist", album="Album", track_no=1)
        root = Path("/srv/music")
        self.assertEqual(
            build_path(root, naming), root / build_relative_path(naming)
        )

    def test_build_path_accepts_a_string_root(self):
        naming = TrackNaming(title="Song", artist="Artist", album="Album")
        self.assertEqual(
            build_path("/srv/music", naming), Path("/srv/music") / "Artist" / "Album" / "Song.mp3"
        )


class NamingFromTrackTests(SimpleTestCase):
    """The adapter that keeps plex.py free of an ORM import."""

    @staticmethod
    def _track(**overrides):
        fields = dict(
            path="/srv/music/incoming/dQw4w9WgXcQ.FLAC",
            title="Never Gonna Give You Up",
            artist="Rick Astley",
            album="Whenever You Need Somebody",
            album_artist="Rick Astley",
            track_no=1,
            disc_no=0,
            is_compilation=False,
        )
        fields.update(overrides)
        return SimpleNamespace(**fields)

    def test_extension_and_stem_come_from_the_path(self):
        naming = naming_from_track(self._track())
        self.assertEqual(naming.extension, ".FLAC")
        self.assertEqual(naming.fallback_stem, "dQw4w9WgXcQ")
        self.assertEqual(
            build_relative_path(naming),
            Path("Rick Astley")
            / "Whenever You Need Somebody"
            / "01 - Never Gonna Give You Up.flac",
        )

    def test_extensionless_path_defaults_to_mp3(self):
        naming = naming_from_track(self._track(path="/srv/music/no_extension"))
        self.assertEqual(naming.extension, ".mp3")

    def test_untitled_track_uses_the_filename_stem(self):
        naming = naming_from_track(self._track(title="", track_no=0))
        self.assertEqual(build_relative_path(naming).name, "dQw4w9WgXcQ.flac")


class IsAlreadyOrganizedTests(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.naming = TrackNaming(
            title="Bohemian Rhapsody",
            artist="Queen",
            album="A Night at the Opera",
            track_no=11,
        )

    def test_true_at_the_computed_path(self):
        target = self.root / build_relative_path(self.naming)
        self.assertTrue(is_already_organized(target, self.root, self.naming))

    def test_true_for_a_string_path(self):
        target = str(self.root / build_relative_path(self.naming))
        self.assertTrue(is_already_organized(target, str(self.root), self.naming))

    def test_false_in_the_wrong_album_folder(self):
        wrong = self.root / "Queen" / "Sheer Heart Attack" / "11 - Bohemian Rhapsody.mp3"
        self.assertFalse(is_already_organized(wrong, self.root, self.naming))

    def test_false_in_the_wrong_artist_folder(self):
        wrong = (
            self.root / "David Bowie" / "A Night at the Opera"
            / "11 - Bohemian Rhapsody.mp3"
        )
        self.assertFalse(is_already_organized(wrong, self.root, self.naming))

    def test_false_with_the_wrong_track_number(self):
        wrong = self.root / "Queen" / "A Night at the Opera" / "01 - Bohemian Rhapsody.mp3"
        self.assertFalse(is_already_organized(wrong, self.root, self.naming))

    def test_false_when_flat_in_the_root(self):
        wrong = self.root / "11 - Bohemian Rhapsody.mp3"
        self.assertFalse(is_already_organized(wrong, self.root, self.naming))

    def test_false_outside_the_library_root(self):
        outside = self.root.parent / "somewhere-else" / build_relative_path(self.naming)
        self.assertFalse(is_already_organized(outside, self.root, self.naming))

    def test_case_insensitive(self):
        # The HDD may be mounted case-insensitively; a move that only changes
        # case is pointless and on some filesystems destructive.
        shouty = self.root / "QUEEN" / "A NIGHT AT THE OPERA" / "11 - BOHEMIAN RHAPSODY.MP3"
        self.assertTrue(is_already_organized(shouty, self.root, self.naming))

        quiet = self.root / "queen" / "a night at the opera" / "11 - bohemian rhapsody.mp3"
        self.assertTrue(is_already_organized(quiet, self.root, self.naming))

    def test_round_trip_for_every_shape_of_metadata(self):
        """Whatever build_relative_path emits must be recognised on the way back."""
        namings = [
            TrackNaming(),
            TrackNaming(title="Plain", artist="Artist", album="Album", track_no=1),
            TrackNaming(title="Song", artist="AC/DC", album="Back: In Black",
                        track_no=2, disc_no=2),
            TrackNaming(title="Comp", artist="Performer", album="Mix",
                        track_no=4, is_compilation=True),
            TrackNaming(title="NUL", artist="CON", album="PRN"),
            TrackNaming(title="Trailing dots...", artist="Artist ", album="Album. "),
            TrackNaming(title="日本語のとても長いタイトル" * 40,
                        artist="Ünïcödé Ärtist", album="Ålbum", track_no=9),
            TrackNaming(title="A" * 199 + "." + "B" * 80, artist="X" * 400,
                        album="Y" * 400),
            TrackNaming(fallback_stem="only-a-stem", extension=".flac"),
            TrackNaming(title="Single", artist="Nobody"),  # -> Singles
        ]
        for naming in namings:
            with self.subTest(title=naming.title[:30] or "<empty>"):
                target = build_path(self.root, naming)
                self.assertTrue(
                    is_already_organized(target, self.root, naming),
                    f"{target} was not recognised as organized",
                )


class AlbumNormalizationTests(SimpleTestCase):
    """Folding the spellings of one album into one string.

    Every case here comes from the live library, where 11 albums had been split
    into 25 folders by nothing but a suffix.
    """

    def test_the_soundtrack_suffix_is_removed(self):
        self.assertEqual(
            plex.normalize_album("Rang De Basanti (Original Motion Picture Soundtrack)"),
            "Rang De Basanti",
        )

    def test_the_bracketed_and_odd_spaced_forms_are_removed(self):
        for written in (
            "Guru [Original Motion Picture Soundtrack]",
            "Guru  (Original  Motion  Picture  Soundtrack)  ",
            "Guru (ORIGINAL MOTION PICTURE SOUNDTRACK)",
        ):
            with self.subTest(written=written):
                self.assertEqual(plex.normalize_album(written), "Guru")

    def test_only_that_one_phrase_is_removed(self):
        # Deliberately narrow: widen only against a measured collision. Each of
        # these was in an earlier, looser version of the rule and is now kept.
        for kept in (
            "Guru (Soundtrack)",
            "Guru (OST)",
            "Guru (Original Score)",
            "Guru (Motion Picture Soundtrack)",
            "Guru (Original Soundtrack)",
            "JUST A BOY - Single",
            "Ghoomer - EP",
        ):
            with self.subTest(kept=kept):
                self.assertEqual(plex.normalize_album(kept), kept)

    def test_the_suffix_is_only_removed_from_the_end(self):
        title = "Original Motion Picture Soundtrack Collection"
        self.assertEqual(plex.normalize_album(title), title)

    def test_a_suffix_that_names_the_recording_is_kept(self):
        # These are different recordings with different running times, and
        # `textsearch` works hard to tell them apart. Folding them would undo
        # that.
        for kept in (
            "Zara Zara (Jhankar Beats)",
            "Lagyo Re Prityu No Rang (Slowed + Reverb)",
            "Aashiqui Mein Teri (Akbar Sami Remix)",
            "Hookah Bar (Remix)",
            # "Score" unqualified is a real album name (The Fugees' The Score),
            # so only "Original Score" / "Motion Picture Score" count.
            "The Score",
            "Bandit (Score)",
            "Pushpa (Deluxe Edition)",
            "Rockstar (Live)",
        ):
            with self.subTest(kept=kept):
                self.assertEqual(plex.normalize_album(kept), kept)

    def test_a_title_that_is_only_the_suffix_survives(self):
        # Normalising this to "" would file the track under Unknown Album.
        only = "(Original Motion Picture Soundtrack)"
        self.assertEqual(plex.normalize_album(only), only)

    def test_nothing_in_means_nothing_out(self):
        self.assertEqual(plex.normalize_album(""), "")
        self.assertEqual(plex.normalize_album("   "), "")

    def test_the_two_rang_de_basanti_spellings_become_one(self):
        a = plex.normalize_album("Rang De Basanti")
        b = plex.normalize_album("Rang De Basanti (Original Motion Picture Soundtrack)")
        self.assertEqual(a, b)


class PrincipalArtistTests(SimpleTestCase):
    """Taking the album's artist out of a per-track performer list."""

    def test_a_comma_list_yields_the_first_credit(self):
        self.assertEqual(
            plex.principal_artist("A.R. Rahman, Shreya Ghoshal & Uday Mazumdar"),
            "A.R. Rahman",
        )
        self.assertEqual(
            plex.principal_artist("Pritam, Arijit Singh & Sunidhi Chauhan"), "Pritam"
        )

    def test_a_hyphenated_group_is_never_split(self):
        # Truncating either of these would invent an artist who never existed.
        for band in ("Shankar-Ehsaan-Loy", "Salim-Sulaiman"):
            with self.subTest(band=band):
                self.assertEqual(plex.principal_artist(band), band)

    def test_an_ampersand_pair_is_never_split(self):
        # A duo can legitimately be an album artist, so `&` alone is not a list.
        for duo in ("Asha Bhosle & Adnan Sami", "Mohd. Rafi & Suman Kalyanpur"):
            with self.subTest(duo=duo):
                self.assertEqual(plex.principal_artist(duo), duo)

    def test_a_single_name_is_untouched(self):
        self.assertEqual(plex.principal_artist("A.R. Rahman"), "A.R. Rahman")

    def test_nothing_in_means_nothing_out(self):
        self.assertEqual(plex.principal_artist(""), "")


class NamingPolicyIntegrationTests(SimpleTestCase):
    """The two rules as they reach an actual path."""

    def test_a_performer_list_no_longer_becomes_a_folder(self):
        naming = plex.TrackNaming(
            title="Tu Bin Bataye",
            artist="A.R. Rahman, Madhushree & Naresh Iyer",
            album="Rang De Basanti (Original Motion Picture Soundtrack)",
            track_no=4,
        )
        self.assertEqual(
            plex.build_relative_path(naming),
            Path("A.R. Rahman") / "Rang De Basanti" / "04 - Tu Bin Bataye.mp3",
        )

    def test_an_explicit_album_artist_is_also_reduced_to_its_principal(self):
        # The real Lagaan case: the line-up was written into album_artist, not
        # just inherited from the track's artist tag.
        naming = plex.TrackNaming(
            title="O Rey Chhori",
            album_artist="A.R. Rahman, Alka Yagnik, Udit Narayan & Vasundhara Das",
        )
        self.assertEqual(plex.resolve_album_artist(naming), "A.R. Rahman")

    def test_a_group_name_in_album_artist_is_still_never_split(self):
        naming = plex.TrackNaming(
            title="X", artist="Someone, Else", album_artist="Shankar-Ehsaan-Loy"
        )
        self.assertEqual(plex.resolve_album_artist(naming), "Shankar-Ehsaan-Loy")

    def test_a_compilation_still_goes_to_various_artists(self):
        naming = plex.TrackNaming(
            title="X", artist="A, B", album="Y", is_compilation=True
        )
        self.assertEqual(plex.resolve_album_artist(naming), plex.VARIOUS_ARTISTS)

    def test_the_four_rang_de_basanti_albums_land_in_one_place(self):
        # The real tags from the Pi, minus the MusicBrainz ids.
        tracks = [
            ("Khoon Chala", "Mohit Chauhan", "Rang De Basanti", "A.R. Rahman", 6),
            ("Luka Chuppi", "Lata Mangeshkar & A. R. Rahman", "Rang De Basanti",
             "A.R. Rahman", 8),
            ("Rang De Basanti", "A.R. Rahman, Daler Mehndi & K.S. Chithra",
             "Rang De Basanti (Original Motion Picture Soundtrack)", "A.R. Rahman", 2),
            ("Tu Bin Bataye", "A.R. Rahman, Madhushree & Naresh Iyer",
             "Rang De Basanti (Original Motion Picture Soundtrack)", "A.R. Rahman", 4),
        ]
        folders = {
            plex.build_relative_path(
                plex.TrackNaming(title=t, artist=a, album=al, album_artist=aa,
                                 track_no=n)
            ).parent
            for t, a, al, aa, n in tracks
        }
        self.assertEqual(folders, {Path("A.R. Rahman") / "Rang De Basanti"})


class CanonicalArtistTests(SimpleTestCase):
    """The curated alias table, and how it composes with list-reduction."""

    def test_punctuation_variants_resolve_through_one_entry(self):
        for written in ("A. R. Rahman", "A.R. Rahman", "A R Rahman", "a.r. rahman"):
            with self.subTest(written=written):
                self.assertEqual(plex.canonical_artist(written), "A.R. Rahman")

    def test_a_composer_and_lyricist_pair_files_under_the_composer(self):
        self.assertEqual(plex.canonical_artist("A.R. Rahman & Gulzar"), "A.R. Rahman")
        self.assertEqual(plex.canonical_artist("Pritam & Irshad Kamil"), "Pritam")

    def test_a_duo_not_in_the_table_is_left_alone(self):
        # The rule that makes the table safe: nothing collapses unless listed.
        for duo in ("Asha Bhosle & Adnan Sami", "Mohd. Rafi & Suman Kalyanpur",
                    "Salim-Sulaiman"):
            with self.subTest(duo=duo):
                self.assertEqual(plex.canonical_artist(duo), duo)

    def test_list_reduction_runs_before_the_alias_lookup(self):
        self.assertEqual(
            plex.canonical_artist("A. R. Rahman, Alka Yagnik & Udit Narayan"),
            "A.R. Rahman",
        )

    def test_the_three_dash_characters_become_one_name(self):
        for dash in ("-", "‐", "–"):
            with self.subTest(dash=dash):
                self.assertEqual(
                    plex.canonical_artist(f"Shankar{dash}Ehsaan{dash}Loy"),
                    "Shankar-Ehsaan-Loy",
                )

    def test_an_unknown_credit_is_returned_unchanged(self):
        self.assertEqual(plex.canonical_artist("Some New Artist"), "Some New Artist")

    def test_nothing_in_means_nothing_out(self):
        self.assertEqual(plex.canonical_artist(""), "")

    def test_every_alias_target_is_itself_stable(self):
        # A target that would itself be rewritten means the table disagrees
        # with itself and the result depends on how many times it is applied.
        for target in plex.ARTIST_ALIASES.values():
            with self.subTest(target=target):
                self.assertEqual(plex.canonical_artist(target), target)
