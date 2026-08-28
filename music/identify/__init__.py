"""
Identification: turn an audio file into metadata good enough to file it.

The public surface is deliberately four names. Callers build an
`IdentifyContext` from a Track, call `identify()`, and either get a
`TrackMetadata` or None; which provider answered, what it cost and what was
skipped are this package's business, not theirs.

Only `base` is imported here. The provider modules are pulled in lazily when the
chain is first built, so importing `music.identify` — which the tagging job
handlers do at startup — cannot fail because of an optional dependency that is
missing, broken, or built for the wrong ARM ABI.
"""

from __future__ import annotations

from .base import IdentifyContext, Provider, TrackMetadata, build_chain, identify

__all__ = [
    "IdentifyContext",
    "Provider",
    "TrackMetadata",
    "build_chain",
    "identify",
]
