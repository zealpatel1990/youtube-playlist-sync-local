"""
Sync the YouTube playlist into YoutubeVideo rows, and queue what needs downloading.

Downloads are **queued, not run here**. The previous version's command
downloaded and tagged inline with its own `time.sleep(5)` between videos, so a
batch run competed with the live service for yt-dlp, ffmpeg and the database
while duplicating logic the job handlers already own. Enqueuing means one
downloader, one at a time, with leases and retries — whether the work was
started from cron, the dashboard, or here.
"""

from __future__ import annotations

import argparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

DOWNLOAD_JOB = "youtube.download"


class Command(BaseCommand):
    help = "Fetch the playlist into the database and queue downloads for new videos."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--url",
            default=None,
            help="Playlist URL. Defaults to PLAYLIST_URL from the environment.",
        )
        parser.add_argument(
            "--download",
            action=argparse.BooleanOptionalAction,
            default=True,
            help="Queue downloads for newly available videos (default: on).",
        )

    def handle(self, *args, **options) -> None:
        from music import ingest

        url = options["url"] or settings.PLAYLIST_URL
        if not url:
            raise CommandError(
                "no playlist URL. Pass --url, or set PLAYLIST_URL in .env."
            )

        counts = ingest.sync_playlist(url)
        self.stdout.write(
            self.style.SUCCESS(
                "playlist synced: {seen} seen, {added} added, {updated} updated".format(
                    **counts
                )
            )
        )

        if not options["download"]:
            return

        self._queue_downloads(ingest)

    def _queue_downloads(self, ingest) -> None:
        from music.jobs import engine, registry

        # The registry is populated by the worker bootstrap, which does not run
        # in a management command's process.
        registry.load_handlers()
        if registry.get(DOWNLOAD_JOB) is None:
            raise CommandError(
                f"the {DOWNLOAD_JOB} handler is not registered. Check the log for "
                f"an import error in music.jobs.handlers."
            )

        queued = 0
        for video in ingest.pending_downloads().iterator(chunk_size=200):
            engine.enqueue(
                DOWNLOAD_JOB,
                {"video_id": video.pk},
                dedup_key=f"{DOWNLOAD_JOB}:{video.pk}",
            )
            queued += 1

        if not queued:
            self.stdout.write("nothing to download: every available video has a track.")
            return
        # "or already pending", not "queued": enqueue is deduped against active
        # jobs, so re-running this command absorbs into the existing ones rather
        # than stacking a second copy of the same download.
        self.stdout.write(
            self.style.SUCCESS(
                f"{queued} download job(s) queued or already pending. The running "
                f"service drains them one at a time; start it if it is not up."
            )
        )
