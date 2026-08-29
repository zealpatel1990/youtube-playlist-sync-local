"""Identification job handlers: one job per track through the provider chain."""

from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings

from music.core.locks import track_locks
from music.models import Track, TrackState
from music.jobs import engine
from music.jobs.registry import job

log = logging.getLogger("music.jobs.identify")

#: Every field _apply_metadata and the success path touch; keep in sync.
_IDENTIFIED_FIELDS = [
    "title", "artist", "album", "album_artist",
    "track_no", "disc_no", "year", "genre", "is_compilation",
    "musicbrainz_recording_id", "musicbrainz_release_id",
    "cover_url",
    "identified_by", "confidence",
    "state", "fail_count", "retry_at", "last_error",
    "updated_at",
]


@job("identify.track", max_attempts=3, cooldown=True,
     description="Identify one track through the provider chain")
def identify_track(job_obj) -> str:
    from music.identify import IdentifyContext, identify
    from music.library import tagio

    track_id = job_obj.payload.get("track_id")
    if not track_id:
        return "no track_id in payload; nothing to do"

    with track_locks.acquire(f"track:{track_id}") as acquired:
        if not acquired:  # pragma: no cover - only reachable with a timeout
            return "track busy"

        track = Track.objects.filter(pk=track_id).first()
        if track is None:
            return f"track {track_id} no longer exists"

        path = Path(track.path)
        if not path.exists():
            track.state = TrackState.MISSING
            track.save(update_fields=["state", "updated_at"])
            return f"file missing: {track.path}"

        track.state = TrackState.IDENTIFYING
        track.save(update_fields=["state", "updated_at"])

        existing = tagio.read_tags(path)
        hint_title = ""
        hint_url = ""
        video = getattr(track, "youtube_video", None)
        if video is not None:
            hint_title = video.title
            hint_url = video.url

        context = IdentifyContext(
            path=path,
            duration=track.duration,
            fingerprint=track.fingerprint,
            existing=existing,
            hint_title=hint_title,
            hint_url=hint_url,
        )

        def on_provider(name: str) -> None:
            engine.heartbeat(job_obj, f"asking {name}: {path.name[:60]}")

        try:
            result = identify(context, on_provider=on_provider)
        except Exception as exc:
            track.mark_failed(f"identification error: {exc}")
            raise

        if result is None:
            # Nothing recognised the audio, so fall back to what the upload
            # calls itself. This is a display name only: the dashboard shows it
            # instead of a bare video id, and the track stays FAILED so the
            # retry sweep keeps trying as catalogues grow.
            #
            # Deliberately no artist. That is what keeps a guess out of the
            # library — `plan_track` refuses anything without
            # `has_core_metadata` (title AND artist), so this can never be
            # filed under "Unknown Artist", and `TagsProvider` ignores a
            # title-only file, so a later pass cannot read this back and
            # mistake our own guess for an identification.
            #
            # Only when empty, so a re-identification that fails never
            # overwrites the name a provider gave earlier.
            if hint_title and not track.title:
                track.title = hint_title[:512]
                track.save(update_fields=["title", "updated_at"])
            track.mark_failed("no provider could identify this track")
            return f"unidentified: {track.path}"

        _apply_metadata(track, result)
        track.clear_failure()
        track.state = TrackState.IDENTIFIED
        # update_fields is required: the pk is set, so a bare save() against a
        # row a concurrent DELETE removed would INSERT and resurrect it.
        track.save(update_fields=_IDENTIFIED_FIELDS)

        engine.enqueue(
            "organize.track",
            {"track_id": track.pk, "apply": bool(settings.AUTO_ORGANIZE)},
            dedup_key=f"organize.track:{track.pk}",
        )
        return f"identified by {result.provider} ({result.confidence:.2f}): {result.artist} - {result.title}"


def _apply_metadata(track: Track, meta) -> None:
    """Copy provider output onto the Track without clobbering better local data."""
    track.title = meta.title or track.title
    track.artist = meta.artist or track.artist
    track.album = meta.album or track.album
    track.album_artist = meta.album_artist or track.album_artist
    track.track_no = meta.track_no or track.track_no
    track.disc_no = meta.disc_no or track.disc_no
    track.year = meta.year or track.year
    track.genre = meta.genre or track.genre
    track.is_compilation = meta.is_compilation or track.is_compilation
    track.musicbrainz_recording_id = (
        meta.musicbrainz_recording_id or track.musicbrainz_recording_id
    )
    track.musicbrainz_release_id = (
        meta.musicbrainz_release_id or track.musicbrainz_release_id
    )
    track.cover_url = (meta.cover_url or track.cover_url)[:1024]
    track.identified_by = meta.provider
    track.confidence = meta.confidence


@job("identify.pending", max_attempts=1,
     description="Queue identification for every track that still needs it")
def identify_pending(job_obj) -> str:
    """Fan out: enqueue one identify.track job per track needing identification."""
    limit = int(job_obj.payload.get("limit") or 500)
    ids = (
        Track.objects.filter(state__in=[TrackState.DISCOVERED, TrackState.FAILED])
        .order_by("id")
        .values_list("id", flat=True)[:limit]
    )
    queued = 0
    for track_id in list(ids):
        engine.enqueue(
            "identify.track",
            {"track_id": track_id},
            dedup_key=f"identify.track:{track_id}",
        )
        queued += 1
    return f"queued {queued} track(s) for identification"
