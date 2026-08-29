"""YouTube ingestion: listing a playlist, and downloading one video's audio.

`yt_dlp` is imported lazily inside `_ydl()`, which is also the single seam the
tests patch to stay offline. Exactly one download runs at a time process-wide
(`_download_slot`), and every subprocess call carries an explicit `timeout=`.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from music.models import Availability, YoutubeVideo

log = logging.getLogger("music.ingest")

WATCH_URL = "https://www.youtube.com/watch?v={}"

#: How long a single socket operation may stall before yt-dlp gives up.
SOCKET_TIMEOUT = 20

#: Minimum gap between heartbeats. A progress hook fires several times a second
#: and a heartbeat is a database write.
HEARTBEAT_INTERVAL = 30.0

#: A pip install on an ARMv7 box with a cold wheel cache is slow but bounded.
PIP_TIMEOUT = 600
VERSION_TIMEOUT = 30

#: SQLite's default host-parameter limit is 999; a large playlist would blow
#: through it in a single `pk__in`.
_ID_CHUNK = 400


@dataclass(frozen=True)
class PlaylistEntry:
    """One flat playlist entry, normalized. Unknown duration is 0, never None."""

    video_id: str
    title: str
    uploader: str
    duration: int
    url: str
    availability: str


# --- Availability classification ---------------------------------------
#
# The structured `availability` field is the robust signal: the title
# placeholders are InnerTube strings whose casing is not ours to rely on.

_STRUCTURED_AVAILABILITY = {
    "public": Availability.AVAILABLE,
    # An unlisted video downloads fine once you hold its id, which a playlist
    # entry is. AVAILABLE, not a third state.
    "unlisted": Availability.AVAILABLE,
    "private": Availability.PRIVATE,
    # Age- or account-gated: unfetchable for access reasons, not removal.
    "needs_auth": Availability.PRIVATE,
    "premium_only": Availability.UNAVAILABLE,
    "subscriber_only": Availability.UNAVAILABLE,
}

#: Compared case-folded; the casing of these strings varies in the wild.
_TITLE_MARKERS = {
    "[private video]": Availability.PRIVATE,
    "[deleted video]": Availability.DELETED,
}


def classify_availability(entry: dict) -> str:
    """Map one raw yt-dlp entry onto an `Availability` value.

    The last-resort guess defaults to AVAILABLE: filing a downloadable video as
    UNAVAILABLE means it is never attempted at all, whereas attempting a dead
    one costs one failed job.
    """
    raw = str(entry.get("availability") or "").strip().lower()
    mapped = _STRUCTURED_AVAILABILITY.get(raw)
    if mapped is not None:
        return mapped

    title = str(entry.get("title") or "").strip()
    marker = _TITLE_MARKERS.get(title.casefold())
    if marker is not None:
        return marker

    if not title and _coerce_duration(entry.get("duration")) == 0:
        # Nothing usable came back for this entry at all.
        return Availability.UNAVAILABLE
    return Availability.AVAILABLE


def _coerce_duration(value: Any) -> int:
    """Seconds as a non-negative int. Unknown is 0, never None."""
    if value is None:
        return 0
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return 0
    return seconds if seconds > 0 else 0


# --- Listing -----------------------------------------------------------


def list_playlist(url: str) -> list[PlaylistEntry]:
    """Fetch a playlist's entries without downloading anything.

    Errors propagate rather than becoming an empty list, which would turn
    "YouTube blocked us" into "the playlist is empty".
    """
    if not url:
        raise ValueError("a playlist URL is required")

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # Flat entries: one round trip per page instead of one per video.
        "extract_flat": "in_playlist",
        "socket_timeout": SOCKET_TIMEOUT,
    }

    log.info("listing playlist %s", url)
    with _ydl(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not isinstance(info, dict):
        raise RuntimeError(f"yt-dlp returned no playlist information for {url}")

    entries: list[PlaylistEntry] = []
    for raw in _iter_entries(info):
        video_id = str(raw.get("id") or "").strip()
        if not video_id:
            continue
        entries.append(
            PlaylistEntry(
                video_id=video_id[:32],
                # Truncated to what the columns hold: SQLite would store an
                # over-long value happily and fail on any other backend.
                title=str(raw.get("title") or "").strip()[:512],
                uploader=str(raw.get("uploader") or raw.get("channel") or "").strip()[
                    :255
                ],
                duration=_coerce_duration(raw.get("duration")),
                url=(
                    str(raw.get("url") or raw.get("webpage_url") or "").strip()
                    or WATCH_URL.format(video_id)
                )[:1024],
                availability=classify_availability(raw),
            )
        )

    log.info("playlist %s: %s entr(ies)", url, len(entries))
    return entries


def _iter_entries(info: dict, *, depth: int = 0) -> Iterator[dict]:
    """Yield flat video entries, descending into nested playlists — a channel
    URL returns a playlist *of playlists*."""
    entries = info.get("entries")
    if entries is None:
        yield info  # a single-video URL
        return
    if depth > 3:
        log.warning("stopping at nesting depth %s while walking entries", depth)
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("entries") is not None:
            yield from _iter_entries(entry, depth=depth + 1)
        else:
            yield entry


def sync_playlist(url: str) -> dict[str, int]:
    """Upsert every playlist entry into `YoutubeVideo`.

    Returns `{"seen", "added", "updated"}`, where **updated counts rows whose
    content changed**, not rows touched — `last_seen_at` is refreshed for every
    seen row in one statement, and freshness is not a content change.
    """
    entries = list_playlist(url)

    # A playlist can legitimately contain the same video twice, and a duplicate
    # would make bulk_create raise on the primary key.
    unique: dict[str, PlaylistEntry] = {entry.video_id: entry for entry in entries}

    now = timezone.now()
    # Chunked because SQLite before 3.32 caps host parameters at 999, and
    # Raspberry Pi OS Buster ships 3.27.
    existing: dict[str, YoutubeVideo] = {}
    for chunk in _chunks(list(unique), _ID_CHUNK):
        existing.update(YoutubeVideo.objects.in_bulk(chunk))

    to_create: list[YoutubeVideo] = []
    updated = 0

    with transaction.atomic():
        for video_id, entry in unique.items():
            row = existing.get(video_id)
            values = {
                "title": entry.title,
                "uploader": entry.uploader,
                "duration": entry.duration,
                "url": entry.url,
                "availability": entry.availability,
            }
            if row is None:
                to_create.append(YoutubeVideo(video_id=video_id, **values))
                continue

            changed = [
                field for field, value in values.items() if getattr(row, field) != value
            ]
            if not changed:
                continue
            for field in changed:
                setattr(row, field, values[field])
            # update_fields so a write against a row deleted underneath us
            # raises rather than silently re-INSERTing it.
            row.save(update_fields=[*changed, "updated_at"])
            updated += 1

        if to_create:
            YoutubeVideo.objects.bulk_create(to_create, batch_size=200)

        for chunk in _chunks(list(unique), _ID_CHUNK):
            YoutubeVideo.objects.filter(pk__in=chunk).update(last_seen_at=now)

    counts = {"seen": len(unique), "added": len(to_create), "updated": updated}
    log.info(
        "playlist sync: %s seen, %s added, %s updated",
        counts["seen"], counts["added"], counts["updated"],
    )
    return counts


def _chunks(items: list, size: int) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def pending_downloads(*, limit: int | None = None):
    """Videos eligible for download, in playlist order.

    `retry_at__isnull=True` is deliberate: filtering on `retry_at <= now` alone
    makes a failed row that never received a `retry_at` invisible forever.
    """
    queryset = (
        YoutubeVideo.objects.filter(
            availability=Availability.AVAILABLE, track__isnull=True
        )
        .filter(Q(retry_at__isnull=True) | Q(retry_at__lte=timezone.now()))
        .order_by("created_at")
    )
    return queryset[:limit] if limit else queryset


# --- Downloading -------------------------------------------------------

#: Held for the whole of one download+transcode. See `_download_slot`.
_slot = threading.Lock()


def download_audio(
    video: YoutubeVideo,
    dest_dir: Path,
    *,
    heartbeat: Callable[[], None] | None = None,
) -> Path:
    """Download one video's audio into `dest_dir`; return the file written.

    The returned path is **the one yt-dlp reports**, never `<id>.mp3` assembled
    from the output template: the postprocessor may pick a different extension
    or yt-dlp may sanitise the name. `heartbeat` fires from yt-dlp's hooks at
    most once every `HEARTBEAT_INTERVAL` seconds, so a long download keeps
    extending its job lease instead of being reclaimed and run a second time.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    url = video.url or WATCH_URL.format(video.video_id)
    hook = _heartbeat_hook(heartbeat)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(dest_dir / f"{video.video_id}.%(ext)s"),
        # AUDIO_FORMAT=native keeps YouTube's stream and runs no postprocessor:
        # single-threaded libmp3lame can take longer than the download itself
        # on a Pi, and it is a second lossy pass over already-lossy Opus.
        # native: remux into Ogg without re-encoding. yt-dlp hands back WebM,
        # which mutagen cannot tag AT ALL — no title, no artist, no cover art,
        # leaving the filename as the only metadata and Plex reads tags over
        # filenames. Remuxing is a container swap with `-c:a copy`, so it keeps
        # the whole point of native (no transcode, no second lossy pass) while
        # producing a file that can actually carry tags.
        "postprocessors": (
            [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": str(settings.AUDIO_QUALITY),
                }
            ]
            if settings.AUDIO_FORMAT == "mp3"
            else [{"key": "FFmpegVideoRemuxer", "preferedformat": "opus"}]
        ),
        # DASH audio comes as many small fragments; a few in flight keeps the
        # link busy instead of paying a round trip per fragment.
        "concurrent_fragment_downloads": settings.DOWNLOAD_CONCURRENT_FRAGMENTS,
        # Without these the progress bar streams thousands of lines per track
        # into journald, every one a write to the SD card.
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # A watch URL that happens to carry &list= must not drag the whole
        # playlist down with it.
        "noplaylist": True,
        "socket_timeout": SOCKET_TIMEOUT,
        "retries": 3,
        "fragment_retries": 3,
        "progress_hooks": [hook],
        # ffmpeg runs *after* the last progress hook fires; without this the
        # lease could expire mid-transcode.
        "postprocessor_hooks": [hook],
    }
    if settings.FFMPEG_LOCATION:
        opts["ffmpeg_location"] = settings.FFMPEG_LOCATION

    log.info("downloading %s (%s)", video.video_id, video.title or "untitled")
    with _download_slot(heartbeat):
        with _ydl(opts) as ydl:
            info = ydl.extract_info(url, download=True)

    path = _downloaded_path(info, dest_dir, video.video_id)
    if path is None:
        raise FileNotFoundError(
            f"yt-dlp reported no output file for {video.video_id} in {dest_dir}"
        )
    log.info("downloaded %s -> %s", video.video_id, path)
    return path


