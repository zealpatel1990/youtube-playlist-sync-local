"""Fetch cover art for embedding into audio files."""

from __future__ import annotations

import logging
import threading
import urllib.error
import urllib.request

log = logging.getLogger("music.artwork")

#: Album art is per-release, so every track of an album resolves to the same
#: URL. Caching by URL turns a 14-track album into one download.
_cache: dict[str, bytes | None] = {}
_lock = threading.Lock()

MAX_BYTES = 2_000_000
TIMEOUT_SECONDS = 20.0

_MAGIC = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
}


def fetch(url: str) -> bytes | None:
    """Download artwork, or None if it is unavailable or not an image."""
    if not url:
        return None

    with _lock:
        if url in _cache:
            return _cache[url]

    data = _download(url)
    with _lock:
        _cache[url] = data
    return data


def _download(url: str) -> bytes | None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "music-manager/1.0", "Accept": "image/*"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            # read(n+1) so an oversized image is detected rather than truncated
            # into a corrupt embed.
            data = response.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("artwork: could not fetch %s: %s", url, exc)
        return None

    if len(data) > MAX_BYTES:
        log.warning("artwork: %s is larger than %d bytes; skipping", url, MAX_BYTES)
        return None
    if not is_image(data):
        log.warning("artwork: %s did not return an image", url)
        return None
    return data


def is_image(data: bytes) -> bool:
    return any(data.startswith(magic) for magic in _MAGIC)


def mime_type(data: bytes) -> str:
    for magic, mime in _MAGIC.items():
        if data.startswith(magic):
            return mime
    return "image/jpeg"


def reset_for_tests() -> None:
    with _lock:
        _cache.clear()
