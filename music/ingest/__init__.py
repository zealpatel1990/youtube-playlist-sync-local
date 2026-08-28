"""Ingestion from YouTube. A download ends as a `Track`, like a scanned file."""

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