def _downloaded_path(info: Any, dest_dir: Path, video_id: str) -> Path | None:
    """The file yt-dlp actually wrote.

    The per-download record first: postprocessors update it in place, so it
    names the finished .mp3 rather than the source .webm.
    """
    candidates: list[str] = []
    if isinstance(info, dict):
        if info.get("entries") is not None:
            # noplaylist did not apply after all; the first real result is ours.
            info = next(
                (e for e in info["entries"] or [] if isinstance(e, dict)), {}
            )
        for download in info.get("requested_downloads") or []:
            if isinstance(download, dict):
                candidates.append(str(download.get("filepath") or ""))
        candidates.append(str(info.get("filepath") or ""))

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)

    # Nothing usable came back. Look at what landed rather than guess a name;
    # matching on the stem skips `<id>.mp3.part` and `<id>.f251.webm` leftovers.
    matches = [
        path
        for path in dest_dir.iterdir()
        if path.is_file()
        and path.stem == video_id
        and path.suffix.lower() in settings.AUDIO_EXTENSIONS
    ]
    if not matches:
        return None
    matches.sort(key=lambda p: (p.suffix.lower() != ".mp3", -p.stat().st_mtime))
    return matches[0]


def _heartbeat_hook(heartbeat: Callable[..., None] | None) -> Callable[[dict], None]:
    """Wrap `heartbeat` in a yt-dlp hook that fires at most once per interval.

    Progress hooks run several times a second; each heartbeat is a db write.
    The hook also passes a short status line, which the caller records on the
    job so the dashboard shows what is happening instead of an empty row.
    """
    state = {"last": 0.0}

    def hook(status: dict) -> None:
        if heartbeat is None:
            return
        now = time.monotonic()
        if state["last"] and now - state["last"] < HEARTBEAT_INTERVAL:
            return
        state["last"] = now
        _beat(heartbeat, _describe_progress(status))

    return hook


