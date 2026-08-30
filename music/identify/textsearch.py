"""Keyless catalogue search — Apple Music (iTunes) and Deezer.

Unlike every other provider in the chain, these two **do not listen to the
audio**. They take a string — the file's own tags, its upload title, or an
answer an earlier provider already gave — and look it up in a public
catalogue. So they can never identify a file that carries no text at all;
what they do is turn a bare "title + artist" into a real release, with the
album, the year, and the track and disc number the Plex layout needs.

That last part is why they earn a place. `acoustid` was previously the only
provider supplying track *and* disc number; both of these do too, for a
catalogue (Indian film music especially) where AcoustID's coverage is thin.

Neither needs an API key, an account, or a signup — they are the public search
endpoints behind the two stores, stdlib `urllib` and JSON. Measured against
`_devdata/failed-samples`, iTunes answered 6 of 7 identifiable files and Deezer
5, on files where AcoustID managed 2.

**Duration is what makes an answer trustworthy.** A text search always returns
rows, so agreement with the query proves nothing — every row shares words with
it. What separates a real match from a plausible one is the running time
lining up with the file on disk, so that is what `_confidence` is built from
and why an answer for a file of unknown duration can never score highly.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace

from django.conf import settings

from music.core.ratelimit import RATE_LIMIT_WAIT_SECONDS, RateLimiter

from . import matching
from .base import IdentifyContext, Provider, TrackMetadata

log = logging.getLogger("music.identify")

#: Rows to ask each catalogue for. Enough to hold the original alongside the
#: remixes and lofi edits that crowd a popular title — measured on "Zara Zara",
#: where the wanted Jhankar Beats cut was the third row — without turning the
#: suggestion panel into a list of every edit ever released.
SEARCH_LIMIT = 5

#: A refusal to allocate an unbounded body into a shared 2GB of RAM, as in
#: `acoustid`. A 5-row search response is a few KB; anything at this size is a
#: redirect to something that is not JSON.
MAX_RESPONSE_BYTES = 1 * 1024 * 1024

#: How far a catalogue's running time may sit from the file's, and what an
#: answer at that distance is worth. Ordered tightest first; the first band
#: that fits wins.
#:
#: Calibrated on real matches rather than picked round: 195s/195s (Just A Boy),
#: 383s/382s (the slowed Lagyo Prityu No Rang edit) and 297s/295s (Zara Zara
#: Jhankar) were all correct, while the wrong answers on the same corpus were
#: out by a minute or more. The widest band sits below the default
#: IDENTIFY_MIN_CONFIDENCE of 0.5 on purpose: a title that matches with the
#: duration disagreeing is worth *offering* to a person and not worth filing a
#: track on by itself.
DURATION_BANDS: tuple[tuple[int, float], ...] = (
    (2, 0.90),
    (5, 0.82),
    (15, 0.66),
    (45, 0.52),
)

#: Everything past the last band, and anything where a duration is missing on
#: either side. Below the threshold by design — see above.
CONFIDENCE_UNVERIFIED = 0.40

#: A ceiling on how many already-found answers are re-searched as queries. The
#: flow this exists for: Shazam names "Lagyo Prityu No Rang" for a file tagged
#: "Tane Joyi Me Jyaarthi", and only searching Shazam's answer finds the actual
#: release (the Slowed + Reverb single, 382s against the file's 383s). Bounded
#: because each seed is another rate-limited call.
MAX_SEED_QUERIES = 3


# Module-level, as in `acoustid` and `shazam`: a bucket rebuilt with the chain
# would start full every time and limit nothing. One bucket per catalogue —
# they are unrelated services and must not throttle each other.

_limiters: dict[str, RateLimiter] = {}
_limiters_lock = threading.Lock()


def rate_limiter(name: str, rate_per_min: float) -> RateLimiter:
    with _limiters_lock:
        limiter = _limiters.get(name)
        if limiter is None:
            limiter = RateLimiter(rate_per_min / 60.0)
            _limiters[name] = limiter
        return limiter


def reset_for_tests() -> None:
    with _limiters_lock:
        _limiters.clear()


# --- query building -----------------------------------------------------


def _clean(text: str) -> str:
    """Collapse whitespace and drop the punctuation a search box chokes on."""
    return " ".join((text or "").replace("_", " ").split())


def queries_for(ctx: IdentifyContext) -> list[str]:
    """What to search for, best first, de-duplicated.

    Tags lead because "title artist" is already the shape a catalogue indexes.
    The upload title is a distant second — it carries the film, the label and
    the word "Official" as often as it carries the song — and the filename is
    the last resort for a file that claims nothing at all.
    """
    candidates: list[str] = []

    existing = ctx.existing
    if existing.title:
        artist = existing.artist or existing.album_artist
        candidates.append(_clean(f"{existing.title} {artist}"))

    hint = matching.usable_hint(ctx.hint_title)
    if hint:
        candidates.append(_clean(hint))

    candidates.append(_clean(ctx.path.stem))

    seen: set[str] = set()
    unique: list[str] = []
    for query in candidates:
        key = query.lower()
        if query and key not in seen:
            seen.add(key)
            unique.append(query)
    return unique


# --- scoring ------------------------------------------------------------


def _confidence(result_duration: int, file_duration: int) -> float:
    """What an answer is worth, given how far its running time sits from ours."""
    if not result_duration or not file_duration:
        return CONFIDENCE_UNVERIFIED
    delta = abs(int(result_duration) - int(file_duration))
    for limit, score in DURATION_BANDS:
        if delta <= limit:
            return score
    return CONFIDENCE_UNVERIFIED


#: How much of the query an answer must account for, counting its title and
#: artist together.
#:
#: One shared word — what `matching.is_unrelated` asks for — is the right test
#: *there*, where a fingerprint already vouched for the audio and the title is
#: only a sanity check. It is far too weak here, where nothing vouches for
#: anything and a catalogue answers every query with five rows. Measured on
#: `_devdata/failed-samples`, one shared word plus a coincidental duration
#: match promoted two wrong answers over the threshold:
#:
#:   "Diwali Mela Final Alex & Kiran"  ->  "Haqiqi (feat. ... Kiran Kamath)"
#:                                         by Justin-Uday Duo, at 0.82
#:   "Merry Christmas Daniel B. George" -> "Edvard Grieg's In The Hall Of
#:                                          The Mountain King", at 0.52
#:
#: Both share exactly one word with the query. Every correct answer on that
#: corpus covered 80–100% of it, and both wrong ones covered 50% or less, so
#: the boundary is wide and this is not fitted to the noise.
MIN_QUERY_COVERAGE = 0.6


def _is_relevant(meta: TrackMetadata, query: str) -> bool:
    """Whether this row accounts for enough of what was actually asked for.

    Counting title and artist together is what lets a title-only query still
    match on the title alone, while a query naming a performer needs the answer
    to credit them.
    """
    asked = matching.tokens(query)
    if not asked:
        # Nothing comparable in the query — a filename of digits, say. Judging
        # is impossible, so defer rather than invent a verdict: the duration
        # band is the only evidence, and an unverified row scores below the
        # threshold anyway.
        return True
    answered = matching.tokens(f"{meta.title} {meta.artist}")
    return len(asked & answered) / len(asked) >= MIN_QUERY_COVERAGE


# --- the providers ------------------------------------------------------


class _CatalogueProvider(Provider):
    """Shared shape: build a query, GET JSON, map rows to TrackMetadata."""

    #: Set by subclasses.
    rate_setting: str = ""
    enabled_setting: str = ""

    def unavailable_reason(self) -> str:
        if not getattr(settings, self.enabled_setting, False):
            return f"{self.enabled_setting} is off"
        return ""

    # -- subclass contract --

    def _url(self, query: str) -> str:
        raise NotImplementedError

    def _rows(self, payload) -> list:
        raise NotImplementedError

    def _to_metadata(self, row: dict) -> TrackMetadata | None:
        """This row as metadata, or None when it is unusable."""
        raise NotImplementedError

    # -- shared --

    def _get(self, url: str):
        """One bounded GET returning parsed JSON, or None. Never raises."""
        limiter = rate_limiter(
            self.name, getattr(settings, self.rate_setting)
        )
        if not limiter.acquire(timeout=RATE_LIMIT_WAIT_SECONDS):
            log.warning("%s: rate limiter is saturated; skipping", self.name)
            return None

        request = urllib.request.Request(
            url, headers={"User-Agent": "music-manager/2.0 (+local library tool)"}
        )
        try:
            with urllib.request.urlopen(
                request, timeout=settings.PROVIDER_TIMEOUT_SECONDS
            ) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            log.warning("%s: HTTP %s for %s", self.name, exc.code, url)
            return None
        except Exception as exc:
            # A public endpoint on someone else's schedule; a DNS failure or a
            # reset connection is a miss, never a raise. See the Provider
            # contract in `base`.
            log.warning("%s: request failed (%s)", self.name, exc)
            return None

        if len(body) > MAX_RESPONSE_BYTES:
            log.warning("%s: response exceeded %d bytes; discarded",
                        self.name, MAX_RESPONSE_BYTES)
            return None
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            log.warning("%s: response was not JSON", self.name)
            return None

    def search(self, query: str, file_duration: int) -> list[TrackMetadata]:
        """Every row this catalogue offers for one query, best first."""
        payload = self._get(self._url(query))
        if payload is None:
            return []

        found: list[TrackMetadata] = []
        for row in self._rows(payload)[:SEARCH_LIMIT]:
            try:
                meta = self._to_metadata(row)
            except Exception:
                log.debug("%s: unmappable row", self.name, exc_info=True)
                continue
            if meta is None or not meta.is_usable() or not _is_relevant(meta, query):
                continue
            found.append(
                replace(
                    meta,
                    confidence=_confidence(meta.duration, file_duration),
                    provider=self.name,
                )
            )
        return found

    def candidates(
        self, ctx: IdentifyContext, seeds: list[str] | None = None
    ) -> list[TrackMetadata]:
        """Answers for this file, best first, across every query worth trying.

        `seeds` are answers other providers already gave, searched *before* the
        file's own text: a provider that recognised the audio is a better
        starting point than a tag nobody verified.
        """
        found: list[TrackMetadata] = []
        queries = (seeds or [])[:MAX_SEED_QUERIES] + queries_for(ctx)

        seen_queries: set[str] = set()
        for query in queries:
            key = query.lower()
            if not query or key in seen_queries:
                continue
            seen_queries.add(key)
            found.extend(self.search(query, ctx.duration))

        return sorted(found, key=lambda m: -m.confidence)

    def identify(self, ctx: IdentifyContext) -> TrackMetadata | None:
        """The single best answer, or None. The chain's guards judge it after."""
        found = self.candidates(ctx)
        return found[0] if found else None


