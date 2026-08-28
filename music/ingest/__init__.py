"""
Ingestion from YouTube.

One source among others: a download ends as a `Track` on disk, identical to a
track a library scan found, so identification, tagging and organization are
written once and both paths get them.

The reasoning behind each function lives in `music.ingest.youtube`.
"""

from __future__ import annotations

from music.ingest.youtube import (
    HEARTBEAT_INTERVAL,
    SOCKET_TIMEOUT,
    PlaylistEntry,
    classify_availability,
    download_audio,
    list_playlist,
    pending_downloads,
    sync_playlist,
    upgrade_ytdlp,
    ytdlp_version,
)

__all__ = [
    "HEARTBEAT_INTERVAL",
    "SOCKET_TIMEOUT",
    "PlaylistEntry",
    "classify_availability",
    "download_audio",
    "list_playlist",
    "pending_downloads",
    "sync_playlist",
    "upgrade_ytdlp",
    "ytdlp_version",
]
