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
import importlib.util
import logging
import re
import threading

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


async def _recognize_async(ctx: IdentifyContext, timeout: float) -> dict:
    # Imported here, inside the running loop: shazamio builds an aiohttp client
    # whose connector binds to whatever loop is current at construction time, so
    # a Shazam() made outside this coroutine would attach to the wrong one.
    # It is also the lazy import that keeps a missing shazamio from breaking the
    # rest of the app at import.
    from shazamio import Shazam

    shazam = Shazam()
    return await asyncio.wait_for(shazam.recognize(str(ctx.path)), timeout=timeout)


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