def _describe_progress(status: dict) -> str:
    """A one-line summary of a yt-dlp progress or postprocessor event."""
    kind = status.get("status") or ""
    if kind == "finished":
        return "downloaded; converting audio"
    if kind == "processing":
        return f"converting audio ({status.get('postprocessor') or 'ffmpeg'})"
    if kind != "downloading":
        return kind or ""

    done = status.get("downloaded_bytes") or 0
    total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
    speed = status.get("speed") or 0
    parts = []
    if total:
        parts.append(f"downloading {done * 100 // total}%")
        parts.append(f"{done / 1048576:.1f}/{total / 1048576:.1f} MB")
    else:
        parts.append(f"downloading {done / 1048576:.1f} MB")
    if speed:
        parts.append(f"{speed / 1024:.0f} KB/s")
    return "  ".join(parts)


def _beat(heartbeat: Callable[..., None] | None, status: str = "") -> None:
    """A lease extension must never be the thing that aborts a download."""
    if heartbeat is None:
        return
    try:
        heartbeat(status) if status else heartbeat()
    except TypeError:
        # A caller that only accepts a no-arg heartbeat.
        try:
            heartbeat()
        except Exception:
            log.exception("heartbeat failed; continuing the download")
    except Exception:
        log.exception("heartbeat failed; continuing the download")


