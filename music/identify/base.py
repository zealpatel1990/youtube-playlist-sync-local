"""The identification contract and the chain runner.

Provider rules: never raise at import (optional packages are imported inside
methods), never block without a bound, never escape — `identify()` catches and
moves on. Chain order is cost, not quality: tags -> acoustid -> shazam -> gemini.
"""

from __future__ import annotations

import abc
import dataclasses
import logging
import threading
from dataclasses import dataclass, field, replace
from importlib import import_module
from pathlib import Path

from typing import Callable

from django.conf import settings

from . import matching

log = logging.getLogger("music.identify")


@dataclass(frozen=True)
class TrackMetadata:
    """What a provider returns. Frozen; derive with `replace()`/`merged_with()`.

    Unknown numbers are 0, never None — see `music.models.Track`.
    """

    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track_no: int = 0
    disc_no: int = 0
    year: int = 0
    genre: str = ""
    is_compilation: bool = False
    musicbrainz_recording_id: str = ""
    musicbrainz_release_id: str = ""
    cover_url: str = ""
    #: The running time of the recording the *provider* is describing, in
    #: seconds. Informational only, and deliberately absent from
    #: `_IDENTIFIED_FIELDS`: `Track.duration` is measured from the file on disk
    #: and must never be replaced by a catalogue's idea of how long the song
    #: is. What this is for is judging an answer — comparing it against the
    #: file's own length is how a remix or a radio edit gives itself away, and
    #: showing both is what lets a person see why a row ranked where it did.
    #: 0 when the provider does not say, as with every other unknown number.
    duration: int = 0
    #: 0.0–1.0, compared against settings.IDENTIFY_MIN_CONFIDENCE.
    confidence: float = 0.0
    provider: str = ""

    def is_usable(self) -> bool:
        """Enough to place and name the file. Mirrors `Track.has_core_metadata`."""
        return bool(self.title and (self.artist or self.album_artist))

    def merged_with(self, other: TrackMetadata) -> TrackMetadata:
        """Fill this result's gaps from `other`. Self wins on every set field."""
        merged = {}
        for spec in dataclasses.fields(self):
            mine = getattr(self, spec.name)
            merged[spec.name] = mine if mine else getattr(other, spec.name)
        return TrackMetadata(**merged)


@dataclass
class IdentifyContext:
    """Everything a provider may look at, gathered once for the whole chain.

    AcoustID writes `fingerprint` and `duration` back, so the caller can
    persist them and a later attempt never re-runs fpcalc.
    """

    path: Path
    #: Seconds; 0 is unknown, never None.
    duration: int = 0
    fingerprint: str = ""
    #: What the file's own tags already say.
    existing: TrackMetadata = field(default_factory=TrackMetadata)
    #: e.g. the YouTube video title — the only thing Gemini has to work with.
    hint_title: str = ""
    hint_url: str = ""


class Provider(abc.ABC):
    """One metadata source. Subclasses override `unavailable_reason()`."""

    #: Must match the name used in settings.IDENTIFY_CHAIN.
    name: str = ""

    def unavailable_reason(self) -> str:
        """Why this provider cannot run right now, or "" when it can.

        Only *durable* conditions: the chain is built once and cached, so a
        transient one (an exhausted budget) belongs in `identify()` instead.
        """
        return ""

    def available(self) -> bool:
        """Config present and dependency importable."""
        return not self.unavailable_reason()

    @abc.abstractmethod
    def identify(self, ctx: IdentifyContext) -> TrackMetadata | None:
        """Return metadata, or None to pass to the next provider.

        A miss, a timeout and a rate-limit refusal are all None, not raises.
        """


def _provider_classes() -> dict[str, type[Provider]]:
    """Import the provider modules, one failure at a time — an unimportable one
    must disable that provider only, not this package."""
    classes: dict[str, type[Provider]] = {}
    for name, module_path, attribute in (
        ("tags", "music.identify.tags", "TagsProvider"),
        ("acoustid", "music.identify.acoustid", "AcoustidProvider"),
        ("shazam", "music.identify.shazam", "ShazamProvider"),
        ("itunes", "music.identify.textsearch", "ItunesProvider"),
        ("deezer", "music.identify.textsearch", "DeezerProvider"),
        ("gemini", "music.identify.gemini", "GeminiProvider"),
    ):
        try:
            classes[name] = getattr(import_module(module_path), attribute)
        except Exception:
            log.exception("provider %s could not be loaded and is disabled", name)
    return classes


