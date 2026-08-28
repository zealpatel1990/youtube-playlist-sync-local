"""
Tier 4 — Gemini. Last resort, and the most tightly constrained thing in the app.

This is inference, not recognition. Nothing here has listened to the file: the
model is shown a title string and asked what song it probably names. It is in
the chain because a YouTube rip titled "Fleetwood Mac - Dreams (HQ Audio)" is
trivially parseable by a language model and completely opaque to a fingerprint
that has no MusicBrainz entry to match. Its 0.55 confidence says exactly that —
above the 0.5 threshold so the answer is used, below every other provider so it
is never preferred over one that actually identified the audio.

The user's key is free-tier with a very low daily quota, which shapes three
decisions the previous version got wrong:

* **A hard `DailyBudget`, not just a rate limiter.** Pacing does not stop you
  making five thousand calls over a day. The old code called Gemini once per
  unidentified track with neither, so a library sweep burned the day's quota in
  minutes and then failed every remaining track (music/core/ratelimit.py).
* **Text only.** The old prompt attached `file_data(file_uri=video.url,
  mime_type="video/*")` and asked the model to watch the entire video — vastly
  more quota per call, for a question a title answers. Its duration-based
  branch to a text-only prompt is also where `None < 500` raised
  (docs/CODE-AUDIT.md A14).
* **The client is built inside the method.** `metadata_parser_service.py` built
  `genai.Client(api_key=...)` at module scope, so an unset key raised during
  import and took down the entire tagging chain — including the providers that
  need no key at all. Nothing in this package may fail at import.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import threading
from datetime import date
from pathlib import Path

from django.conf import settings

from music.core.ratelimit import DailyBudget, RateLimiter

from .base import IdentifyContext, Provider, TrackMetadata

log = logging.getLogger("music.identify")

#: Deliberately just above the 0.5 default threshold and below every other
#: provider: good enough to file the track, never good enough to outrank audio.
CONFIDENCE = 0.55

#: Placeholder answers a model reaches for when it does not know. Treated as
#: empty, so they cannot pass `is_usable()` and become a real-looking tag.
_PLACEHOLDERS = {
    "", "unknown", "unknown artist", "unknown title", "unknown album",
    "n/a", "na", "none", "null", "various", "untitled",
}

#: Strips the ```json ... ``` fence models add even when asked for raw JSON.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

#: Whitespace or a title separator. Used to decide whether a *filename* reads
#: like a description — see `_describe`.
_SEPARATOR = re.compile(r"[\s\-–—_|]")

PROMPT = """\
You are a music metadata expert. Identify the recording described below.

{context}

Reply with a single JSON object and nothing else, using exactly these keys:
  "title"          the track title alone, without the artist or any
                   "(Official Video)" / "(HQ Audio)" style decoration
  "artist"         the performing artist
  "album"          the album it appeared on, or "" if you are not confident
  "year"           the release year as a 4-digit number, or 0 if unsure
  "is_compilation" true only if that album is a various-artists compilation

Use "" or 0 for anything you are not reasonably sure of. Do not guess an album
in order to fill the field, and do not invent a title you cannot support.\
"""


# --- shared limiter and budget ------------------------------------------
#
# Module-level, and this matters more here than anywhere else: a budget rebuilt
# alongside the provider would reset to full on every call and enforce nothing
# at all, which is precisely the failure it exists to prevent.

_limiter: RateLimiter | None = None
_budget: DailyBudget | None = None
_client = None
_client_key = ""
#: The day we last logged exhaustion, so the message appears once and not once
#: per track for the remainder of a sweep.
_notice_day: date | None = None

_state_lock = threading.Lock()


def rate_limiter() -> RateLimiter:
    global _limiter
    with _state_lock:
        if _limiter is None:
            _limiter = RateLimiter(settings.GEMINI_RATE_PER_MIN / 60.0)
        return _limiter


def daily_budget() -> DailyBudget:
    global _budget
    with _state_lock:
        if _budget is None:
            _budget = DailyBudget(settings.GEMINI_DAILY_BUDGET)
        return _budget


def reset_for_tests() -> None:
    global _limiter, _budget, _client, _client_key, _notice_day
    with _state_lock:
        _limiter = None
        _budget = None
        _client = None
        _client_key = ""
        _notice_day = None


def _note_exhausted() -> None:
    """Log that the quota is gone — once per day, not once per track."""
    global _notice_day
    today = date.today()
    with _state_lock:
        if _notice_day == today:
            return
        _notice_day = today
    log.warning(
        "gemini: the daily budget of %s call(s) is spent; tracks that reach this "
        "provider will stay unidentified until tomorrow. Raise GEMINI_DAILY_BUDGET "
        "only if your key's quota actually allows it.",
        settings.GEMINI_DAILY_BUDGET,
    )


class GeminiProvider(Provider):
    """Infers metadata from the track's title. Never sees the audio."""

    name = "gemini"

    def unavailable_reason(self) -> str:
        if not settings.GEMINI_API_KEY:
            return "GEMINI_API_KEY is not set"
        if settings.GEMINI_DAILY_BUDGET <= 0:
            return "GEMINI_DAILY_BUDGET is 0, which disables this provider"
        # find_spec does not execute the package, so a chain built at startup
        # pays none of google-genai's import cost. It raises rather than
        # returning None when the parent `google` namespace is itself absent.
        try:
            if importlib.util.find_spec("google.genai") is None:
                return "the google-genai package is not installed"
        except (ImportError, ValueError):
            return "the google-genai package is not installed"
        return ""

    def identify(self, ctx: IdentifyContext) -> TrackMetadata | None:
        context = _describe(ctx)
        if not context:
            # Nothing to infer *from*. Spending a scarce call to ask the model
            # about an empty string is the one thing worse than not asking.
            log.debug("gemini: %s carries no title or tags to work from", ctx.path)
            return None

        # Checked before the limiter and consumed after it, on purpose. The
        # cheap read short-circuits the common steady state — quota gone, three
        # thousand tracks still to sweep — without parking each one on the
        # limiter for a token it will not use. The consume() is the real
        # decision, taken under the budget's own lock.
        if daily_budget().remaining <= 0:
            _note_exhausted()
            return None

        if not rate_limiter().acquire(timeout=settings.PROVIDER_TIMEOUT_SECONDS):
            log.warning("gemini: rate limiter is saturated; skipping %s", ctx.path.name)
            return None

        if not daily_budget().consume():
            _note_exhausted()
            return None

        try:
            raw = self._generate(PROMPT.format(context=context))
        except Exception as exc:
            log.warning("gemini: request failed for %s: %s", ctx.path.name, exc)
            return None

        data = parse_response(raw)
        if data is None:
            log.warning("gemini: response for %s was not usable JSON", ctx.path.name)
            return None

        return _metadata_from(data)

    def _generate(self, prompt: str) -> str:
        """The one place the SDK is touched. Returns the raw response text."""
        from google.genai import types

        client = _client_for(settings.GEMINI_API_KEY)
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                # HttpOptions.timeout is in MILLISECONDS, unlike every other
                # timeout in this codebase. Passing seconds here would set a
                # 30ms deadline and fail every call.
                http_options=types.HttpOptions(
                    timeout=int(settings.PROVIDER_TIMEOUT_SECONDS * 1000)
                ),
            ),
        )
        return response.text or ""


