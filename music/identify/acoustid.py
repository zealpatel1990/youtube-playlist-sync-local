"""Tier 2 — AcoustID / Chromaprint -> MusicBrainz.

The only provider that supplies disc and track number, which is what the Plex
layout is built from. Uses `fpcalc` plus stdlib urllib rather than pyacoustid,
to avoid a compiled dependency on armv7.
"""

from __future__ import annotations

import gzip
import json
import logging
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

from music.core.ratelimit import RATE_LIMIT_WAIT_SECONDS, RateLimiter

from . import matching
from .base import IdentifyContext, Provider, TrackMetadata

log = logging.getLogger("music.identify")

LOOKUP_URL = "https://api.acoustid.org/v2/lookup"

#: Meta flags, SPACE separated — NOT "+" separated. urlencode percent-encodes a
#: literal "+" as "%2B", so "recordings+releases" reaches AcoustID as one
#: unknown flag and the response comes back with results but zero recordings
#: attached. A space encodes to "+" on the wire, which is what the server wants.
#: Measured on one real file: "+"-joined gave 11 results and 0 recordings;
#: space-joined gave 11 results, 11 recordings and 103 releases.
#:
#: `tracks` carries the medium/track position — without it the disc and track
#: numbers, the whole point of this provider, are absent. `compress` asks for a
#: gzipped body, which urllib does not decode; `_read_body` handles that.
LOOKUP_META = "recordings releases tracks compress"

#: Seconds of audio Chromaprint reads. AcoustID's index is built from the first
#: two minutes, so more buys no accuracy and costs real ARMv7 CPU.
FPCALC_LENGTH_SECONDS = 120

#: Fingerprinting gets its own budget rather than PROVIDER_TIMEOUT_SECONDS,
#: which exists to bound a *network* call. This is bounded CPU work — decoding
#: at most FPCALC_LENGTH_SECONDS of audio — and how long it takes depends
#: entirely on how busy the box is. Measured here: 8s on an idle container, but
#: over 30s with several workers competing, which silently disabled AcoustID on
#: exactly the tracks a loaded queue was working through. Abandoning it halfway
#: wastes the decode and drops the one provider that supplies track numbers.
FPCALC_TIMEOUT_SECONDS = 300

#: A refusal to allocate an unbounded body into 1GB of shared RAM.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

#: MusicBrainz's special-purpose artist for compilations.
VARIOUS_ARTISTS_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"
VARIOUS_ARTISTS = "Various Artists"

#: Ranking value when the duration is unknown, so every candidate ties and the
#: tie-break decides instead.
_NO_DURATION_MATCH = 10_000

USER_AGENT = "music-manager/1.0 (+https://acoustid.org/)"


# Module-level: the chain is rebuilt whenever settings change, and a
# per-instance bucket would start full every time and limit nothing.

_limiter: RateLimiter | None = None
_limiter_lock = threading.Lock()


def rate_limiter() -> RateLimiter:
    global _limiter
    with _limiter_lock:
        if _limiter is None:
            _limiter = RateLimiter(settings.ACOUSTID_RATE_PER_SEC)
        return _limiter


def reset_for_tests() -> None:
    global _limiter
    with _limiter_lock:
        _limiter = None


