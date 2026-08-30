"""Collect what the providers *could* have said, for a person to choose from.

Separate from `base.identify()` on purpose. Identification answers "what is
this file?" with one row and must stay predictable; this answers "what else
could it be?" and is only ever reached when someone asks. Nothing here runs on
the pipeline path, so a change to ranking here cannot alter how tracks are
identified automatically.

**Held in memory, not in a table.** These are working notes for one person over
a few seconds, true of nothing until a row is accepted, and the app already
runs as a single process (`core.runtime` takes a lock file to guarantee it) —
the same reason `core.events` and `core.locks` are in-process. A restart drops
them, which costs one button press to redo.

Gemini's guess is never shown: it reads the title rather than the audio, so on
its own it would pad the list with plausible names nothing verified. It is
consulted only as a *search term*, and only when nothing else answered — see
`SUGGEST_SEED_ONLY`.

Searching runs in two stages, sequential then concurrent, because the
catalogues need what the audio providers found. `collect` explains why.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent import futures

from .base import IdentifyContext, TrackMetadata, get_chain

log = logging.getLogger("music.identify")

#: Providers worth asking for alternatives, in the order their answers are
#: shown. AcoustID leads because it returns several, and because it carries a
#: track number.
#:
#: The two catalogue searches run **last, and deliberately so**: they take a
#: string rather than audio, so they are the only ones that can be handed what
#: the others already found and asked to look it up. See `_seed_queries`.
SUGGEST_PROVIDERS = ("acoustid", "shazam", "tags", "itunes", "deezer")

#: Providers suggestions may build for themselves when `IDENTIFY_CHAIN` does
#: not name them.
#:
#: Everything else here is shared with the chain on purpose — asking AcoustID
#: for alternatives reuses the fingerprint an identification already computed,
#: and running a provider the operator removed from the chain would be a
#: surprise. These two are the exception because they cost one keyless HTTP
#: GET and nothing else, which makes "let me see what Apple thinks" a
#: reasonable thing to offer a person looking at a track by hand, whether or
#: not it has been promoted to automatic use yet.
SUGGEST_STANDALONE = ("itunes", "deezer")

#: Consulted for a *query*, never shown as a candidate.
#:
#: Gemini reads the title rather than the audio, so its answer on its own is a
#: plausible name nothing verified — which is why it stayed out of this panel.
#: As a seed it is a different thing entirely: it proposes the spelling a
#: catalogue actually indexes, and the catalogue then either confirms it with a
#: real release whose running time matches the file or does not. The guess is
#: never displayed; only what iTunes and Deezer corroborate is.
#:
#: The case this exists for is transliteration. Apple returns nothing at all
#: for "daal bhaat saak rotli" — the store spells it "Dhaal Bhaat Shaak
#: Rotli" — and no amount of duration checking helps a query that matches zero
#: rows. Something has to propose the other spelling.
SUGGEST_SEED_ONLY = ("gemini",)

#: A ceiling per track. Generous on purpose: this list exists to show
#: everything the audio supports, and the pipeline's judgement is deliberately
#: not applied here — no confidence threshold, no cover/unrelated/artist
#: guards. Those keep automatic identification honest; a person choosing by
#: hand can see a cover and simply not pick it.
MAX_SUGGESTIONS = 24

#: Where the chooser samples Shazam. The chain stops at its first hit, which is
#: right when one answer is wanted — but a long upload can be several different
#: songs, and here each distinct answer is a candidate worth showing. Measured:
#: a 20-minute soundtrack suite named a different piece at 10% than at 90%.
#:
#: Two points, not the four this started with. Every one of these fires on
#: every search — unlike the chain's retries, which are reached only after a
#: miss — so each costs a decode, an upload and a rate-limited call whether or
#: not it is needed, and the panel's wait is mostly this. Four was worth it
#: when Shazam and AcoustID were the only things filling the list; now iTunes
#: and Deezer contribute candidates of their own, and the extra windows were
#: buying variety the catalogues already supply.
#:
#: 0.33 is kept because it is the single best window there is — see
#: `shazam.EXCERPT_START_FRACTION`, where a third of the way in beat every
#: alternative over 162 real files. 0.75 is the far end, which is what actually
#: catches a multi-song upload; the dropped 0.15 sampled intros and idents, the
#: worst place to look, and 0.50 sat close enough to 0.33 to usually repeat it.
SUGGEST_SHAZAM_FRACTIONS = (0.33, 0.75)

#: How many tracks' suggestions are kept at once, and for how long. Both exist
#: only to bound memory on a 1 GB box: nobody is choosing from a list they
#: asked for an hour ago.
MAX_TRACKS = 40
TTL_SECONDS = 3600.0

_lock = threading.Lock()
#: track_id -> (stored_at, [TrackMetadata, ...])
_store: dict[int, tuple[float, list[TrackMetadata]]] = {}


# --- the store ----------------------------------------------------------


def remember(track_id: int, candidates: list[TrackMetadata]) -> None:
    """Replace this track's candidates. A stale set is worse than none."""
    with _lock:
        _store[int(track_id)] = (time.monotonic(), list(candidates))
        _evict()


