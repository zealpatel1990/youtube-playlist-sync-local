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

from django.conf import settings

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


def identify(ctx: IdentifyContext) -> TrackMetadata | None:
    """Run the chain and return the first result that clears the bar, or None."""
    threshold = settings.IDENTIFY_MIN_CONFIDENCE

    for provider in get_chain():
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

        if result.confidence < threshold:
            log.info(
                "provider %s scored %.2f on %s, below the %.2f threshold; continuing",
                provider.name, result.confidence, ctx.path.name, threshold,
            )
            continue

        # A provider that forgot to stamp itself leaves `identified_by` blank.
        if not result.provider:
            result = replace(result, provider=provider.name)

        log.info(
            "identified %s as '%s - %s' via %s (%.2f)",
            ctx.path.name, result.artist, result.title, result.provider,
            result.confidence,
        )
        return result

    log.info("no provider could identify %s", ctx.path)
    return None
