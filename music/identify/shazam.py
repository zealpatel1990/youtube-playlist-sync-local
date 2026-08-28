"""
Tier 3 — Shazam, via the unofficial `shazamio` client.

Reached only for what AcoustID could not match: rips, remixes, live cuts and
YouTube-only audio that was never pressed to a release and therefore has no
MusicBrainz entry to find. In exchange it gives no track or disc number, so a
Shazam-identified file lands in its album folder without a leading number —
correct, but less than AcoustID would have managed.

It is also the most expensive tier per file. Signature generation runs in
`shazamio_core` and is genuinely CPU-heavy on a 900MHz Cortex-A7, which is why
it sits behind the free local tier and the cheap fingerprint tier rather than
being the first thing tried (as it was in the previous version, where it also
ran three attempts with 5-second sleeps between them for every failure).

The async boundary is the delicate part. `shazamio` is asyncio-only while every
caller here is a plain worker thread, so each call gets exactly one event loop,
created and closed around it — see `_recognize`.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import tempfile
import importlib.util
import logging
import re
import threading
from pathlib import Path

from django.conf import settings

from music.core.ratelimit import RateLimiter

from .base import IdentifyContext, Provider, TrackMetadata

log = logging.getLogger("music.identify")

#: Recognition, not inference — but an unofficial endpoint matching a lossy
#: transcode, so below what the fingerprint tier earns and well above Gemini.
CONFIDENCE = 0.8

#: Matches a year anywhere in a "Released" string, which arrives as "1979",
#: "1979-11-30" or "30 November 1979" depending on the release.
_YEAR = re.compile(r"(1[89]\d{2}|20\d{2})")


# --- shared limiter -----------------------------------------------------
#
# Module-level for the same reason as AcoustID's: a bucket rebuilt with the
# chain would start full every time and limit nothing.

_limiter: RateLimiter | None = None
_limiter_lock = threading.Lock()


def rate_limiter() -> RateLimiter:
    global _limiter
    with _limiter_lock:
        if _limiter is None:
            _limiter = RateLimiter(settings.SHAZAM_RATE_PER_MIN / 60.0)
        return _limiter


def reset_for_tests() -> None:
    global _limiter
    with _limiter_lock:
        _limiter = None


class ShazamProvider(Provider):
    """Audio recognition against Shazam's catalogue."""

    name = "shazam"

    def unavailable_reason(self) -> str:
        if not settings.SHAZAM_ENABLED:
            return "SHAZAM_ENABLED is off"
        # find_spec locates the package without executing it, so a chain built
        # at startup does not pay shazamio's (substantial) import cost, and a
        # package that is present but broken fails inside identify() where it
        # is caught, rather than here where it would look like a config error.
        if importlib.util.find_spec("shazamio") is None:
            return "the shazamio package is not installed (pip install shazamio)"
        return ""

    def identify(self, ctx: IdentifyContext) -> TrackMetadata | None:
        if not rate_limiter().acquire(timeout=settings.PROVIDER_TIMEOUT_SECONDS):
            log.warning("shazam: rate limiter is saturated; skipping %s", ctx.path.name)
            return None

        try:
            response = _recognize(ctx, settings.PROVIDER_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            log.warning(
                "shazam: recognition of %s exceeded %.0fs",
                ctx.path.name, settings.PROVIDER_TIMEOUT_SECONDS,
            )
            return None
        except ImportError as exc:
            log.warning("shazam: shazamio could not be imported: %s", exc)
            return None
        except Exception as exc:
            # An unofficial endpoint behind an HTTP client behind a Rust
            # extension: the exception surface is wide and none of it is worth a
            # traceback per file. The chain's own handler keeps tracebacks for
            # the genuinely unexpected.
            log.warning("shazam: recognition failed for %s: %s", ctx.path.name, exc)
            return None

        return parse_recognition(response)


def _recognize(ctx: IdentifyContext, timeout: float) -> dict:
    """Run the async recognizer on one event loop, and always close it.

    `asyncio.run` is exactly the right tool here despite the name suggesting a
    main-function helper: it creates one loop, cancels anything still pending on
    the way out, shuts down async generators, and closes the loop even when the
    body raises. A hand-rolled `new_event_loop`/`run_until_complete` reliably
    forgets one of those and leaks a socket per call — which on a long-running
    single process is a slow bleed rather than a visible bug.
    """
    return asyncio.run(_recognize_async(ctx, timeout))


#: Seconds of audio sent to Shazam. Its own app matches from a few seconds of
#: microphone input; more than this buys nothing and costs decode time.
EXCERPT_SECONDS = 15


async def _recognize_async(ctx: IdentifyContext, timeout: float) -> dict:
    # Imported here, inside the running loop: shazamio builds an aiohttp client
    # whose connector binds to whatever loop is current at construction time, so
    # a Shazam() made outside this coroutine would attach to the wrong one.
    # It is also the lazy import that keeps a missing shazamio from breaking the
    # rest of the app at import.
    from shazamio import Shazam

    shazam = Shazam()
    with _excerpt(ctx.path) as source:
        return await asyncio.wait_for(shazam.recognize(str(source)), timeout=timeout)


@contextlib.contextmanager
def _excerpt(path: Path):
    """Yield a short mono WAV of `path`, falling back to the file itself.

    Two reasons this is not just `shazam.recognize(path)`:

    **Format.** shazamio decodes with symphonia, which reads MP3, WAV, FLAC and
    friends — but not the WebM/Opus that YouTube actually serves and that
    AUDIO_FORMAT=native keeps. Handed one, it tries to demux it as MP3 and
    produces thousands of "skipping junk" warnings and no match. Letting ffmpeg
    do the decoding makes this provider work on whatever the library holds
    instead of only on MP3.

    **Cost.** Shazam matches from a few seconds. Decoding fifteen seconds at
    16kHz mono is far cheaper than a whole four-minute track — which matters on
    a Pi, where this is the CPU-heaviest provider in the chain — and it uploads
    a fraction of the bytes.

    If ffmpeg is unavailable the original path is yielded, so an MP3 library
    still works exactly as before.
    """
    ffmpeg = _ffmpeg_binary()
    if ffmpeg is None:
        yield path
        return

    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    handle.close()
    target = Path(handle.name)
    try:
        completed = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-t", str(EXCERPT_SECONDS),
                "-i", str(path),
                "-ac", "1",          # mono: Shazam's fingerprint is mono anyway
                "-ar", "16000",      # 16kHz is ample for recognition
                "-c:a", "pcm_s16le",
                "-y", str(target),
            ],
            capture_output=True, text=True, timeout=120,
        )
        if completed.returncode == 0 and target.stat().st_size > 1024:
            yield target
        else:
            log.debug(
                "shazam: could not excerpt %s (%s); using the file as-is: %s",
                path, completed.returncode, (completed.stderr or "").strip()[:200],
            )
            yield path
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("shazam: excerpt failed for %s (%s); using the file as-is", path, exc)
        yield path
    finally:
        target.unlink(missing_ok=True)


