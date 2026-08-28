"""YouTube ingestion job handlers."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from music.core.locks import track_locks
from music.models import Availability, Source, Track, TrackState, YoutubeVideo
from music.jobs import engine
from music.jobs.registry import job

log = logging.getLogger("music.jobs.youtube")


@job("youtube.sync", max_attempts=2, cooldown=True,
     description="Refresh the playlist listing and queue new downloads")
def sync(job_obj) -> str:
    from music.ingest import youtube

    url = job_obj.payload.get("url") or settings.PLAYLIST_URL
    if not url:
        return "no playlist URL configured"

    stats = youtube.sync_playlist(url)
    queued = _queue_downloads()
    return (
        f"{stats.get('seen', 0)} entries, {stats.get('added', 0)} new; "
        f"queued {queued} download(s)"
    )


def _queue_downloads() -> int:
    """Enqueue downloads for available videos with no track yet, or due for retry."""
    from django.db.models import Q

    now = timezone.now()
    candidates = (
        YoutubeVideo.objects.filter(availability=Availability.AVAILABLE, track__isnull=True)
        .filter(Q(retry_at__isnull=True) | Q(retry_at__lte=now))
        .order_by("created_at")
        .values_list("video_id", flat=True)[:200]
    )
    count = 0
    for video_id in list(candidates):
        engine.enqueue(
            "youtube.download",
            {"video_id": video_id},
            dedup_key=f"youtube.download:{video_id}",
        )
        count += 1
    return count


@job("youtube.download", max_attempts=3, cooldown=True,
     description="Download one video's audio and hand it to the pipeline")
def download(job_obj) -> str:
    from music.ingest import youtube
    from music.library import tagio

    video_id = job_obj.payload.get("video_id")
    if not video_id:
        return "no video_id in payload"

    with track_locks.acquire(f"video:{video_id}") as acquired:
        if not acquired:  # pragma: no cover
            return "video busy"

        video = YoutubeVideo.objects.filter(pk=video_id).first()
        if video is None:
            return f"video {video_id} no longer exists"
        if video.track_id and Path(video.track.path).exists():
            return f"already downloaded: {video.track.path}"
        if video.availability != Availability.AVAILABLE:
            return f"skipped, availability is {video.availability}"

        staging = Path(settings.DOWNLOAD_STAGING)
        staging.mkdir(parents=True, exist_ok=True)

        def heartbeat() -> None:
            engine.heartbeat(job_obj)

        try:
            path = youtube.download_audio(video, staging, heartbeat=heartbeat)
        except Exception as exc:
            video.fail_count += 1
            video.last_error = str(exc)[:2000]
            hours = min(2.0 * (2 ** (video.fail_count - 1)), 168.0)
            video.retry_at = timezone.now() + timedelta(hours=hours)
            video.save(
                update_fields=["fail_count", "last_error", "retry_at", "updated_at"]
            )
            raise

        duration, bitrate = tagio.read_audio_properties(path)
        track, _ = Track.objects.update_or_create(
            path=str(path),
            defaults={
                "source": Source.YOUTUBE,
                "state": TrackState.DISCOVERED,
                "size_bytes": path.stat().st_size,
                "mtime": path.stat().st_mtime,
                "duration": duration or video.duration,
                "bitrate": bitrate,
                # The only metadata we have yet; the identify chain hints off it.
                "title": video.title,
            },
        )

        video.track = track
        video.fail_count = 0
        video.retry_at = None
        video.last_error = ""
        video.save(update_fields=["track", "fail_count", "retry_at", "last_error", "updated_at"])

        engine.enqueue(
            "identify.track",
            {"track_id": track.pk},
            dedup_key=f"identify.track:{track.pk}",
        )
        return f"downloaded {path.name}"
