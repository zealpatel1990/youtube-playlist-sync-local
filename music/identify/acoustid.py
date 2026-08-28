"""
Tier 2 — AcoustID / Chromaprint → MusicBrainz. The workhorse.

This provider exists for one field the others cannot give us. Shazam knows what
a song *is*; MusicBrainz knows where it *sits* — which disc, which track number
— and the Plex layout is built from exactly those two numbers
(docs/ARCHITECTURE.md, docs/MUSIC-LIBRARY-MERGE.md). Everything else AcoustID
returns is a bonus; the track/disc position is the reason it is in the chain and
the reason the `tracks` meta flag is requested below.

Two implementation choices worth stating, both driven by the Pi:

**No `pyacoustid`.** The library would add a compiled Chromaprint binding on a
box where armv7 wheels come from piwheels and every new dependency is a
deployment risk (CLAUDE.md). What it does for us is a `fpcalc` subprocess and
one HTTP GET/POST, which is thirty lines of stdlib. `requests` is avoided for
the same reason — `urllib.request` is already installed and takes a `timeout`.

**Bounded work everywhere.** `fpcalc` is given `-length`, so the decode cost is
the same for a three-minute single and a two-hour DJ set; the subprocess carries
a timeout so a wedged decoder is killed rather than holding a worker's lease
(docs/CODE-AUDIT.md A6); the HTTP call carries `PROVIDER_TIMEOUT_SECONDS`; the
response read is capped; and the rate limiter is acquired *with* a timeout, so a
saturated bucket makes the provider pass rather than park a thread on it.
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

from music.core.ratelimit import RateLimiter

from .base import IdentifyContext, Provider, TrackMetadata

log = logging.getLogger("music.identify")

LOOKUP_URL = "https://api.acoustid.org/v2/lookup"

#: `recordings` and `releases` give the names; `tracks` is what makes the
#: `mediums[].tracks[].position` structure appear, and without it the track and
#: disc numbers — the entire point of this provider — are simply absent from
#: the response. `compress` asks for a gzipped body, which urllib does not
#: transparently decode; `_read_body` handles that.
#: Meta flags, SPACE separated — not "+" separated.
#:
#: https://acoustid.org/webservice says the values are "combined with space
#: separation", and this is the difference between the provider working and
#: silently returning nothing. urlencode percent-encodes a literal "+" as
#: "%2B", so "recordings+releases" reaches AcoustID as the single unknown flag
#: `recordings+releases` rather than as two flags, and the response comes back
#: with results but zero recordings attached. A space encodes to "+" on the
#: wire, which is what the server expects.
#:
#: Measured against one real file: "recordings+releases+tracks+compress" gave
#: 11 results and 0 recordings; "recordings releases tracks compress" gave 11
#: results, 11 recordings and 103 releases — including the track and disc
#: numbers that are the whole reason for using AcoustID over Shazam.
#:
#: `tracks` is what carries medium/track position; `compress` shrinks a
#: response that routinely runs to hundreds of releases.
LOOKUP_META = "recordings releases tracks compress"

#: Seconds of audio Chromaprint reads. AcoustID's own index is built from the
#: first two minutes, so more buys no accuracy and costs real ARMv7 CPU.
FPCALC_LENGTH_SECONDS = 120

#: A lookup response is a few KB. The cap is not a tuning knob, it is a refusal
#: to allocate an unbounded body into 1GB of shared RAM.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

#: MusicBrainz's special-purpose artist for compilations.
VARIOUS_ARTISTS_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"
VARIOUS_ARTISTS = "Various Artists"

#: Used to rank recordings when the file's duration is unknown, so every
#: candidate ranks equally and the tie-break (has releases, then API order)
#: decides instead.
_NO_DURATION_MATCH = 10_000

USER_AGENT = "music-manager/1.0 (+https://acoustid.org/)"


# --- shared limiter -----------------------------------------------------
#
# Module-level, because the chain is rebuilt whenever settings change and a
# per-instance bucket would start full every time — which is not a rate limit,
# it is a rate limit shaped hole. AcoustID asks for no more than 3 req/s.

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

        The lookup needs both values, so a cached fingerprint with an unknown
        duration is not enough to skip the decode. When we do run it, the
        results are written back onto the context: the caller persists them onto
        the Track, and a retry after a network failure then costs no CPU at all.
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
                timeout=settings.PROVIDER_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # run() has already killed the child, so no orphan decoder is left
            # burning a core.
            log.warning(
                "acoustid: fpcalc timed out after %.0fs on %s",
                settings.PROVIDER_TIMEOUT_SECONDS, ctx.path,
            )
            return None
        except (OSError, ValueError) as exc:
            log.warning("acoustid: could not run %s: %s", settings.FPCALC_PATH, exc)
            return None

        # Deliberately NOT gated on the exit code. fpcalc routinely exits
        # non-zero while still writing a perfectly good fingerprint to stdout —
        # a real 20-second MP3 produces "ERROR: Error decoding audio frame (End
        # of file)" on stderr and exit status 3, because it reports trouble
        # decoding the final partial frame after it has already fingerprinted
        # the audio. Treating that as failure disabled AcoustID on every file,
        # which is invisible from the outside: the chain simply falls through to
        # Shazam and Gemini and looks like it is working.
        #
        # The output is the authority. A fingerprint means success whatever the
        # exit code said; no fingerprint is a failure whatever it said.
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
        if not rate_limiter().acquire(timeout=settings.PROVIDER_TIMEOUT_SECONDS):
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

        # POST, not GET: a Chromaprint fingerprint is a couple of kilobytes of
        # base64 and would sit well past what some proxies allow in a URL.
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
            # The audit's A6 hang was an untimed urlopen; this one always ends,
            # and a Pi that has dropped its wifi simply fails the track.
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

    urllib, unlike `requests`, neither advertises nor decodes gzip on its own,
    so the `compress` meta flag would otherwise hand us a binary blob.
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
# Split out as plain functions over plain dicts: the response shape is the part
# most likely to drift, and this way it is testable against a captured payload
# with no network, no subprocess and no settings.


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

    recording = _pick_recording(best.get("recordings") or [], ctx.duration)
    if recording is None:
        # A fingerprint match with no MusicBrainz recording attached happens for
        # tracks nobody has linked yet. There is nothing to write, so pass.
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
            # Costs no request here — the tagger fetches it later, under its own
            # timeout, and tolerates the 404 that releases without art return.
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


def _pick_recording(recordings: list, duration: int) -> dict | None:
    """The recording whose length best matches the file.

    One fingerprint routinely matches a studio cut, a radio edit and three live
    versions. Duration is the only signal available to tell them apart, and
    picking the wrong one puts a 7-minute album version under the single's
    track number. When the duration is unknown every candidate ties and the
    tie-break — has releases, then the order AcoustID ranked them in — decides.
    """
    candidates = [
        r for r in recordings if isinstance(r, dict) and str(r.get("title") or "").strip()
    ]
    if not candidates:
        return None

    def rank(recording: dict) -> tuple[int, int]:
        recorded = int(_as_float(recording.get("duration")))
        if duration > 0 and recorded > 0:
            delta = abs(recorded - duration)
        else:
            delta = _NO_DURATION_MATCH
        return (delta, 0 if recording.get("releases") else 1)

    return min(candidates, key=rank)


def _pick_release(releases: list) -> dict | None:
    """The release that actually tells us where the track sits, else the oldest.

    A recording can appear on the original album, four compilations and a
    remaster. Preferring one that carries a medium/track position keeps the
    numbers we came here for; preferring the earliest year after that picks the
    original album over a later reissue.
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

    The response includes only the medium and track that matched, so the first
    entry carrying a position is the one we want. A single-disc release reports
    medium position 1, which `plex.py` correctly declines to prepend.
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

    MusicBrainz splits a credit into parts with join phrases, so "A feat. B"
    arrives as two artists; joining on the phrases reproduces the credit as
    written rather than inventing a comma.
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
    """Coerce whatever the API sent into a number. 0.0 for anything that is not.

    The API is well behaved, but this is the seam where a None or a string would
    otherwise raise inside a `max()` key and take out the whole result set.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