def recall(track_id: int) -> list[TrackMetadata]:
    """This track's candidates, or [] when there are none or they expired."""
    with _lock:
        entry = _store.get(int(track_id))
        if entry is None:
            return []
        stored_at, candidates = entry
        if time.monotonic() - stored_at > TTL_SECONDS:
            del _store[int(track_id)]
            return []
        return list(candidates)


def has_run(track_id: int) -> bool:
    """True when a search has completed for this track and is still held.

    "Nothing was found" and "nothing has been asked yet" look identical if only
    the list is consulted — and after a restart, which empties the store, the
    panel confidently reported "No candidates" for a track nobody had searched.
    """
    with _lock:
        entry = _store.get(int(track_id))
        if entry is None:
            return False
        if time.monotonic() - entry[0] > TTL_SECONDS:
            del _store[int(track_id)]
            return False
        return True


def forget(track_id: int) -> None:
    with _lock:
        _store.pop(int(track_id), None)


def _evict() -> None:
    """Drop expired entries, then the oldest, until the cap holds. Caller holds
    the lock."""
    now = time.monotonic()
    for key in [k for k, (at, _) in _store.items() if now - at > TTL_SECONDS]:
        del _store[key]
    while len(_store) > MAX_TRACKS:
        oldest = min(_store, key=lambda k: _store[k][0])
        del _store[oldest]


def reset_for_tests() -> None:
    with _lock:
        _store.clear()


# --- collecting ---------------------------------------------------------


def collect(ctx: IdentifyContext) -> list[TrackMetadata]:
    """Every candidate this file supports, best first, de-duplicated.

    Two stages, because the second depends on the first: the catalogues are
    *seeded* with whatever the audio providers recognised, which is what turns
    a slowed-down edit or an oddly transliterated title into a real release.

    Stage one stays sequential. Both providers in it decode audio — `fpcalc`
    for AcoustID, ffmpeg excerpts for Shazam — and running two decodes at once
    on the Pi is measured to make each far slower (see the timeout note in
    `acoustid`), so overlapping them buys nothing there. Stage two is pure
    network I/O against two unrelated hosts, which is worth overlapping:
    measured at 3.1s and 2.3s, so concurrently it costs the larger of the two
    rather than their sum.
    """
    available = {provider.name: provider for provider in get_chain()}
    found: list[TrackMetadata] = []

    # --- stage one: the audio, one at a time ---
    for name in SUGGEST_PROVIDERS:
        if name in SUGGEST_STANDALONE:
            continue
        provider = available.get(name)
        if provider is None:
            continue
        try:
            found.extend(_from(provider, name, ctx, found))
        except Exception:
            # One provider's bad day must not cost the others, as in the chain.
            log.exception("suggest: provider %s raised on %s", name, ctx.path)

    # --- what the catalogues will look up ---
    #
    # Computed once, from stage one only, and handed to both. Letting one
    # catalogue seed the other would make the result depend on which finished
    # first, and the same file would suggest different things on each search.
    seeds = _seed_queries(found) + _ask_for_seeds(available, ctx, found)

    # --- stage two: the catalogues, together ---
    found.extend(_from_catalogues(available, ctx, seeds))

    return _rank(found, ctx.duration)[:MAX_SUGGESTIONS]


