"""
Tier 1 — the file's own tags.

Zero cost: no network, no subprocess, no dependency, no rate limit. That is the
entire argument for running it first. A library of already-tagged MP3s resolves
here and never touches AcoustID, Shazam or Gemini at all, which is what makes a
full sweep of an existing collection finish in an afternoon rather than in days
(docs/MUSIC-LIBRARY-MERGE.md — Shazam and Gemini are both rate- and CPU-bound on
ARMv7, so any design that routes the bulk through them is not viable).

The provider trusts the tags rather than re-verifying them. Re-identifying a
file that already says "Pink Floyd — The Wall — Comfortably Numb" spends a
scarce quota to learn what the file already knew.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from .base import IdentifyContext, Provider, TrackMetadata

log = logging.getLogger("music.identify")

#: Title + artist + album: everything the Plex layout needs bar the track
#: number, which `library/tags.py` reads straight off the file anyway.
FULL_CONFIDENCE = 1.0
#: Title + artist only. Above the 0.5 default threshold, so it still wins the
#: chain — but deliberately below 1.0 so raising IDENTIFY_MIN_CONFIDENCE past
#: it is a working way to say "go and find the album for these".
PARTIAL_CONFIDENCE = 0.7


class TagsProvider(Provider):
    """Returns what the file already claims about itself."""

    name = "tags"

    def identify(self, ctx: IdentifyContext) -> TrackMetadata | None:
        existing = ctx.existing
        if not existing.is_usable():
            return None

        confidence = FULL_CONFIDENCE if existing.album else PARTIAL_CONFIDENCE
        log.debug(
            "tags: %s already carries usable metadata (%.2f)", ctx.path.name, confidence
        )
        return replace(existing, confidence=confidence, provider=self.name)
