"""Plex music layout rules. Pure functions, so the naming policy is testable.

Reference: https://support.plex.tv/articles/200265296-adding-music-media-from-folders/

    Music/Artist/Album/TrackNumber - TrackName.ext
    Music/Various Artists/Album/TrackNumber - TrackName.ext

Multi-disc albums prepend the disc number, so disc 3 track 2 becomes `302 - …`.
The disc number is prepended only when it is 2 or higher: a single-disc rip
that happens to tag `disc=1` would otherwise become `101 - …` rather than
`01 - …`. Plex reads the disc number from the embedded tag regardless, so the
filename convention is the secondary signal here, not the authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from music.core.fileio import sanitize_component

VARIOUS_ARTISTS = "Various Artists"
UNKNOWN_ARTIST = "Unknown Artist"
UNKNOWN_ALBUM = "Unknown Album"
#: Where tracks with no album land. Plex prefers albums-in-folders over a flat
#: dump even when tags are complete, so everything gets *some* album folder.
SINGLES_ALBUM = "Singles"


@dataclass(frozen=True)
class TrackNaming:
    """The metadata subset that determines a file's place in the library."""

    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track_no: int = 0
    disc_no: int = 0
    is_compilation: bool = False
    extension: str = ".mp3"
    #: Used only when the title is unknown, to avoid inventing one.
    fallback_stem: str = ""


#: The one trailing parenthetical that names the *kind* of release rather than
#: distinguishing one recording from another. Removed so both spellings of an
#: album become one string.
#:
#: Deliberately the narrowest rule that fixes the observed problem. Wider
#: versions were tried and rejected: a bare `(Score)` matches *The Score*, and
#: `(Soundtrack)`, `(OST)` and `(Original Score)` buy nothing here — every one
#: of the 11 albums this library had split into 25 folders was split by this
#: exact string. Anything that only *narrows* which recording is meant —
#: `(Jhankar Beats)`, `(Slowed + Reverb)`, `(Remix)`, `(Live)` — must never
#: match, because those are different recordings with different running times.
#:
#: Widen this only against a measured collision, one phrase at a time.
_RELEASE_KIND = re.compile(
    r"\s*[\(\[]\s*original\s+motion\s+picture\s+soundtrack\s*[\)\]]\s*$",
    re.IGNORECASE,
)


def normalize_album(album: str) -> str:
    """An album title with the release-kind suffix removed.

    `Rang De Basanti (Original Motion Picture Soundtrack)` and
    `Rang De Basanti` are one album; Plex groups on the string, so they have to
    become one string.
    """
    cleaned = (album or "").strip()
    if not cleaned:
        return ""
    stripped = _RELEASE_KIND.sub("", cleaned).strip()
    # Never normalise a title out of existence: a title that is nothing but the
    # suffix is still all the name there is.
    return stripped or cleaned


#: Only a comma starts a list of performers. Deliberately not `&` or a dash:
#: `Salim-Sulaiman`, `Shankar-Ehsaan-Loy`, `Asha Bhosle & Adnan Sami` and
#: `Mohd. Rafi & Suman Kalyanpur` are all one credit, and truncating any of
#: them would invent an artist who never existed. A comma is the separator the
#: tag writers actually use for a list, and in every case measured on this
#: library the composer is credited first:
#:
#:   "A.R. Rahman, Shreya Ghoshal & Uday Mazumdar" -> A.R. Rahman
#:   "Pritam, Arijit Singh & Sunidhi Chauhan"      -> Pritam
_CREDIT_LIST = re.compile(r"\s*,\s*")


def principal_artist(credit: str) -> str:
    """The first name in a performer credit, which is the one an album is filed under.

    47 of 331 artist folders on the live library were whole per-track performer
    lists, because a track carrying no album artist fell back to its own
    `artist` tag verbatim. Every one of those was one album fragmented into a
    folder per singer line-up.
    """
    cleaned = (credit or "").strip()
    if not cleaned:
        return ""
    return _CREDIT_LIST.split(cleaned, 1)[0].strip() or cleaned


#: Artist credits that mean an artist already in the library under another name.
#:
#: Curated on purpose. Automatic folding was considered and rejected: nothing
#: mechanical separates `A.R. Rahman & Gulzar` (a composer and his lyricist,
#: one album) from `Asha Bhosle & Adnan Sami` (a duet, genuinely two names), so
#: the ones that collapse are listed by hand and everything else is left alone.
#:
#: **Keys are matched with punctuation, spacing and case folded away**, so one
#: entry covers every spelling of a name — `A. R. Rahman`, `A.R. Rahman` and
#: `A R Rahman` all resolve through the single `A.R. Rahman` key. Add a second
#: entry only when the *words* differ.
#:
#: Every entry below is a collision measured on the live library.
ARTIST_ALIASES: dict[str, str] = {
    # Spelling only — same words, different punctuation.
    "A.R. Rahman": "A.R. Rahman",
    "K K": "KK",
    # Three different dash characters were in use: ASCII, U+2010 and en dash.
    "Shankar-Ehsaan-Loy": "Shankar-Ehsaan-Loy",
    # Composer & lyricist credited as a pair, filed under the composer.
    "A.R. Rahman & Gulzar": "A.R. Rahman",
    "A.R. Rahman & Irshad Kamil": "A.R. Rahman",
    "Pritam & Irshad Kamil": "Pritam",
    "Pritam & Amitabh Bhattacharya": "Pritam",
    "Vishal Bhardwaj & Rahat Fateh Ali Khan": "Vishal Bhardwaj",
    "Vishal Bhardwaj & Shreya Ghoshal": "Vishal Bhardwaj",
    "S.D. Burman & Shailendra": "S.D. Burman",
}