def build_chain() -> list[Provider]:
    """Instantiate the providers named by settings.IDENTIFY_CHAIN, in order.

    Unavailable ones are dropped with a reason at INFO — the operator's answer
    to "why did nothing get identified?".
    """
    classes = _provider_classes()
    chain: list[Provider] = []

    for name in settings.IDENTIFY_CHAIN:
        provider_class = classes.get(name)
        if provider_class is None:
            log.error("identify chain names %r, which is not a known provider", name)
            continue
        try:
            provider = provider_class()
            reason = provider.unavailable_reason()
        except Exception:
            log.exception("provider %s failed to initialise and is disabled", name)
            continue
        if reason:
            log.info("identify: %s is skipped — %s", name, reason)
            continue
        chain.append(provider)

    if not chain:
        log.warning(
            "identify: no providers are usable; every track will stay unidentified"
        )
    else:
        log.info("identify chain: %s", " -> ".join(p.name for p in chain))
    return chain


# --- chain cache --------------------------------------------------------
#
# Built once and reused: rebuilding per track would re-log every skip reason,
# thousands of identical lines onto the SD card during a sweep. Keyed on the
# configured names so an override_settings in a test rebuilds by itself.

_chain_lock = threading.Lock()
_chain_cache: tuple[tuple[str, ...], list[Provider]] | None = None


def get_chain() -> list[Provider]:
    """The chain `identify()` runs, built on first use and cached thereafter."""
    global _chain_cache
    key = tuple(settings.IDENTIFY_CHAIN)
    with _chain_lock:
        if _chain_cache is not None and _chain_cache[0] == key:
            return _chain_cache[1]
    # Built outside the lock: construction touches the filesystem.
    chain = build_chain()
    with _chain_lock:
        _chain_cache = (key, chain)
        return chain


def reset_chain() -> None:
    """Drop the cached chain. For tests, and for any future settings reload."""
    global _chain_cache
    with _chain_lock:
        _chain_cache = None


#: Demoted answers, best tier first, with the reason logged when one is kept.
#:
#: `artist_mismatch` leads because such an answer still shares wording with the
#: title — only the performer is in doubt. `derivative` trails because a cover
#: is the one case where a wrong answer actively mis-files the track, so it is
#: preferred only over giving up entirely.
FALLBACK_TIERS: tuple[tuple[str, str], ...] = (
    ("artist_mismatch", "the title naming a different artist"),
    ("unrelated", "sharing no word with the title"),
    ("derivative", "looking like a cover or karaoke of it"),
)


def available_names() -> list[str]:
    """The providers that can actually run right now, in chain order.

    What the UI offers when someone asks for one provider by name — a menu
    listing a provider whose key is missing would only produce a job that
    reports "not available".
    """
    return [provider.name for provider in get_chain()]