def _ffmpeg_binary() -> str | None:
    configured = getattr(settings, "FFMPEG_LOCATION", "")
    if configured:
        candidate = Path(configured)
        # The setting may name the directory or the executable itself.
        binary = candidate / "ffmpeg" if candidate.is_dir() else candidate
        if binary.exists():
            return str(binary)
    return shutil.which("ffmpeg")


def parse_recognition(response: dict | None) -> TrackMetadata | None:
    """Turn a shazamio response into metadata, or None when nothing matched."""
    track = (response or {}).get("track")
    if not isinstance(track, dict):
        return None

    title = str(track.get("title") or "").strip()
    artist = str(track.get("subtitle") or "").strip()
    if not title or not artist:
        return None

    album = _section_value(track, "Album")
    label = _section_value(track, "Label")
    year = _year(_section_value(track, "Released"))
    genre = ""
    genres = track.get("genres")
    if isinstance(genres, dict):
        genre = str(genres.get("primary") or "").strip()

    images = track.get("images")
    cover_url = ""
    if isinstance(images, dict):
        # The HQ variant is the same art at a usable size for Plex; `coverart`
        # is a thumbnail.
        cover_url = str(images.get("coverarthq") or images.get("coverart") or "").strip()

    # The label is read but not carried: TrackMetadata has no publisher field,
    # and adding one to Track for a value the Plex layout never reads would be
    # schema for schema's sake. It earns its place in the log line instead.
    log.debug(
        "shazam: matched '%s - %s' (album=%r label=%r year=%s)",
        artist, title, album, label, year,
    )

    return TrackMetadata(
        title=title,
        artist=artist,
        album=album,
        # Left empty deliberately. Shazam has no album-artist concept, and
        # copying `artist` into it would mislabel every compilation track;
        # `plex.py` already falls back to the artist when this is blank.
        album_artist="",
        year=year,
        genre=genre,
        cover_url=cover_url,
        confidence=CONFIDENCE,
        provider=ShazamProvider.name,
    )


def _section_value(track: dict, key: str) -> str:
    """Read one labelled value out of the response's `sections` metadata.

    The interesting fields live in `sections[].metadata[]` as
    `{"title": "Album", "text": "..."}` pairs. The previous version dug them out
    with a recursive "find the deepest key anywhere in the document" search,
    which had no bound and would happily return a value from an unrelated
    subtree; this walk knows exactly where it is looking.
    """
    for section in track.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for entry in section.get("metadata") or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("title") or "").casefold() == key.casefold():
                return str(entry.get("text") or "").strip()
    return ""


def _year(text: str) -> int:
    match = _YEAR.search(text or "")
    return int(match.group(1)) if match else 0