def _client_for(api_key: str):
    """Build the SDK client on first use and keep it.

    Lazy because a module-level client is what broke the previous version's
    import chain when the key was missing; cached because constructing one per
    track sets up a fresh HTTP stack for a call that is rate limited to one
    every six seconds anyway.
    """
    global _client, _client_key
    with _state_lock:
        if _client is None or _client_key != api_key:
            from google import genai

            _client = genai.Client(api_key=api_key)
            _client_key = api_key
        return _client


def _describe(ctx: IdentifyContext) -> str:
    """The evidence block handed to the model, or "" when there is none.

    Everything here is text the app already has. No audio, no video, no file
    upload — see the module docstring.
    """
    lines = []
    hint = _hint(ctx)
    if hint:
        lines.append(f'Title as published: "{hint}"')
    if ctx.existing.artist:
        lines.append(f'Existing artist tag: "{ctx.existing.artist}"')
    if ctx.existing.album:
        lines.append(f'Existing album tag: "{ctx.existing.album}"')
    if ctx.duration > 0:
        lines.append(f"Duration: {ctx.duration} seconds")
    if ctx.hint_url:
        lines.append(f"Source URL: {ctx.hint_url}")

    # A duration and a URL alone say nothing about which song this is.
    if not hint and not ctx.existing.artist:
        return ""
    return "\n".join(lines)


def _hint(ctx: IdentifyContext) -> str:
    """The best human-readable description of the track, or "".

    The caller's hint and the file's own title tag are real metadata and are
    used as given. The filename is a last fallback and is used only when it
    reads like a description: a yt-dlp download is named for its video id, and
    "dQw4w9WgXcQ" buys nothing but a confident hallucination for a call this
    provider can only afford a couple of hundred of per day.
    """
    for candidate in (ctx.hint_title, ctx.existing.title):
        if candidate and candidate.strip():
            return candidate.strip()
    stem = Path(ctx.path).stem.strip()
    return stem if _SEPARATOR.search(stem) else ""


def parse_response(raw: str) -> dict | None:
    """Pull a JSON object out of the model's reply, or None.

    Defensive by default: models wrap JSON in fences, prepend a sentence of
    explanation, or return a list. Anything that is not a plain object is a
    miss, not an exception — this provider is the end of the chain and its
    failure mode must be "the track stays unidentified", never "the worker
    raised".
    """
    text = (raw or "").strip()
    if not text:
        return None

    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()

    try:
        data = json.loads(text)
    except ValueError:
        # A stray sentence before the object is common enough to be worth one
        # bounded retry on the outermost braces.
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except ValueError:
            return None

    return data if isinstance(data, dict) else None


def _metadata_from(data: dict) -> TrackMetadata | None:
    title = _clean(data.get("title"))
    artist = _clean(data.get("artist"))
    if not title or not artist:
        return None

    is_compilation = _as_bool(data.get("is_compilation"))
    album_artist = "Various Artists" if is_compilation else ""

    return TrackMetadata(
        title=title,
        artist=artist,
        album=_clean(data.get("album")),
        album_artist=album_artist,
        year=_as_year(data.get("year")),
        is_compilation=is_compilation,
        confidence=CONFIDENCE,
        provider=GeminiProvider.name,
    )


def _clean(value) -> str:
    """Normalise a model-supplied string, mapping its placeholders to ""."""
    text = str(value or "").strip()
    return "" if text.casefold() in _PLACEHOLDERS else text


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"true", "yes", "1"}


def _as_year(value) -> int:
    try:
        year = int(float(value))
    except (TypeError, ValueError):
        return 0
    # Anything outside this is a hallucinated number, not a release year.
    return year if 1000 <= year <= 2999 else 0
