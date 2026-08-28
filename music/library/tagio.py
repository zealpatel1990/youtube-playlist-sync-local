"""Reading and writing embedded tags, via mutagen.

Every writer assembles the complete tag set on one mutagen object and calls
`save()` exactly once: a tag write is a whole-file rewrite whenever the tag
outgrows the padding, which on SD/USB storage costs an erase cycle.

Reading never raises; a scan meets truncated and half-written files and must
survive them. Writing raises `TagWriteError`, because the caller is usually
about to move the file and needs to know the tags did not take.
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path

from music.identify.base import TrackMetadata

log = logging.getLogger("music.library.tagio")

__all__ = [
    "TagWriteError",
    "read_tags",
    "read_audio_properties",
    "read_metadata",
    "write_tags",
    "tagging_available",
]

#: Read natively rather than through the easy interface: EasyMP4 has no key for
#: the compilation flag, and a compilation that loses its flag lands under the
#: wrong artist in Plex.
_MP4_SUFFIXES = {".m4a", ".m4b", ".mp4"}
_FLAC_SUFFIXES = {".flac"}
_OGG_SUFFIXES = {".ogg", ".oga", ".opus"}
_MP3_SUFFIXES = {".mp3"}

#: Picard's conventions, which is what makes the tags round-trip through other
#: taggers.
_MB_OWNER = "http://musicbrainz.org"
_MP4_MB_RECORDING = "----:com.apple.iTunes:MusicBrainz Track Id"
_MP4_MB_RELEASE = "----:com.apple.iTunes:MusicBrainz Album Id"


class TagWriteError(RuntimeError):
    """A tag write failed. Reading never raises; writing does, on purpose."""


# --- Lazy mutagen ------------------------------------------------------

_mutagen_module = None
_mutagen_checked = False


def _mutagen():
    """Import mutagen once, remembering failure.

    Not module-level: a broken mutagen must disable tagging, not stop the
    workers booting.
    """
    global _mutagen_module, _mutagen_checked
    if not _mutagen_checked:
        _mutagen_checked = True
        try:
            import mutagen  # noqa: PLC0415 - lazy on purpose, see docstring

            _mutagen_module = mutagen
        except Exception as exc:  # ImportError, but also a bad compiled wheel
            log.error(
                "mutagen is unavailable (%s): tag reading and writing are disabled. "
                "Install it with: pip install mutagen",
                exc,
            )
    return _mutagen_module


def tagging_available() -> bool:
    """True when tags can be read and written. False disables the feature."""
    return _mutagen() is not None


# --- Reading -----------------------------------------------------------


def read_tags(path: Path) -> TrackMetadata:
    """Everything the file claims about itself. Empty metadata if it cannot say."""
    metadata, _duration, _bitrate = read_metadata(path)
    return metadata


def read_audio_properties(path: Path) -> tuple[int, int]:
    """`(duration_seconds, bitrate_kbps)`. 0 means unknown — never None.

    Kilobits, not mutagen's bits per second.
    """
    _metadata, duration, bitrate = read_metadata(path)
    return duration, bitrate


def read_metadata(path: Path) -> tuple[TrackMetadata, int, int]:
    """Tags *and* audio properties from a single open, which halves the seeks."""
    audio, kind = _open(path)
    if audio is None:
        return TrackMetadata(), 0, 0

    info = getattr(audio, "info", None)
    duration = _positive_int(getattr(info, "length", 0) or 0)
    # mutagen reports bits per second; store kbps (see read_audio_properties).
    bitrate = _positive_int((getattr(info, "bitrate", 0) or 0) / 1000)

    tags = getattr(audio, "tags", None)
    if tags is None:
        return TrackMetadata(), duration, bitrate

    try:
        metadata = _read_mp4_tags(tags) if kind == "mp4" else _read_easy_tags(audio)
    except Exception as exc:
        log.warning("could not interpret the tags on %s: %s", path, exc)
        return TrackMetadata(), duration, bitrate

    return metadata, duration, bitrate


def _open(path: Path):
    """Open for reading. Returns `(audio_or_None, kind)`, kind "mp4" or "easy"."""
    mutagen = _mutagen()
    if mutagen is None:
        return None, ""

    suffix = Path(path).suffix.lower()
    try:
        if suffix in _MP4_SUFFIXES:
            from mutagen.mp4 import MP4

            return MP4(str(path)), "mp4"
        # easy=True gives mp3 and m4a a lowercase, human-named key space;
        # FLAC and Ogg already have one, so one accessor covers all four.
        return mutagen.File(str(path), easy=True), "easy"
    except Exception as exc:
        log.warning("could not read %s: %s", path, exc)
        return None, ""


def _read_easy_tags(audio) -> TrackMetadata:
    return TrackMetadata(
        title=_first(audio, "title"),
        artist=_first(audio, "artist"),
        album=_first(audio, "album"),
        album_artist=_first(audio, "albumartist"),
        track_no=_to_int(_first(audio, "tracknumber")),
        disc_no=_to_int(_first(audio, "discnumber")),
        year=_to_year(_first(audio, "date") or _first(audio, "originaldate")),
        genre=_first(audio, "genre"),
        is_compilation=_to_bool(_first(audio, "compilation")),
        musicbrainz_recording_id=_first(audio, "musicbrainz_trackid"),
        musicbrainz_release_id=_first(audio, "musicbrainz_albumid"),
    )


def _read_mp4_tags(tags) -> TrackMetadata:
    track_no, disc_no = 0, 0
    pairs = tags.get("trkn") or []
    if pairs:
        track_no = _positive_int(pairs[0][0])
    pairs = tags.get("disk") or []
    if pairs:
        disc_no = _positive_int(pairs[0][0])

    return TrackMetadata(
        title=_first_value(tags.get("\xa9nam")),
        artist=_first_value(tags.get("\xa9ART")),
        album=_first_value(tags.get("\xa9alb")),
        album_artist=_first_value(tags.get("aART")),
        track_no=track_no,
        disc_no=disc_no,
        year=_to_year(_first_value(tags.get("\xa9day"))),
        genre=_first_value(tags.get("\xa9gen")),
        is_compilation=bool(tags.get("cpil")),
        musicbrainz_recording_id=_first_value(tags.get(_MP4_MB_RECORDING)),
        musicbrainz_release_id=_first_value(tags.get(_MP4_MB_RELEASE)),
    )


# --- value coercion -------------------------------------------------------
#
# Tags are user data from twenty years of taggers: "3/12" track numbers,
# "1994-05-01" dates, bytes where text was expected. Nothing below raises — one
# odd frame must not cost the file its other fields.


def _first(audio, key: str) -> str:
    try:
        return _first_value(audio[key])
    except (KeyError, ValueError, TypeError, AttributeError):
        return ""


def _first_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        value = value[0]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace").strip()
    return str(value).strip()


def _to_int(value) -> int:
    """`"7"`, `"7/12"` and `7` all mean 7. Anything else means unknown."""
    text = str(value or "").strip().split("/")[0].strip()
    if not text:
        return 0
    try:
        return _positive_int(float(text))
    except (TypeError, ValueError):
        return 0


def _to_year(value) -> int:
    match = re.search(r"(\d{4})", str(value or ""))
    return int(match.group(1)) if match else 0


def _to_bool(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(value) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


# --- Writing -----------------------------------------------------------


def write_tags(path: Path, meta: TrackMetadata, *, cover: bytes | None = None) -> None:
    """Write metadata and cover art into the file in a **single** save.

    Plex reads embedded tags in preference to filenames, so this is the write
    that decides where a track appears. Empty values are left alone rather than
    blanked; the exception is the compilation flag, which is *cleared* when
    False, since a stale one moves the album under "Various Artists".
    """
    path = Path(path)
    if _mutagen() is None:
        return  # tagging is disabled; the failure was logged once at import

    suffix = path.suffix.lower()
    try:
        if suffix in _MP3_SUFFIXES:
            _write_mp3(path, meta, cover)
        elif suffix in _MP4_SUFFIXES:
            _write_mp4(path, meta, cover)
        elif suffix in _FLAC_SUFFIXES:
            _write_flac(path, meta, cover)
        elif suffix in _OGG_SUFFIXES:
            _write_ogg(path, meta, cover)
        else:
            _write_generic(path, meta)
    except TagWriteError:
        raise
    except Exception as exc:
        raise TagWriteError(f"could not write tags to {path}: {exc}") from exc


def _write_mp3(path: Path, meta: TrackMetadata, cover: bytes | None) -> None:
    from mutagen.id3 import (
        APIC,
        TALB,
        TCMP,
        TCON,
        TDRC,
        TIT2,
        TPE1,
        TPE2,
        TPOS,
        TRCK,
        TXXX,
        UFID,
    )
    from mutagen.mp3 import MP3

    audio = MP3(str(path))
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    def put(frame) -> None:
        """Replace, not append: ID3 allows duplicate frames, and a second TIT2
        is how a file ends up with two different titles."""
        tags.delall(frame.FrameID)
        tags.add(frame)

    if meta.title:
        put(TIT2(encoding=3, text=[meta.title]))
    if meta.artist:
        put(TPE1(encoding=3, text=[meta.artist]))
    if meta.album:
        put(TALB(encoding=3, text=[meta.album]))
    if meta.album_artist:
        put(TPE2(encoding=3, text=[meta.album_artist]))
    if meta.track_no:
        put(TRCK(encoding=3, text=[str(meta.track_no)]))
    if meta.disc_no:
        put(TPOS(encoding=3, text=[str(meta.disc_no)]))
    if meta.year:
        put(TDRC(encoding=3, text=[str(meta.year)]))
    if meta.genre:
        put(TCON(encoding=3, text=[meta.genre]))

    tags.delall("TCMP")
    if meta.is_compilation:
        tags.add(TCMP(encoding=3, text=["1"]))

    if meta.musicbrainz_recording_id:
        tags.delall(f"UFID:{_MB_OWNER}")
        tags.add(UFID(owner=_MB_OWNER, data=meta.musicbrainz_recording_id.encode()))
    if meta.musicbrainz_release_id:
        tags.delall("TXXX:MusicBrainz Album Id")
        tags.add(
            TXXX(
                encoding=3,
                desc="MusicBrainz Album Id",
                text=[meta.musicbrainz_release_id],
            )
        )

    if cover:
        tags.delall("APIC")
        tags.add(
            APIC(
                encoding=3,
                mime=_cover_mime(cover),
                type=3,  # PictureType.COVER_FRONT
                desc="Cover",
                data=cover,
            )
        )

    audio.save()  # the single write — see the module docstring


def _write_mp4(path: Path, meta: TrackMetadata, cover: bytes | None) -> None:
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

    audio = MP4(str(path))
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    for atom, value in (
        ("\xa9nam", meta.title),
        ("\xa9ART", meta.artist),
        ("\xa9alb", meta.album),
        ("aART", meta.album_artist),
        ("\xa9gen", meta.genre),
        ("\xa9day", str(meta.year) if meta.year else ""),
    ):
        if value:
            tags[atom] = [value]

    if meta.track_no:
        tags["trkn"] = [(meta.track_no, 0)]
    if meta.disc_no:
        tags["disk"] = [(meta.disc_no, 0)]

    tags.pop("cpil", None)
    if meta.is_compilation:
        tags["cpil"] = True

    for atom, value in (
        (_MP4_MB_RECORDING, meta.musicbrainz_recording_id),
        (_MP4_MB_RELEASE, meta.musicbrainz_release_id),
    ):
        if value:
            tags[atom] = [MP4FreeForm(value.encode("utf-8"))]

    if cover:
        image_format = (
            MP4Cover.FORMAT_PNG
            if _cover_mime(cover) == "image/png"
            else MP4Cover.FORMAT_JPEG
        )
        tags["covr"] = [MP4Cover(cover, imageformat=image_format)]

    audio.save()


def _write_flac(path: Path, meta: TrackMetadata, cover: bytes | None) -> None:
    from mutagen.flac import FLAC

    audio = FLAC(str(path))
    _apply_vorbis(audio, meta)
    if cover:
        audio.clear_pictures()
        audio.add_picture(_flac_picture(cover))
    audio.save()


def _write_ogg(path: Path, meta: TrackMetadata, cover: bytes | None) -> None:
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis

    opener = OggOpus if path.suffix.lower() == ".opus" else OggVorbis
    audio = opener(str(path))
    _apply_vorbis(audio, meta)
    if cover:
        # Ogg carries cover art as a base64-encoded FLAC picture block; that is
        # the convention every player that reads Ogg art expects.
        audio["metadata_block_picture"] = [
            base64.b64encode(_flac_picture(cover).write()).decode("ascii")
        ]
    audio.save()


def _apply_vorbis(audio, meta: TrackMetadata) -> None:
    for key, value in (
        ("title", meta.title),
        ("artist", meta.artist),
        ("album", meta.album),
        ("albumartist", meta.album_artist),
        ("tracknumber", str(meta.track_no) if meta.track_no else ""),
        ("discnumber", str(meta.disc_no) if meta.disc_no else ""),
        ("date", str(meta.year) if meta.year else ""),
        ("genre", meta.genre),
        ("musicbrainz_trackid", meta.musicbrainz_recording_id),
        ("musicbrainz_albumid", meta.musicbrainz_release_id),
    ):
        if value:
            audio[key] = [value]

    audio.pop("compilation", None)
    if meta.is_compilation:
        audio["compilation"] = ["1"]


def _write_generic(path: Path, meta: TrackMetadata) -> None:
    """Best effort for containers with no first-class support here (wav, wma).

    A rejected key is not an error; only a save failure propagates.
    """
    mutagen = _mutagen()
    audio = mutagen.File(str(path), easy=True)
    if audio is None:
        log.info("%s is not a format mutagen can tag; leaving it alone", path.name)
        return
    if audio.tags is None:
        try:
            audio.add_tags()
        except Exception:
            log.info("%s cannot carry tags; leaving it alone", path.name)
            return

    written = 0
    for key, value in (
        ("title", meta.title),
        ("artist", meta.artist),
        ("album", meta.album),
        ("albumartist", meta.album_artist),
        ("tracknumber", str(meta.track_no) if meta.track_no else ""),
        ("discnumber", str(meta.disc_no) if meta.disc_no else ""),
        ("date", str(meta.year) if meta.year else ""),
        ("genre", meta.genre),
    ):
        if not value:
            continue
        try:
            audio[key] = [value]
            written += 1
        except Exception:
            log.debug("%s does not support the %s tag", path.suffix, key)

    if written:
        audio.save()


def _flac_picture(cover: bytes):
    from mutagen.flac import Picture

    picture = Picture()
    picture.data = cover
    picture.type = 3  # front cover
    picture.mime = _cover_mime(cover)
    picture.desc = "Cover"
    return picture


def _cover_mime(cover: bytes) -> str:
    """Sniff the image type. Cover art arrives as bytes with no content type."""
    if cover[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if cover[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    log.debug("unrecognised cover art format; declaring it JPEG")
    return "image/jpeg"
