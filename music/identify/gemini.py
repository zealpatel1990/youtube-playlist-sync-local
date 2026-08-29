"""Tier 4 — Gemini. Last resort.

Inference, not recognition: the model is shown a title string, never the audio.
The key is free-tier with a low daily quota, so calls are capped by a hard
`DailyBudget` as well as a rate limiter, and the client is built lazily inside
the method so an unset key cannot fail at import.
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

from music.core.ratelimit import RATE_LIMIT_WAIT_SECONDS, DailyBudget, RateLimiter

from .base import IdentifyContext, Provider, TrackMetadata

log = logging.getLogger("music.identify")

#: Just above the 0.5 default threshold and below every other provider: good
#: enough to file the track, never good enough to outrank audio.
CONFIDENCE = 0.55

#: Placeholders a model reaches for when it does not know. Treated as empty so
#: they cannot pass `is_usable()` and become a real-looking tag.
_PLACEHOLDERS = {
    "", "unknown", "unknown artist", "unknown title", "unknown album",
    "n/a", "na", "none", "null", "various", "untitled",
}

#: Strips the ```json ... ``` fence models add even when asked for raw JSON.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

#: Used to decide whether a filename reads like a description — see `_hint`.
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


# Module-level: a budget rebuilt alongside the provider would reset to full on
# every call and enforce nothing.

_limiter: RateLimiter | None = None
_budget: DailyBudget | None = None
_client = None
_client_key = ""
#: The day exhaustion was last logged, so the message appears once per day.
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
        # find_spec does not execute the package. It raises rather than
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
            log.debug("gemini: %s carries no title or tags to work from", ctx.path)
            return None

        # Checked before the limiter and consumed after it: the cheap read
        # short-circuits the steady state (quota gone, thousands of tracks left)
        # without parking each track on the limiter for a token it will not use.
        # The consume() below is the real decision, under the budget's own lock.
        if daily_budget().remaining <= 0:
            _note_exhausted()
            return None

        if not rate_limiter().acquire(timeout=RATE_LIMIT_WAIT_SECONDS):
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
    """Build the SDK client on first use and keep it — lazy so a missing key
    cannot fail at import, cached to reuse the HTTP stack."""
    global _client, _client_key
    with _state_lock:
        if _client is None or _client_key != api_key:
            from google import genai

            _client = genai.Client(api_key=api_key)
            _client_key = api_key
        return _client


def _describe(ctx: IdentifyContext) -> str:
    """The evidence block handed to the model, or "" when there is none.

    Text the app already has: no audio, no video, no file upload.
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

    The filename is used only when it reads like a description: a yt-dlp
    download is named for its video id, and "dQw4w9WgXcQ" buys only a
    confident hallucination.
    """
    for candidate in (ctx.hint_title, ctx.existing.title):
        if candidate and candidate.strip():
            return candidate.strip()
    stem = Path(ctx.path).stem.strip()
    return stem if _SEPARATOR.search(stem) else ""


def parse_response(raw: str) -> dict | None:
    """Pull a JSON object out of the model's reply, or None. Models fence JSON
    or prepend a sentence; anything not an object is a miss, not an exception."""
    text = (raw or "").strip()
    if not text:
        return None

    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()

    try:
        data = json.loads(text)
    except ValueError:
        # One bounded retry on the outermost braces, for a stray leading
        # sentence.
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