def identify(
    ctx: IdentifyContext,
    *,
    on_provider: Callable[[str], None] | None = None,
    only: str = "",
) -> TrackMetadata | None:
    """Run the chain and return the first result that clears the bar, or None.

    `on_provider` is called with each provider's name before it runs, so a
    caller can report which tier a slow identification is currently in.

    `only` restricts the run to the single named provider — what the UI sends
    when someone picks one by hand. The guards below still run, but with one
    provider there is no "rest of the chain" to defer to, so a demoted answer
    falls through to `FALLBACK_TIERS` and is returned with its warning. That is
    the point: asking for Shazam should give you Shazam's answer, flagged, not
    silence.
    """
    threshold = settings.IDENTIFY_MIN_CONFIDENCE

    chain = get_chain()
    if only:
        chain = [provider for provider in chain if provider.name == only]
        if not chain:
            log.warning(
                "identify: %r was requested for %s but is not an available "
                "provider (available: %s)",
                only, ctx.path.name, ", ".join(available_names()) or "none",
            )
            return None

    #: Answers a guard objected to, kept by tier instead of thrown away.
    #:
    #: A guard **demotes, it does not veto**. Preferring an answer that agrees
    #: with the upload title is right; leaving a track unidentified when every
    #: provider agreed with each other is not. Observed on a real library: a
    #: file whose YouTube title had decayed to "[Deleted video]" was answered
    #: "DrINsaNE - JUST A BOY" by AcoustID, Shazam, Gemini *and* its own tags,
    #: and all four were discarded for sharing no word with the placeholder.
    #:
    #: Ordered best-first. `derivative` is last because returning a cover
    #: actively mis-files a track, so it is the true last resort.
    fallbacks: dict[str, TrackMetadata] = {}

    for provider in chain:
        if on_provider is not None:
            try:
                on_provider(provider.name)
            except Exception:
                log.debug("on_provider callback failed", exc_info=True)
        try:
            result = provider.identify(ctx)
        except Exception:
            # One provider's bad day must not cost the track the rest of the
            # chain. Tracebacks kept: a repeatedly raising provider is a bug.
            log.exception("provider %s raised on %s", provider.name, ctx.path)
            continue

        if result is None:
            continue

        if not result.is_usable():
            log.debug(
                "provider %s returned an unusable result for %s", provider.name, ctx.path
            )
            continue

        # The threshold gates first, so nothing below it can ever be kept as a
        # fallback, and the provider is stamped before any demotion so a kept
        # answer always records who gave it.
        if result.confidence < threshold:
            log.info(
                "provider %s scored %.2f on %s, below the %.2f threshold; continuing",
                provider.name, result.confidence, ctx.path.name, threshold,
            )
            continue

        # A provider that forgot to stamp itself leaves `identified_by` blank.
        if not result.provider:
            result = replace(result, provider=provider.name)

        # Guards run worst-first, so an answer tripping several lands in the
        # most pessimistic tier. Each one warns and demotes; none discards.
        if matching.looks_like_a_different_recording(
            result.title, result.artist, ctx.hint_title
        ):
            # A cover, karaoke or instrumental of what was asked for. The
            # fingerprint of a faithful cover is close enough that providers
            # return one confidently — AcoustID filed a Billie Eilish download
            # under Poté at 0.97. Passing lets a later provider answer, and one
            # usually does.
            if "derivative" not in fallbacks:
                fallbacks["derivative"] = result
                log.warning(
                    "provider %s answered '%s - %s' for %s, which looks like a "
                    "different recording of it; asking the rest of the chain",
                    provider.name, result.artist, result.title, ctx.path.name,
                )
            continue

        if matching.is_unrelated(result.title, result.artist, ctx.hint_title):
            if "unrelated" not in fallbacks:
                fallbacks["unrelated"] = result
                log.warning(
                    "provider %s answered '%s - %s' for %s, which shares nothing "
                    "with its title; asking the rest of the chain",
                    provider.name, result.artist, result.title, ctx.path.name,
                )
            continue

        if matching.contradicts_hint_artist(result.artist, ctx.hint_title):
            # Everything above passed and the answer is still probably wrong:
            # the upload title names one artist and this credits another.
            # Confidence cannot settle it — AcoustID returned "Sons of Serendip"
            # for a Billie Eilish download at 0.98, because MusicBrainz has only
            # covers linked to that fingerprint, so no score or sampling change
            # reaches the right answer. Ask the rest of the chain and prefer
            # whoever agrees with the title; keep this as the fallback for when
            # nobody does, which is the common case for uploads whose title
            # simply omits the performer.
            if "artist_mismatch" not in fallbacks:
                fallbacks["artist_mismatch"] = result
                log.warning(
                    "provider %s answered '%s - %s' for %s, but the title names "
                    "'%s'; asking the rest of the chain",
                    provider.name, result.artist, result.title, ctx.path.name,
                    matching.hint_artist(ctx.hint_title),
                )
            continue

        log.info(
            "identified %s as '%s - %s' via %s (%.2f)",
            ctx.path.name, result.artist, result.title, result.provider,
            result.confidence,
        )
        return _enriched(result, ctx)

    for tier, why in FALLBACK_TIERS:
        kept = fallbacks.get(tier)
        if kept is None:
            continue
        # Nobody corroborated the title. An answer every provider agreed on is
        # still better than none — and `AUTO_ORGANIZE` is off by default, so a
        # questionable one is reviewed before it moves a file.
        log.warning(
            "identified %s as '%s - %s' via %s (%.2f) — kept despite %s; review it",
            ctx.path.name, kept.artist, kept.title, kept.provider,
            kept.confidence, why,
        )
        return _enriched(kept, ctx)

    log.info("no provider could identify %s", ctx.path)
    return None


def _enriched(result: TrackMetadata, ctx: IdentifyContext) -> TrackMetadata:
    """Fill the answer's blanks from a catalogue. Never fails the caller.

    Imported here rather than at module scope because `textsearch` imports this
    module; the pattern is the same one the providers use for their optional
    dependencies. A failure returns the unenriched answer — an identification
    that succeeded must never be lost to a cosmetic follow-up call.
    """
    try:
        from . import textsearch

        return textsearch.enrich(result, ctx)
    except Exception:
        log.exception("enrichment failed for %s; keeping the answer as given", ctx.path)
        return result
