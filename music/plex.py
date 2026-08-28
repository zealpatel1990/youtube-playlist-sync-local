"""
Plex music layout rules.

Pure functions — no IO, no ORM, no settings beyond what is passed in — so the
whole naming policy is exhaustively unit-testable, which matters because
getting it wrong means moving thousands of files into the wrong shape.

Reference: https://support.plex.tv/articles/200265296-adding-music-media-from-folders/

    Music/Artist/Album/TrackNumber - TrackName.ext
    Music/Various Artists/Album/TrackNumber - TrackName.ext

Multi-disc albums prepend the disc number to the track number, so disc 3
track 2 becomes `302 - Track Name.mp3`.

One judgement call worth stating: we prepend the disc number only when it is
2 or higher. A single-disc rip that happens to tag `disc=1` would otherwise
become `101 - …`, which is worse than `01 - …` and is not what the guide
intends. Plex reads the disc number from the embedded tag regardless — and the
article is explicit that the tag is what must be correct — so the filename
convention is the secondary signal here, not the authority.
"""

from __future__ import annotations

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


def resolve_album_artist(naming: TrackNaming) -> str:
    """The artist folder name.

    Compilations go to `Various Artists` per the guide; the per-track `artist`
    tag still carries the real performer, which is what Plex reads to attribute
    individual tracks.
    """
    if naming.is_compilation:
        return VARIOUS_ARTISTS
    for candidate in (naming.album_artist, naming.artist):
        if candidate and candidate.strip():
            return candidate.strip()
    return UNKNOWN_ARTIST


def resolve_album(naming: TrackNaming) -> str:
    if naming.album and naming.album.strip():
        return naming.album.strip()
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

    Compared case-insensitively on the relative part, because the HDD may be
    mounted from a case-insensitive filesystem and a spurious "move" that only
    changes case is both pointless and, on some filesystems, destructive.
    """
    try:
        current_rel = Path(current_path).resolve().relative_to(Path(library_root).resolve())
    except (ValueError, OSError):
        return False
    return str(current_rel).lower() == str(build_relative_path(naming)).lower()
