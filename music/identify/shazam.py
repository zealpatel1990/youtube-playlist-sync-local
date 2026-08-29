"""Tier 3 — Shazam, via the unofficial `shazamio` client.

Reached only for what AcoustID could not match; gives no track or disc number.
`shazamio` is asyncio-only while every caller is a plain worker thread, so each
call gets exactly one event loop, created and closed around it — see `_recognize`.
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

from music.core.ratelimit import RATE_LIMIT_WAIT_SECONDS, RateLimiter

from .base import IdentifyContext, Provider, TrackMetadata

log = logging.getLogger("music.identify")

#: Shazam returns no score of its own — a match carries only `offset`,
#: `timeskew` and `frequencyskew`. The skews are how far the audio had to be
#: stretched in time and pitch to line up with the reference, so a genuine
#: same-recording match sits near zero and a forced one does not. Measured on
#: real files (|timeskew| + |frequencyskew|):
#:
#:   0.0001  Chatte Batte          correct
#:   0.0001  Dilliwaali Girlfriend correct
#:   0.0022  Hookah Bar            correct
#:   0.0085  Rinku Bhabhi          WRONG ("Lil Muillet - *Horror Sounds*")
#:   0.0124  Shape of You (Carnatic cover)  WRONG
#:
#: A flat score let those two outrank a correct later provider — Gemini names
#: the Rinku Bhabhi track properly but never ran, because a constant 0.80 beat
#: the threshold first.
CONFIDENCE_CEILING = 0.88

#: Chosen so the correct matches above stay comfortably over
#: IDENTIFY_MIN_CONFIDENCE (0.5) and both wrong ones fall well under it.
SKEW_PENALTY = 76.0

#: When a response carries no usable skew, neither reward nor reject it.
CONFIDENCE_UNKNOWN = 0.6

#: A "Released" string arrives as "1979", "1979-11-30" or "30 November 1979".
_YEAR = re.compile(r"(1[89]\d{2}|20\d{2})")


# Module-level, as in AcoustID: a bucket rebuilt with the chain would start
# full every time and limit nothing.

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
        # at startup does not pay shazamio's substantial import cost.
        if importlib.util.find_spec("shazamio") is None:
            return "the shazamio package is not installed (pip install shazamio)"
        return ""

    def identify(self, ctx: IdentifyContext) -> TrackMetadata | None:
        if not rate_limiter().acquire(timeout=RATE_LIMIT_WAIT_SECONDS):
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
            # extension: the exception surface is too wide to be worth a
            # traceback per file.
            log.warning("shazam: recognition failed for %s: %s", ctx.path.name, exc)
            return None

        return parse_recognition(response)


def _recognize(ctx: IdentifyContext, timeout: float) -> dict:
    """Run the async recognizer on one event loop, and always close it —
    `asyncio.run` closes even when the body raises, a hand-rolled loop leaks."""
    return asyncio.run(_recognize_async(ctx, timeout))


#: Seconds of audio sent to Shazam. Its own app matches from a few seconds of
#: a phone microphone, so more does not help — and sending a whole track costs
#: decode time and upload for no gain.
EXCERPT_SECONDS = 15

#: WHERE those seconds come from matters more than how many. The opening of a
#: track is the worst place to look: intros, silence, spoken introductions and
#: label idents carry nothing to recognise, which is how a comedy sketch got
#: matched to "*Horror Sounds*". A third of the way in lands on the actual
#: music for almost anything.
EXCERPT_START_FRACTION = 0.33

#: Never seek past this, so a long mix still gets a sensible window.
EXCERPT_MAX_START_SECONDS = 90.0


async def _recognize_async(ctx: IdentifyContext, timeout: float) -> dict:
    # Constructed here, inside the running loop: shazamio builds an aiohttp
    # client whose connector binds to whatever loop is current at construction
    # time, so a Shazam() made outside this coroutine attaches to the wrong one.
    # The import is also what keeps a missing shazamio from breaking the app.
    from shazamio import Shazam

    shazam = Shazam()
    with _excerpt(ctx.path, ctx.duration) as source:
        return await asyncio.wait_for(shazam.recognize(str(source)), timeout=timeout)


@contextlib.contextmanager
def _excerpt(path: Path, duration: int = 0):
    """Yield a short mono WAV of `path`, falling back to the file itself.

    Not just `shazam.recognize(path)` because shazamio decodes with symphonia,
    which reads MP3/WAV/FLAC but NOT the WebM/Opus that YouTube serves and that
    AUDIO_FORMAT=native keeps: handed one it demuxes it as MP3 and produces
    thousands of "skipping junk" warnings and no match. Excerpting is also far
    cheaper than decoding a whole track on a Pi.

    If ffmpeg is unavailable the original path is yielded.
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
                # -ss before -i seeks by keyframe without decoding the skipped
                # part, so starting later costs nothing.
                "-ss", f"{_excerpt_start(duration):.1f}",
                "-t", str(EXCERPT_SECONDS),
                "-i", str(path),
                "-ac", "1",
                "-ar", "16000",
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


def _excerpt_start(duration: int) -> float:
    """Where to take the sample from. Never the opening — see the constants."""
    if duration <= EXCERPT_SECONDS * 2:
        return 0.0
    return min(duration * EXCERPT_START_FRACTION, EXCERPT_MAX_START_SECONDS)


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
        # `coverart` is a thumbnail; the HQ variant is usable for Plex.
        cover_url = str(images.get("coverarthq") or images.get("coverart") or "").strip()

    confidence = _confidence_from_skew(response)

    # The label is logged but not carried: TrackMetadata has no publisher field
    # and the Plex layout never reads one.
    log.debug(
        "shazam: matched '%s - %s' (album=%r label=%r year=%s confidence=%.2f)",
        artist, title, album, label, year, confidence,
    )

    return TrackMetadata(
        title=title,
        artist=artist,
        album=album,
        # Empty on purpose: Shazam has no album-artist concept, and copying
        # `artist` in would mislabel every compilation track. `plex.py` falls
        # back to the artist when this is blank.
        album_artist="",
        year=year,
        genre=genre,
        cover_url=cover_url,
        confidence=confidence,
        provider=ShazamProvider.name,
    )


def _confidence_from_skew(response: dict) -> float:
    """Derive a confidence from how far the audio had to be bent to match.

    Shazam gives no score, so the skews stand in for one: a real match needs
    almost no correction, a forced one needs a lot. Calibrated against the
    measurements listed with the constants above.
    """
    matches = (response or {}).get("matches")
    if not isinstance(matches, list) or not matches:
        return CONFIDENCE_UNKNOWN

    best = None
    for match in matches:
        if not isinstance(match, dict):
            continue
        # Absent skew is unknown, not zero: a match dict carrying neither key
        # would otherwise score as a flawless one.
        if "timeskew" not in match and "frequencyskew" not in match:
            continue
        try:
            skew = abs(float(match.get("timeskew") or 0.0)) + abs(
                float(match.get("frequencyskew") or 0.0)
            )
        except (TypeError, ValueError):
            continue
        best = skew if best is None else min(best, skew)

    if best is None:
        return CONFIDENCE_UNKNOWN
    return max(0.0, min(CONFIDENCE_CEILING, CONFIDENCE_CEILING - SKEW_PENALTY * best))


def _section_value(track: dict, key: str) -> str:
    """Read one labelled value out of the response's `sections` metadata.

    The fields live in `sections[].metadata[]` as `{"title": ..., "text": ...}`.
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
