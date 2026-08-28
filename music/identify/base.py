"""
The identification contract and the chain runner.

Identification is the one part of the pipeline that talks to the outside world,
so the shape of this module is dictated by the two failure modes the audit found
there: unbounded network calls that wedge a worker (docs/CODE-AUDIT.md A6) and
optional dependencies that take the whole app down when they are missing or
broken (the module-level Gemini client, A1's sibling — see `gemini.py`).

Three rules follow from that, and every provider obeys them:

* **A provider never raises at import.** Optional packages are imported inside
  methods, so a missing `shazamio` disables Shazam and nothing else. The
  provider modules themselves are imported lazily here for the same reason.
* **A provider never blocks without a bound.** Every network call carries
  `settings.PROVIDER_TIMEOUT_SECONDS`, and every rate limiter is acquired with a
  timeout rather than waited on forever.
* **A provider never escapes.** `identify()` catches per-provider exceptions and
  moves to the next one, because a failure in the cheap tier must not cost the
  track its chance at the expensive tier.

Order is cost, not quality: tags → acoustid → shazam → gemini. The chain stops
at the first result clearing `settings.IDENTIFY_MIN_CONFIDENCE`, so the
rate-limited providers only ever see what the free ones could not resolve.
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
    """What a provider returns, and the only currency this package deals in.

    Frozen because a result is evidence: once a provider has spoken, nothing
    downstream should be able to edit its answer in place and leave the
    `provider`/`confidence` pair describing something else. Use `replace()` or
    `merged_with()` to derive a new value.

    Unknown numbers are 0, never None, matching `music.models.Track` — the
    comparisons in this package and in `plex.py` run against these values
    directly and a None reaching one of them raises at a distance (A14).
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
    #: 0.0–1.0. Compared against settings.IDENTIFY_MIN_CONFIDENCE by the chain.
    confidence: float = 0.0
    #: The provider name, so a track records how it came to be identified.
    provider: str = ""

    def is_usable(self) -> bool:
        """Enough to place and name the file.

        Mirrors `Track.has_core_metadata` deliberately: a result that cannot
        satisfy that property would be stored and then immediately fail to
        organize, which is worse than passing to the next provider.
        """
        return bool(self.title and (self.artist or self.album_artist))

    def merged_with(self, other: TrackMetadata) -> TrackMetadata:
        """Fill this result's gaps from `other`. Self wins on every set field.

        "Set" means truthy, which for this dataclass is exactly the same as
        "not the unknown value" — "" for text, 0 for numbers, False for the
        compilation flag. That equivalence is why unknown numbers are 0 rather
        than None, and it keeps this one rule correct for every field.
        """
        merged = {}
        for spec in dataclasses.fields(self):
            mine = getattr(self, spec.name)
            merged[spec.name] = mine if mine else getattr(other, spec.name)
        return TrackMetadata(**merged)


@dataclass
class IdentifyContext:
    """Everything a provider may look at, gathered once for the whole chain.

    Mutable on purpose, and in exactly two fields: `fingerprint` and `duration`
    are filled in by AcoustID when it runs `fpcalc`, so the caller can persist
    them onto the Track and a later attempt never pays for the same fingerprint
    twice. Fingerprinting is the single most expensive local operation in the
    pipeline on an ARMv7 core.
    """

    path: Path
    #: Seconds. 0 means UNKNOWN — never None (A14). AcoustID fills it in.
    duration: int = 0
    #: Chromaprint fingerprint; "" means not computed yet.
    fingerprint: str = ""
    #: What the file's own tags already say. Read once by the caller.
    existing: TrackMetadata = field(default_factory=TrackMetadata)
    #: e.g. the YouTube video title — the only thing Gemini has to work with.
    hint_title: str = ""
    hint_url: str = ""


class Provider(abc.ABC):
    """One metadata source.

    Subclasses override `unavailable_reason()` rather than `available()`, so the
    "why" exists as text for the startup log instead of being reconstructed from
    a bare False. `available()` stays the predicate callers use.
    """

    #: Must match the name used in settings.IDENTIFY_CHAIN.
    name: str = ""

    def unavailable_reason(self) -> str:
        """Why this provider cannot run right now, or "" when it can.

        Report only *durable* conditions here — a missing key, an uninstalled
        package, a disabled flag. A transient one (an exhausted daily budget,
        say) belongs in `identify()`, because the chain is built once and cached
        and would otherwise strand the provider until the next restart.
        """
        return ""

    def available(self) -> bool:
        """Config present and dependency importable."""
        return not self.unavailable_reason()

    @abc.abstractmethod
    def identify(self, ctx: IdentifyContext) -> TrackMetadata | None:
        """Return metadata, or None to pass to the next provider.

        Must not raise for an ordinary miss, a timeout, or a rate-limit refusal
        — those are all "None". The chain catches exceptions anyway, but a
        provider that signals a miss by raising makes the log unreadable.
        """


def _provider_classes() -> dict[str, type[Provider]]:
    """Import the provider modules, one failure at a time.

    Deliberately not module-level imports: a provider module that cannot be
    imported at all — a broken transitive dependency, a stale compiled wheel
    for the wrong ARM ABI — must disable that one provider, not this package,
    and not the tagging job handlers that import it.
    """
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

    Unavailable providers are dropped with a reason at INFO — that line is the
    operator's answer to "why did nothing get identified?", so it says what is
    missing rather than merely that something is.

    Settings validation already rejects unknown names at import
    (`music_manager.settings`), so an unknown name here means the two lists
    have drifted apart; that is a bug, and it is logged as one.
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
# `available()` costs a PATH scan and an importlib search. That is nothing
# beside fingerprinting a file, but rebuilding per track would also re-log every
# skip reason — thousands of identical lines onto the Pi's SD card during a
# library sweep. So the chain is built once and reused, keyed on the configured
# names so an override_settings in a test rebuilds without any extra ceremony.

_chain_lock = threading.Lock()
_chain_cache: tuple[tuple[str, ...], list[Provider]] | None = None


def get_chain() -> list[Provider]:
    """The chain `identify()` runs, built on first use and cached thereafter."""
    global _chain_cache
    key = tuple(settings.IDENTIFY_CHAIN)
    with _chain_lock:
        if _chain_cache is not None and _chain_cache[0] == key:
            return _chain_cache[1]
    # Built outside the lock: construction touches the filesystem, and holding a
    # lock across IO on a Pi is how you turn a slow disk into a stalled worker.
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
    """Run the chain and return the first result that clears the bar.

    Returns None when nothing did, which is a normal outcome — the caller marks
    the track failed with a retry, and a later pass may do better once the file
    has a fingerprint cached on it.
    """
    threshold = settings.IDENTIFY_MIN_CONFIDENCE

    for provider in get_chain():
        try:
            result = provider.identify(ctx)
        except Exception:
            # One provider's bad day must not cost the track the rest of the
            # chain. Logged with a traceback because a *repeatedly* raising
            # provider is a real bug and needs to be diagnosable from journald.
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

        # A provider that forgot to stamp itself would otherwise leave the
        # track's `identified_by` blank and unauditable.
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