class AcoustidProvider(Provider):
    """Chromaprint fingerprint → AcoustID → MusicBrainz recording and release."""

    name = "acoustid"

    def unavailable_reason(self) -> str:
        if not settings.ACOUSTID_API_KEY:
            return (
                "ACOUSTID_API_KEY is not set; get a free key at "
                "https://acoustid.org/new-application"
            )
        if not shutil.which(settings.FPCALC_PATH):
            return (
                f"the Chromaprint binary {settings.FPCALC_PATH!r} was not found on "
                f"PATH; install it with: apt install libchromaprint-tools"
            )
        return ""

    def identify(self, ctx: IdentifyContext) -> TrackMetadata | None:
        if not self._ensure_fingerprint(ctx):
            return None

        payload = self._lookup(ctx.fingerprint, ctx.duration)
        if payload is None:
            return None

        return parse_lookup(payload, ctx)

    # --- fingerprinting -------------------------------------------------

    def _ensure_fingerprint(self, ctx: IdentifyContext) -> bool:
        """Fill `ctx.fingerprint` and `ctx.duration`, running fpcalc only if needed.

        The lookup needs both, so a cached fingerprint with an unknown duration
        is not enough to skip the decode.
        """
        if ctx.fingerprint and ctx.duration > 0:
            log.debug("acoustid: reusing the stored fingerprint for %s", ctx.path.name)
            return True

        result = self._run_fpcalc(ctx)
        if result is None:
            return False

        fingerprint, duration = result
        if not fingerprint or duration <= 0:
            log.warning("acoustid: fpcalc returned nothing usable for %s", ctx.path)
            return False

        ctx.fingerprint = fingerprint
        ctx.duration = duration
        return True

    def _run_fpcalc(self, ctx: IdentifyContext) -> tuple[str, int] | None:
        command = [
            settings.FPCALC_PATH,
            "-json",
            "-length",
            str(FPCALC_LENGTH_SECONDS),
            str(ctx.path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=FPCALC_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.warning(
                "acoustid: fpcalc timed out after %.0fs on %s — the box is very "
                "busy; lower WORKER_THREADS if this recurs",
                FPCALC_TIMEOUT_SECONDS, ctx.path,
            )
            return None
        except (OSError, ValueError) as exc:
            log.warning("acoustid: could not run %s: %s", settings.FPCALC_PATH, exc)
            return None

        # Deliberately NOT gated on the exit code. fpcalc routinely exits
        # non-zero while still writing a good fingerprint to stdout — it reports
        # trouble decoding the final partial frame ("ERROR: Error decoding audio
        # frame (End of file)", exit 3) after already fingerprinting the audio.
        # Treating that as failure disables AcoustID on every file, invisibly:
        # the chain just falls through to Shazam and Gemini and looks fine.
        # The output is the authority, not the exit code.
        try:
            data = json.loads(completed.stdout or "{}")
        except ValueError:
            log.warning(
                "acoustid: fpcalc produced no usable output for %s (exit %s): %s",
                ctx.path, completed.returncode,
                (completed.stderr or "").strip()[:300],
            )
            return None

        fingerprint = str(data.get("fingerprint") or "")
        if not fingerprint:
            log.warning(
                "acoustid: fpcalc returned no fingerprint for %s (exit %s): %s",
                ctx.path, completed.returncode,
                (completed.stderr or "").strip()[:300],
            )
            return None

        if completed.returncode != 0:
            log.debug(
                "acoustid: fpcalc exited %s but produced a fingerprint for %s: %s",
                completed.returncode, ctx.path,
                (completed.stderr or "").strip()[:200],
            )

        # fpcalc reports a float; Track.duration is an integer number of seconds.
        duration = int(round(float(data.get("duration") or 0.0)))
        return fingerprint, duration

    # --- lookup ---------------------------------------------------------

    def _lookup(self, fingerprint: str, duration: int) -> dict | None:
        if not rate_limiter().acquire(timeout=RATE_LIMIT_WAIT_SECONDS):
            log.warning("acoustid: rate limiter is saturated; skipping this lookup")
            return None

        body = urllib.parse.urlencode(
            {
                "client": settings.ACOUSTID_API_KEY,
                "format": "json",
                "meta": LOOKUP_META,
                "duration": duration,
                "fingerprint": fingerprint,
            }
        ).encode("ascii")

        # POST, not GET: a fingerprint is kilobytes of base64, past what some
        # proxies allow in a URL.
        request = urllib.request.Request(
            LOOKUP_URL,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept-Encoding": "gzip",
                "User-Agent": USER_AGENT,
            },
        )

        try:
            with urllib.request.urlopen(
                request, timeout=settings.PROVIDER_TIMEOUT_SECONDS
            ) as response:
                raw = _read_body(response)
        except urllib.error.HTTPError as exc:
            log.warning("acoustid: lookup returned HTTP %s", exc.code)
            return None
        except (urllib.error.URLError, OSError) as exc:
            log.warning("acoustid: lookup failed: %s", exc)
            return None

        try:
            return json.loads(raw)
        except ValueError:
            # Also the path a body truncated by MAX_RESPONSE_BYTES takes.
            log.warning("acoustid: lookup returned an unparseable body")
            return None


def _read_body(response) -> str:
    """Read at most MAX_RESPONSE_BYTES, decompressing if the server gzipped it.

    urllib does not decode gzip, so `compress` would hand us a binary blob.
    """
    raw = response.read(MAX_RESPONSE_BYTES)
    if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError):
            log.warning("acoustid: response claimed gzip but did not decompress")
            return ""
    return raw.decode("utf-8", errors="replace")


# --- parsing ------------------------------------------------------------
#
# Plain functions over plain dicts, so the response shape is testable against a
# captured payload with no network, subprocess or settings.