def _from_catalogues(
    available: dict, ctx: IdentifyContext, seeds: list[str]
) -> list[TrackMetadata]:
    """Ask every catalogue at once; return their answers in a fixed order.

    Results are gathered by provider name rather than by completion, so the
    panel lists iTunes before Deezer however the network behaves — an order
    that changed between searches would reshuffle the buttons under whoever is
    reading.

    The pool is created and shut down inside this call. Nothing here outlives
    the search, which is the same rule the rest of the app follows: no
    long-lived threads, and nothing running while the box is idle.
    """
    providers = []
    for name in SUGGEST_PROVIDERS:
        if name not in SUGGEST_STANDALONE:
            continue
        provider = available.get(name) or _standalone(name)
        if provider is not None:
            providers.append((name, provider))

    if not providers:
        return []
    if len(providers) == 1:
        name, provider = providers[0]
        return _catalogue_answers(provider, name, ctx, seeds)

    results: dict[str, list[TrackMetadata]] = {}
    with futures.ThreadPoolExecutor(
        max_workers=len(providers), thread_name_prefix="suggest"
    ) as pool:
        submitted = {
            pool.submit(_catalogue_answers, provider, name, ctx, seeds): name
            for name, provider in providers
        }
        for future in futures.as_completed(submitted):
            name = submitted[future]
            # `_catalogue_answers` already swallows its own failures, so this
            # only fires if the pool itself could not run the call.
            try:
                results[name] = future.result()
            except Exception:
                log.exception("suggest: %s could not be run for %s", name, ctx.path)

    return [meta for name, _ in providers for meta in results.get(name, [])]


def _catalogue_answers(
    provider, name: str, ctx: IdentifyContext, seeds: list[str]
) -> list[TrackMetadata]:
    """One catalogue's rows. Runs in a pool thread, so it never raises."""
    try:
        return provider.candidates(ctx, seeds=seeds)
    except Exception:
        log.exception("suggest: provider %s raised on %s", name, ctx.path)
        return []


def _ask_for_seeds(available: dict, ctx: IdentifyContext, found: list) -> list[str]:
    """A query from `SUGGEST_SEED_ONLY`, but only when nothing else answered.

    Returns `[]` whenever the audio providers already found something — their
    answer is a better seed than a guess, and Gemini's daily budget is far too
    small to spend confirming what is already known.
    """
    if found:
        return []
    seeds: list[str] = []
    for name in SUGGEST_SEED_ONLY:
        provider = available.get(name)
        if provider is None:
            continue
        try:
            answer = provider.identify(ctx)
        except Exception:
            log.exception("suggest: seed provider %s raised on %s", name, ctx.path)
            continue
        if answer is None or not answer.is_usable():
            continue
        query = " ".join(p for p in (answer.title, answer.artist) if p).strip()
        if query:
            log.info("suggest: %s proposes %r as a search for %s",
                     name, query, ctx.path.name)
            seeds.append(query)
    return seeds


def _standalone(name: str):
    """Build a `SUGGEST_STANDALONE` provider the chain does not carry, or None.

    Unavailability is normal here — the provider may simply be switched off —
    so it is not logged at anything louder than debug. A failure to construct
    one must never take the rest of the panel down with it.
    """
    if name not in SUGGEST_STANDALONE:
        return None
    from .base import _provider_classes

    provider_class = _provider_classes().get(name)
    if provider_class is None:
        return None
    try:
        provider = provider_class()
        reason = provider.unavailable_reason()
    except Exception:
        log.exception("suggest: %s could not be built", name)
        return None
    if reason:
        log.debug("suggest: %s is skipped — %s", name, reason)
        return None
    return provider