@contextmanager
def _download_slot(heartbeat: Callable[[], None] | None):
    """Serialize downloads process-wide.

    Two yt-dlp+ffmpeg runs at once are slower than two in sequence on this
    hardware. The wait itself heartbeats: a worker blocked behind another
    download would otherwise lose its lease and run the same download twice.
    """
    while not _slot.acquire(timeout=HEARTBEAT_INTERVAL):
        log.debug("waiting for the download slot")
        _beat(heartbeat)
    try:
        yield
    finally:
        _slot.release()


# --- Version management ------------------------------------------------


def ytdlp_version() -> str:
    """The yt-dlp version *this process* is using.

    From the imported module, not a subprocess: after `upgrade_ytdlp` this
    keeps reporting the old version until the service restarts.
    """
    try:
        module = _ytdlp()
    except ImportError:
        return "not installed"
    version = getattr(getattr(module, "version", None), "__version__", "")
    return str(version) if version else "unknown"


def upgrade_ytdlp() -> str:
    """pip-install the latest yt-dlp; return the version now on disk.

    Does not restart: the new package needs a fresh process, but restarting
    from inside the running job would kill it before its terminal state is
    persisted. The caller writes its state first, then restarts.
    """
    command = [
        settings.PIP_PATH, "install", "--upgrade",
        # The root filesystem is an SD card; a wheel cache is pure cost here.
        "--no-cache-dir",
        # Keeps pip's upgrade notice out of the job's captured output.
        "--disable-pip-version-check",
        "yt-dlp",
    ]
    log.info("upgrading yt-dlp: %s", " ".join(command))
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=PIP_TIMEOUT,
        check=True,
    )

    version = _installed_ytdlp_version() or _version_from_pip_output(result.stdout or "")
    log.info(
        "yt-dlp upgrade finished: %s (a restart is required to load it)", version
    )
    return version


def _installed_ytdlp_version() -> str:
    """Ask the yt-dlp on disk, which this process's own import cannot know."""
    try:
        result = subprocess.run(
            [settings.YTDLP_PATH, "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not read the installed yt-dlp version: %s", exc)
        return ""
    text = (result.stdout or "").strip()
    return text.splitlines()[0].strip() if text else ""


#: pip prints "Successfully installed yt-dlp-2026.08.19" on a real upgrade.
_PIP_INSTALLED = re.compile(r"yt[-_]dlp-(\S+)")


def _version_from_pip_output(text: str) -> str:
    match = _PIP_INSTALLED.search(text)
    if match:
        return match.group(1)
    # "Requirement already satisfied": what is running is what is installed.
    return ytdlp_version()


# --- The yt-dlp boundary -----------------------------------------------


def _ydl(opts: dict):
    """Construct a YoutubeDL. Every yt-dlp call goes through here, so the
    import stays lazy and the tests have one name to patch."""
    return _ytdlp().YoutubeDL(opts)


def _ytdlp():
    import yt_dlp  # noqa: PLC0415 — lazy on purpose; see the module docstring

    return yt_dlp
