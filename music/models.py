"""Data model. `Track` is the root object: one audio file on disk.

Unknown numbers are stored as 0, never NULL. `duration`, `track_no` and
`disc_no` are compared against thresholds all over the identification code, and
a NULL reaching a comparison raises TypeError at a distance; 0 compares
harmlessly. Every failure path goes through `mark_failed`, so no track can be
both FAILED and invisible to the retry query.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import models
from django.utils import timezone


class Source(models.TextChoices):
    LIBRARY = "LIBRARY", "Existing library"
    YOUTUBE = "YOUTUBE", "YouTube"


class TrackState(models.TextChoices):
    DISCOVERED = "DISCOVERED", "Discovered"
    IDENTIFYING = "IDENTIFYING", "Identifying"
    IDENTIFIED = "IDENTIFIED", "Identified"
    ORGANIZED = "ORGANIZED", "Organized"
    FAILED = "FAILED", "Failed"
    SKIPPED = "SKIPPED", "Skipped"
    MISSING = "MISSING", "File missing"

    @classmethod
    def terminal(cls) -> tuple[str, ...]:
        return (cls.ORGANIZED, cls.SKIPPED)


class Track(models.Model):
    """One audio file on disk."""

    # --- identity -------------------------------------------------------
    path = models.CharField(max_length=1024, unique=True)
    #: Where the file was before the last organize, for rollback.
    previous_path = models.CharField(max_length=1024, blank=True)
    #: Destination computed by a plan, applied only on an explicit action.
    planned_path = models.CharField(max_length=1024, blank=True)
    plan_note = models.CharField(max_length=255, blank=True)

    size_bytes = models.BigIntegerField(default=0)
    #: Unix timestamp; cheap change detection on rescan.
    mtime = models.FloatField(default=0.0)
    #: Populated lazily — hashing is a full IO pass.
    content_hash = models.CharField(max_length=64, blank=True, db_index=True)

    # --- audio properties ----------------------------------------------
    #: Seconds. 0 means unknown, never NULL — see the module docstring.
    duration = models.PositiveIntegerField(default=0)
    bitrate = models.PositiveIntegerField(default=0)
    #: Chromaprint fingerprint, reused across identification attempts.
    fingerprint = models.TextField(blank=True)

    # --- metadata -------------------------------------------------------
    title = models.CharField(max_length=512, blank=True)
    artist = models.CharField(max_length=512, blank=True)
    album = models.CharField(max_length=512, blank=True)
    album_artist = models.CharField(max_length=512, blank=True)
    track_no = models.PositiveIntegerField(default=0)
    disc_no = models.PositiveIntegerField(default=0)
    year = models.PositiveIntegerField(default=0)
    genre = models.CharField(max_length=255, blank=True)
    is_compilation = models.BooleanField(default=False)

    musicbrainz_recording_id = models.CharField(max_length=64, blank=True)
    musicbrainz_release_id = models.CharField(max_length=64, blank=True)

    # --- provenance -----------------------------------------------------
    source = models.CharField(
        max_length=16, choices=Source.choices, default=Source.LIBRARY, db_index=True
    )
    identified_by = models.CharField(max_length=32, blank=True)
    confidence = models.FloatField(default=0.0)

    # --- pipeline state -------------------------------------------------
    state = models.CharField(
        max_length=16, choices=TrackState.choices, default=TrackState.DISCOVERED
    )
    fail_count = models.PositiveIntegerField(default=0)
    retry_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    organized_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            # The pipeline sweep: "what needs work, and is it due yet?"
            models.Index(fields=["state", "retry_at"], name="track_state_retry_idx"),
            models.Index(fields=["state", "source"], name="track_state_source_idx"),
            models.Index(fields=["album_artist", "album"], name="track_album_idx"),
            models.Index(fields=["-created_at"], name="track_created_idx"),
        ]

    def __str__(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.path.rsplit("/", 1)[-1] or self.path

    # --- helpers --------------------------------------------------------

    @property
    def display_name(self) -> str:
        return str(self)

    @property
    def has_core_metadata(self) -> bool:
        """Enough to place the file in the Plex tree."""
        return bool(self.title and (self.artist or self.album_artist))

    @property
    def needs_move(self) -> bool:
        return bool(self.planned_path) and self.planned_path != self.path

    def effective_album_artist(self) -> str:
        if self.is_compilation:
            return "Various Artists"
        return self.album_artist or self.artist

    def mark_failed(self, error: str, *, backoff_base_hours: float = 2.0) -> None:
        """The single failure path, so a failed track is always both FAILED
        *and* carrying a retry_at that the retry query can see."""
        self.fail_count += 1
        self.state = TrackState.FAILED
        self.last_error = (error or "")[:2000]
        # Exponential backoff, capped at a week.
        hours = min(backoff_base_hours * (2 ** (self.fail_count - 1)), 168.0)
        self.retry_at = timezone.now() + timedelta(hours=hours)
        self.save(
            update_fields=[
                "fail_count",
                "state",
                "last_error",
                "retry_at",
                "updated_at",
            ]
        )

    def clear_failure(self) -> None:
        self.fail_count = 0
        self.retry_at = None
        self.last_error = ""


class Availability(models.TextChoices):
    AVAILABLE = "AVAILABLE", "Available"
    PRIVATE = "PRIVATE", "Private"
    UNAVAILABLE = "UNAVAILABLE", "Unavailable"
    DELETED = "DELETED", "Deleted"


class YoutubeVideo(models.Model):
    """A YouTube playlist entry. A thin source record pointing at a Track."""

    #: Not assumed to be 11 characters, so a future id format is not truncated.
    video_id = models.CharField(max_length=32, primary_key=True)
    title = models.CharField(max_length=512, blank=True)
    uploader = models.CharField(max_length=255, blank=True)
    #: Seconds; 0 means unknown.
    duration = models.PositiveIntegerField(default=0)
    url = models.URLField(max_length=1024, blank=True)

    availability = models.CharField(
        max_length=16,
        choices=Availability.choices,
        default=Availability.AVAILABLE,
        db_index=True,
    )

    track = models.OneToOneField(
        Track,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="youtube_video",
    )

    fail_count = models.PositiveIntegerField(default=0)
    retry_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["availability", "retry_at"], name="yt_avail_retry_idx"
            ),
        ]

    def __str__(self) -> str:
        return self.title or self.video_id

    @property
    def is_downloaded(self) -> bool:
        return self.track_id is not None


class ScanRoot(models.Model):
    """A directory to scan for existing audio files."""

    path = models.CharField(max_length=1024, unique=True)
    enabled = models.BooleanField(default=True)
    last_scan_started_at = models.DateTimeField(null=True, blank=True)
    last_scan_finished_at = models.DateTimeField(null=True, blank=True)
    files_seen = models.PositiveIntegerField(default=0)
    files_added = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["path"]

    def __str__(self) -> str:
        return self.path


class JobState(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"

    @classmethod
    def active(cls) -> tuple[str, ...]:
        return (cls.QUEUED, cls.RUNNING)


class Job(models.Model):
    """A unit of background work. Durable so it survives a restart, but workers
    are woken by an in-process event rather than by polling this table."""

    kind = models.CharField(max_length=64, db_index=True)
    #: Handler arguments.
    payload = models.JSONField(default=dict, blank=True)

    #: Non-empty for dedupable work. The partial unique index below makes "only
    #: one active job per key" a database guarantee, not a check-then-create.
    dedup_key = models.CharField(max_length=255, blank=True)

    state = models.CharField(
        max_length=16, choices=JobState.choices, default=JobState.QUEUED
    )
    #: Higher runs first.
    priority = models.IntegerField(default=0)

    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=3)

    #: The reaper reclaims expired leases, so a wedged handler self-heals
    #: instead of blocking the queue until a restart.
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    #: A job is not claimable before this time.
    scheduled_for = models.DateTimeField(default=timezone.now)

    message = models.TextField(blank=True)
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The claim query, in its exact shape.
            models.Index(
                fields=["state", "scheduled_for", "-priority"],
                name="job_claim_idx",
            ),
            models.Index(fields=["state", "lease_expires_at"], name="job_lease_idx"),
            models.Index(fields=["-created_at"], name="job_created_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["dedup_key"],
                condition=models.Q(state__in=["QUEUED", "RUNNING"])
                & ~models.Q(dedup_key=""),
                name="job_unique_active_dedup_key",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind}#{self.pk} ({self.state})"

    @property
    def is_active(self) -> bool:
        return self.state in JobState.active()

    @property
    def duration_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or timezone.now()
        return (end - self.started_at).total_seconds()
