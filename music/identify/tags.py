"""Tier 1 — the file's own tags. Zero cost, so it runs first."""

from __future__ import annotations

import logging
from dataclasses import replace

from .base import IdentifyContext, Provider, TrackMetadata

log = logging.getLogger("music.identify")

#: Title + artist + album.
FULL_CONFIDENCE = 1.0
#: Title + artist only. Below 1.0 so raising IDENTIFY_MIN_CONFIDENCE past it is
#: a working way to say "go and find the album for these".
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
