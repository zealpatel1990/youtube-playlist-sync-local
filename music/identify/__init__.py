"""Identification: turn an audio file into metadata good enough to file it.

Only `base` is imported here; provider modules load lazily when the chain is
built, so a missing optional dependency cannot break importing this package.
"""

from __future__ import annotations

from .base import (
    IdentifyContext,
    Provider,
    TrackMetadata,
    available_names,
    build_chain,
    identify,
)

__all__ = [
    "IdentifyContext",
    "Provider",
    "TrackMetadata",
    "build_chain",
    "available_names",
    "identify",
]
