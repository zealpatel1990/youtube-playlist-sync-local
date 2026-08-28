"""Latin-script metadata, so Plex search can find non-English music."""

from __future__ import annotations

import json
import logging
import threading
import unicodedata

from django.conf import settings

log = logging.getLogger("music.romanize")

#: Scripts we leave alone. Everything else is a candidate for romanization.
_LATIN_CATEGORIES = {"Lu", "Ll", "Lt", "Lm", "Lo"}

_cache: dict[str, str] = {}
_cache_lock = threading.Lock()

#: Gemini handles many strings per call, which is what keeps this inside the
#: free tier. Larger batches risk a truncated response.
BATCH_SIZE = 25


def is_latin(text: str) -> bool:
    """True when every letter in `text` is Latin (accents included)."""
    for char in text or "":
        if unicodedata.category(char) not in _LATIN_CATEGORIES:
            continue
        if not unicodedata.name(char, "").startswith("LATIN"):
            return False
    return True


def needs_romanization(text: str) -> bool:
    return bool(text and text.strip()) and not is_latin(text)


def romanize_all(strings: list[str]) -> dict[str, str]:
    """Map each non-Latin string to a Latin spelling.

    Gemini first, because mechanical transliteration is not good enough for
    Indic scripts: unidecode renders पुष्पा as "pusspaa" and ਸਤਿੰਦਰ ਸਰਤਾਜ as
    "stiNdr srtaaj", neither of which anyone would ever type into a search box.
    Gemini returns "Pushpa" and "Satinder Sartaaj".
    """
    pending = sorted({s for s in strings if needs_romanization(s)})
    if not pending:
        return {}

    resolved: dict[str, str] = {}
    with _cache_lock:
        for text in list(pending):
            if text in _cache:
                resolved[text] = _cache[text]
                pending.remove(text)

    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start:start + BATCH_SIZE]
        answers = _romanize_with_gemini(batch) or {}
        for text in batch:
            latin = answers.get(text) or _transliterate(text)
            if latin:
                resolved[text] = latin

    with _cache_lock:
        _cache.update(resolved)
    return resolved


def romanize(text: str) -> str:
    """Latin spelling of one string, or the original if it already is Latin."""
    if not needs_romanization(text):
        return text
    return romanize_all([text]).get(text, text)


def _romanize_with_gemini(batch: list[str]) -> dict[str, str] | None:
    from music.identify import gemini

    provider = gemini.GeminiProvider()
    if not provider.available():
        return None
    if not gemini.daily_budget().consume():
        log.info("romanize: Gemini daily budget spent; falling back to transliteration")
        return None
    if not gemini.rate_limiter().acquire(timeout=settings.PROVIDER_TIMEOUT_SECONDS):
        return None

    numbered = "\n".join(f"{i + 1}. {text}" for i, text in enumerate(batch))
    prompt = (
        "Romanize these music metadata strings (song titles, artist and album "
        "names) into the Latin spelling commonly used for them in English on "
        "streaming services. Keep names people would recognise rather than a "
        "literal phonetic transliteration. If a string is already Latin, "
        "return it unchanged.\n\n"
        f"{numbered}\n\n"
        'Return only JSON: {"results": [{"n": 1, "english": "..."}, ...]}'
    )

    try:
        raw = provider._generate(prompt)
        data = json.loads(raw)
    except Exception as exc:
        log.warning("romanize: Gemini call failed (%s); using transliteration", exc)
        return None

    answers: dict[str, str] = {}
    for item in (data or {}).get("results", []):
        try:
            index = int(item["n"]) - 1
            english = str(item["english"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= index < len(batch) and english and is_latin(english):
            answers[batch[index]] = english
    return answers


def _transliterate(text: str) -> str:
    """Mechanical fallback. Approximate for Indic scripts — see romanize_all."""
    try:
        from unidecode import unidecode
    except ImportError:
        return ""
    latin = unidecode(text).strip()
    if latin and latin != text:
        log.debug("romanize: transliterated %r to %r (approximate)", text, latin)
    return latin


def reset_for_tests() -> None:
    with _cache_lock:
        _cache.clear()