#: The artwork size to ask each catalogue for, matching what the other
#: providers already put in the suggestion panel — AcoustID requests
#: Cover Art Archive's `front-500` and Shazam returns `coverarthq`. Bigger
#: would only be resampled down by the 44px thumbnail the panel draws.
ARTWORK_SIZE = 500

#: Apple returns a 100px thumbnail URL with the size baked into the filename
#: ("…/100x100bb.jpg"). Every other size is served from the same path, so
#: rewriting the segment is the documented way to ask for one.
_ITUNES_THUMB = "100x100"


def _meta(
    *, title, artist, album, album_artist, track_no, disc_no, year, duration,
    cover_url="", genre="",
) -> TrackMetadata | None:
    """One catalogue row, or None when it carries no title to stand on."""
    if not title:
        return None
    return TrackMetadata(
        title=_clean(title),
        artist=_clean(artist),
        album=_clean(album),
        album_artist=_clean(album_artist),
        track_no=_int(track_no),
        disc_no=_int(disc_no),
        year=_int(year),
        genre=_clean(genre),
        cover_url=(cover_url or "").strip(),
        duration=_int(duration),
    )


def _int(value) -> int:
    """A catalogue field as an int. Unknown is 0, never None — as everywhere."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _itunes_artwork(row: dict) -> str:
    """The row's cover at `ARTWORK_SIZE`, or "" when it carries none.

    Falls back to whatever URL was given if the size segment is not where it is
    expected — a smaller cover is worth more than no cover.
    """
    url = str(row.get("artworkUrl100") or row.get("artworkUrl60") or "").strip()
    if not url:
        return ""
    return url.replace(_ITUNES_THUMB, f"{ARTWORK_SIZE}x{ARTWORK_SIZE}")


class ItunesProvider(_CatalogueProvider):
    """Apple's public iTunes Search API. No key, no account.

    Documented at
    https://performance-partners.apple.com/search-api — the `search` endpoint
    is the same one the Store's own search box uses.
    """

    name = "itunes"
    enabled_setting = "ITUNES_ENABLED"
    rate_setting = "ITUNES_RATE_PER_MIN"

    def _url(self, query: str) -> str:
        return "https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {
                "term": query,
                "media": "music",
                "entity": "song",
                "limit": SEARCH_LIMIT,
                "country": settings.ITUNES_COUNTRY,
            }
        )

    def _rows(self, payload) -> list:
        return payload.get("results") or []

    def _to_metadata(self, row: dict) -> TrackMetadata | None:
        return _meta(
            title=row.get("trackName", ""),
            artist=row.get("artistName", ""),
            album=row.get("collectionName", ""),
            # `collectionArtistName` is only present on compilations, which is
            # exactly when the album artist differs from the track's.
            album_artist=row.get("collectionArtistName", "")
            or row.get("artistName", ""),
            track_no=row.get("trackNumber", 0),
            disc_no=row.get("discNumber", 0),
            year=(row.get("releaseDate") or "")[:4],
            duration=_int(row.get("trackTimeMillis")) // 1000,
            cover_url=_itunes_artwork(row),
            genre=row.get("primaryGenreName", ""),
        )


class DeezerProvider(_CatalogueProvider):
    """Deezer's public search API. No key, no account.

    Documented at https://developers.deezer.com/api/search. Search rows carry
    only a nested artist and album stub — no track number — so a hit is worth
    less to the Plex layout than an iTunes one, which is why it runs second.
    """

    name = "deezer"
    enabled_setting = "DEEZER_ENABLED"
    rate_setting = "DEEZER_RATE_PER_MIN"

    def _url(self, query: str) -> str:
        return "https://api.deezer.com/search?" + urllib.parse.urlencode(
            {"q": query, "limit": SEARCH_LIMIT}
        )

    def _rows(self, payload) -> list:
        return payload.get("data") or []

    def _to_metadata(self, row: dict) -> TrackMetadata | None:
        artist = (row.get("artist") or {}).get("name", "")
        album = row.get("album") or {}
        return _meta(
            title=row.get("title", ""),
            artist=artist,
            album=album.get("title", ""),
            album_artist=artist,
            # Absent from a search row; a Plex filename falls back to 0, which
            # is what an unknown number is everywhere in this codebase.
            track_no=0,
            disc_no=0,
            year=(row.get("release_date") or "")[:4],
            duration=row.get("duration", 0),
            # Deezer serves fixed sizes rather than a resizable path, so this
            # picks the published one nearest ARTWORK_SIZE (cover_big is 500).
            cover_url=album.get("cover_big") or album.get("cover_medium") or "",
        )


# --- enrichment ---------------------------------------------------------
#
# The chain answers "what is this?"; this answers "and what else is known
# about it?". A fingerprint provider recognises the recording but returns a
# bare title and artist — Shazam gives no track number at all, which is the one
# field the Plex filename is built from — while a catalogue holds the album,
# the year, the track and disc number and the cover art, and can be found from
# the name the fingerprint just supplied.
#
# This is the same trick `suggest` plays with seeds, applied to the single
# answer the pipeline settled on.


#: What an enriching row must score before its fields are trusted.
#:
#: Higher than IDENTIFY_MIN_CONFIDENCE on purpose. Being *offered* a loosely
#: matched release to look at costs nothing; silently merging one's album and
#: cover art into an answer that was already correct writes a wrong album to
#: disk. Only the two tightest duration bands clear this.
ENRICH_MIN_CONFIDENCE = 0.80

#: Providers whose answers are already catalogue rows. Enriching one of these
#: from the other would only trade one store's spelling for another's.
CATALOGUE_PROVIDERS = frozenset({"itunes", "deezer"})

#: What enrichment is allowed to fill in. Title and artist are deliberately
#: absent: the chain already decided those, and a catalogue's differently
#: transliterated spelling of the same song is not an improvement worth
#: overwriting a verified answer with.
ENRICHABLE_FIELDS = (
    "album", "album_artist", "track_no", "disc_no", "year", "genre", "cover_url",
)


def enrich(result: TrackMetadata, ctx: IdentifyContext) -> TrackMetadata:
    """`result` with its blanks filled from a catalogue, or unchanged.

    Never lowers confidence, never replaces a field the chain already set, and
    never raises — an enrichment failure must leave a good identification
    exactly as it was.
    """
    if not settings.IDENTIFY_ENRICH or not result.is_usable():
        return result
    if result.provider in CATALOGUE_PROVIDERS:
        return result
    if not _missing(result):
        return result

    query = " ".join(part for part in (result.title, result.artist) if part).strip()
    if not query:
        return result

    for provider_class in (ItunesProvider, DeezerProvider):
        try:
            provider = provider_class()
            if not provider.available():
                continue
            rows = provider.search(query, ctx.duration)
        except Exception:
            log.exception("enrich: %s raised for %s", provider_class.__name__, ctx.path)
            continue

        for row in rows:
            if row.confidence < ENRICH_MIN_CONFIDENCE:
                continue
            filled = _fill(result, row)
            if filled is result:
                continue
            log.info(
                "enriched '%s - %s' from %s (%.2f): %s",
                result.artist, result.title, provider.name, row.confidence,
                ", ".join(_missing(result) & set(ENRICHABLE_FIELDS)) or "nothing",
            )
            return filled

    return result


def _missing(result: TrackMetadata) -> set[str]:
    """Which enrichable fields this answer has nothing for."""
    return {name for name in ENRICHABLE_FIELDS if not getattr(result, name)}


def _fill(result: TrackMetadata, row: TrackMetadata) -> TrackMetadata:
    """`result` with only its empty enrichable fields taken from `row`.

    Not `merged_with`, which would copy `confidence` and `provider` too: the
    answer must keep the score and the name of whoever actually identified it,
    or the dashboard credits the wrong provider.
    """
    updates = {
        name: getattr(row, name)
        for name in ENRICHABLE_FIELDS
        if not getattr(result, name) and getattr(row, name)
    }
    return replace(result, **updates) if updates else result