def parse_lookup(payload: dict, ctx: IdentifyContext) -> TrackMetadata | None:
    """Turn a lookup response into metadata, or None if nothing matched."""
    if payload.get("status") != "ok":
        log.warning(
            "acoustid: lookup reported %s", (payload.get("error") or {}).get("message")
        )
        return None

    results = [r for r in (payload.get("results") or []) if isinstance(r, dict)]
    if not results:
        return None

    best = max(results, key=lambda r: _as_float(r.get("score")))
    score = _as_float(best.get("score"))
    if score <= 0:
        return None

    recording = _pick_recording(best.get("recordings") or [], ctx.duration, ctx.hint_title)
    if recording is None:
        # A match with no MusicBrainz recording attached: nothing to write.
        log.debug("acoustid: %s matched but carries no recording", ctx.path.name)
        return None

    artist, _ = _artist_credit(recording)
    release = _pick_release(recording.get("releases") or [])

    album = ""
    album_artist = ""
    is_compilation = False
    year = 0
    disc_no = 0
    track_no = 0
    release_id = ""
    cover_url = ""

    if release is not None:
        album = str(release.get("title") or "").strip()
        album_artist, is_compilation = _artist_credit(release)
        year = _release_year(release)
        disc_no, track_no = _track_position(release)
        release_id = str(release.get("id") or "")
        if release_id:
            # No request here; the tagger fetches it later under its own timeout
            # and tolerates the 404 that releases without art return.
            cover_url = f"https://coverartarchive.org/release/{release_id}/front-500"

    if is_compilation:
        album_artist = VARIOUS_ARTISTS

    return TrackMetadata(
        title=str(recording.get("title") or "").strip(),
        artist=artist,
        album=album,
        album_artist=album_artist,
        track_no=track_no,
        disc_no=disc_no,
        year=year,
        is_compilation=is_compilation,
        musicbrainz_recording_id=str(recording.get("id") or ""),
        musicbrainz_release_id=release_id,
        cover_url=cover_url,
        confidence=min(1.0, max(0.0, score)),
        provider=AcoustidProvider.name,
    )


def _pick_recording(recordings: list, duration: int, hint_title: str = "") -> dict | None:
    """The recording that best matches the file, by title first and length second.

    One fingerprint routinely matches a studio cut, a radio edit, three live
    versions and several covers. Duration alone chooses badly among them: a file
    titled "Billie Eilish - when the party's over" was filed under Poté because
    that cover's length was one second closer than the alternative. When the
    file's own title is known it outranks duration, which only ever separated
    near-identical lengths.
    """
    candidates = [
        r for r in recordings if isinstance(r, dict) and str(r.get("title") or "").strip()
    ]
    if not candidates:
        return None

    def rank(recording: dict) -> tuple[int, int, int]:
        title = str(recording.get("title") or "")
        artist, _ = _artist_credit(recording)
        recorded = int(_as_float(recording.get("duration")))
        delta = (
            abs(recorded - duration)
            if duration > 0 and recorded > 0
            else _NO_DURATION_MATCH
        )
        named = 0 if matching.shares_a_word(artist, hint_title) else 1
        derivative = 1 if matching.looks_like_a_different_recording(
            title, artist, hint_title
        ) else 0
        return (derivative, named, delta)

    return min(candidates, key=rank)


def _pick_release(releases: list) -> dict | None:
    """The release that tells us where the track sits, else the oldest.

    A medium/track position keeps the numbers we came for; the earliest year
    then picks the original over a later reissue.
    """
    candidates = [r for r in releases if isinstance(r, dict)]
    if not candidates:
        return None

    def rank(release: dict) -> tuple[int, int]:
        has_position = 0 if _track_position(release) != (0, 0) else 1
        return (has_position, _release_year(release) or 9999)

    return min(candidates, key=rank)


def _track_position(release: dict) -> tuple[int, int]:
    """(disc_no, track_no) from the release's medium data, or (0, 0).

    Only the matched medium and track are in the response, so the first entry
    carrying a position is the one we want.
    """
    for medium in release.get("mediums") or []:
        if not isinstance(medium, dict):
            continue
        for track in medium.get("tracks") or []:
            if not isinstance(track, dict):
                continue
            position = int(_as_float(track.get("position")))
            if position > 0:
                return int(_as_float(medium.get("position"))), position
    return (0, 0)


def _artist_credit(entity: dict) -> tuple[str, bool]:
    """The credited artist string, and whether it is the Various Artists marker.

    MusicBrainz splits "A feat. B" into two artists plus a join phrase.
    """
    artists = [a for a in (entity.get("artists") or []) if isinstance(a, dict)]
    if not artists:
        return "", False

    parts = []
    for index, artist in enumerate(artists):
        name = str(artist.get("name") or "").strip()
        if not name:
            continue
        parts.append(name)
        joinphrase = str(artist.get("joinphrase") or "")
        if joinphrase and index < len(artists) - 1:
            parts.append(joinphrase)
    credit = "".join(parts).strip()

    is_various = any(artist.get("id") == VARIOUS_ARTISTS_MBID for artist in artists) or (
        credit.casefold() == VARIOUS_ARTISTS.casefold()
    )
    return credit, is_various


def _release_year(release: dict) -> int:
    date = release.get("date")
    if isinstance(date, dict):
        return int(_as_float(date.get("year")))
    return 0


def _as_float(value) -> float:
    """Coerce whatever the API sent into a number, else 0.0. A None or a string
    would otherwise raise inside a `max()` key and lose the whole result set."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