def _seed_queries(found: list[TrackMetadata]) -> list[str]:
    """"Artist - Title" for each distinct answer so far, best first.

    What this is for: a file tagged "Tane Joyi Me Jyaarthi" that Shazam
    recognises as "Lagyo Prityu No Rang". Searching the file's own tag finds
    nothing in either catalogue; searching Shazam's answer finds the actual
    release — the Slowed + Reverb single at 382s against the file's 383s — and
    with it the album and year no fingerprint provider returned.
    """
    seeds: list[str] = []
    seen: set[str] = set()
    for meta in sorted(found, key=lambda m: -m.confidence):
        query = " ".join(part for part in (meta.title, meta.artist) if part).strip()
        key = query.lower()
        if query and key not in seen:
            seen.add(key)
            seeds.append(query)
    return seeds


def _from(
    provider, name: str, ctx: IdentifyContext, found: list[TrackMetadata]
) -> list[TrackMetadata]:
    """One stage-one provider's candidates. Only AcoustID can offer more than one.

    The catalogues do not come through here — they run together in
    `_from_catalogues`, against seeds this stage produced.
    """

    if name == "acoustid":
        from . import acoustid

        # Reuses the chain's own fingerprint and lookup, so a suggestion run
        # costs the one fpcalc and one HTTP call an identification would.
        if not provider._ensure_fingerprint(ctx):
            return []
        payload = provider._lookup(ctx.fingerprint, ctx.duration)
        if payload is None:
            return []
        return acoustid.parse_lookup_candidates(payload, ctx)

    if name == "shazam":
        # One Shazam call answers with exactly one track — its `matches` list
        # is time offsets for a single song id, not alternatives. So ask
        # several times, at different points in the file, and keep every
        # distinct answer. `_attempt` rather than `identify()` because the
        # latter stops at the first hit by design.
        from . import shazam as shazam_module

        found = []
        for start in _sample_points(ctx.duration, shazam_module.EXCERPT_SECONDS):
            answer = provider._attempt(ctx, start)
            if answer is not None:
                found.append(answer)
        return found

    # `tags` reports whatever the file already claims about itself.
    single = provider.identify(ctx)
    return [single] if single is not None else []


def _sample_points(duration: int, window: int) -> list[float]:
    """Distinct offsets to sample, never overlapping and never past the end.

    A track too short to hold two separate windows gets one point: sampling the
    same audio four times would spend four rate-limited calls on one answer.
    """
    if not duration or duration <= window * 2:
        return [0.0]
    points: list[float] = []
    for fraction in SUGGEST_SHAZAM_FRACTIONS:
        start = max(0.0, min(duration * fraction, duration - window))
        if all(abs(start - seen) >= window for seen in points):
            points.append(start)
    return points or [0.0]


def _rank(candidates: list[TrackMetadata], file_duration: int = 0) -> list[TrackMetadata]:
    """Best first, and never the same choice twice.

    A candidate carrying a track number sorts first because that is what the
    Plex filename needs; confidence breaks the tie. The person decides, so this
    is only an ordering — nothing is filtered out for looking wrong.

    Running time breaks what confidence cannot. A catalogue answers "Hookah
    Bar" with the album cut at 4:14 and the remix at 3:22, and both land in the
    same scoring band, so the remix led on insertion order alone and collected
    the highlighted button. Whichever length is closer to the file is the
    better guess, and it costs nothing to prefer it.
    """

    def rank(meta: TrackMetadata) -> tuple:
        if file_duration and meta.duration:
            closeness = abs(meta.duration - file_duration)
        else:
            # Unknown on either side must not sort as a perfect match, and must
            # not jump ahead of a candidate whose length actually agrees.
            closeness = 10**6
        return (0 if meta.track_no else 1, -round(meta.confidence, 2), closeness)

    seen: set[tuple] = set()
    unique: list[TrackMetadata] = []
    for meta in sorted(candidates, key=rank):
        # The provider is part of the identity. Two sources landing on the same
        # answer is *evidence*, not a duplicate, and collapsing them hid it:
        # on "Dilliwaali Girlfriend" Shazam independently confirmed the file's
        # own tags, and that row was dropped for matching them — leaving only
        # the tags and a poor 0.04 remix match on screen.
        #
        # Within one provider a repeat really is a repeat: the same Shazam
        # answer from three of four sample points is one candidate.
        key = (
            meta.provider,
            meta.artist.casefold(),
            meta.title.casefold(),
            meta.album.casefold(),
            meta.track_no,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(meta)
    return unique