_ALIAS_FOLD = re.compile(r"[^a-z0-9]+")


def _fold(name: str) -> str:
    """A comparison key with case, spacing and punctuation removed."""
    return _ALIAS_FOLD.sub("", (name or "").lower())


#: Built once. A dict keyed on the folded form is what lets one entry cover
#: every punctuation variant of the same name.
_ALIASES_FOLDED: dict[str, str] = {_fold(k): v for k, v in ARTIST_ALIASES.items()}


def canonical_artist(credit: str) -> str:
    """One artist name for a credit, after list-reduction and aliasing.

    The two rules compose: `A.R. Rahman, Shreya Ghoshal & Uday Mazumdar` loses
    its line-up to `principal_artist`, and `A. R. Rahman` then resolves to
    `A.R. Rahman` through the alias table. A credit in neither is returned
    unchanged — this never invents a name it was not told about.
    """
    reduced = principal_artist(credit)
    if not reduced:
        return ""
    return _ALIASES_FOLDED.get(_fold(reduced), reduced)


def resolve_album_artist(naming: TrackNaming) -> str:
    """The artist folder name. Compilations go to `Various Artists`; the
    per-track `artist` tag still carries the real performer.

    `principal_artist` applies to the album artist tag as well as to the
    fallback, because a tagger writing a line-up into that field is exactly the
    case this exists for. Measured: Lagaan sat in three folders, one of them
    literally named `A.R. Rahman, Alka Yagnik, Udit Narayan & Vasundhara Das`,
    and treating an explicit tag as authoritative left it there.
    """
    if naming.is_compilation:
        return VARIOUS_ARTISTS
    for candidate in (naming.album_artist, naming.artist):
        if candidate and candidate.strip():
            return canonical_artist(candidate)
    return UNKNOWN_ARTIST


def resolve_album(naming: TrackNaming) -> str:
    if naming.album and naming.album.strip():
        return normalize_album(naming.album)
    return SINGLES_ALBUM if (naming.title or naming.fallback_stem) else UNKNOWN_ALBUM


def format_track_number(track_no: int, disc_no: int) -> str:
    """`02`, or `302` for disc 3 track 2. Empty when the track number is unknown."""
    if track_no <= 0:
        return ""
    if disc_no >= 2:
        return f"{disc_no}{track_no:02d}"
    return f"{track_no:02d}"


def resolve_title(naming: TrackNaming) -> str:
    if naming.title and naming.title.strip():
        return naming.title.strip()
    if naming.fallback_stem:
        return naming.fallback_stem
    return "Unknown Track"


def build_filename(naming: TrackNaming) -> str:
    """`NN - Title.ext`, or `Title.ext` when no track number is known."""
    number = format_track_number(naming.track_no, naming.disc_no)
    title = sanitize_component(resolve_title(naming), fallback="Unknown Track")
    extension = naming.extension if naming.extension.startswith(".") else f".{naming.extension}"
    stem = f"{number} - {title}" if number else title
    return f"{stem}{extension.lower()}"


def build_relative_path(naming: TrackNaming) -> Path:
    """`<Album Artist>/<Album>/<NN> - <Title>.<ext>`, all components sanitized."""
    artist = sanitize_component(resolve_album_artist(naming), fallback=UNKNOWN_ARTIST)
    album = sanitize_component(resolve_album(naming), fallback=UNKNOWN_ALBUM)
    return Path(artist) / album / build_filename(naming)


def build_path(library_root: str | Path, naming: TrackNaming) -> Path:
    return Path(library_root) / build_relative_path(naming)


def naming_from_track(track) -> TrackNaming:
    """Adapt a `music.models.Track` without importing it (keeps this module pure)."""
    extension = Path(track.path).suffix or ".mp3"
    return TrackNaming(
        title=track.title,
        artist=track.artist,
        album=track.album,
        album_artist=track.album_artist,
        track_no=track.track_no,
        disc_no=track.disc_no,
        is_compilation=track.is_compilation,
        extension=extension,
        fallback_stem=Path(track.path).stem,
    )


def is_already_organized(current_path: str | Path, library_root: str | Path,
                         naming: TrackNaming) -> bool:
    """True when the file already sits at its computed destination.

    Case-insensitive: on a case-insensitive filesystem a "move" that only
    changes case is pointless and sometimes destructive.
    """
    try:
        current_rel = Path(current_path).resolve().relative_to(Path(library_root).resolve())
    except (ValueError, OSError):
        return False
    return str(current_rel).lower() == str(build_relative_path(naming)).lower()
