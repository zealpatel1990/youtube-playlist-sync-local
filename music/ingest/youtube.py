"""
YouTube ingestion: listing a playlist, and downloading one video's audio.

yt-dlp and ffmpeg are the two heaviest things this box runs, so this module is
shaped around that rather than around convenience:

* `yt_dlp` is imported lazily, inside `_ydl()`. Importing it builds a large
  extractor table; a web request that only renders the dashboard should not pay
  for that, and a Pi 2 notices. `_ydl` is also the single seam the tests patch,
  which is what keeps the suite offline.
* Exactly one download runs at a time, process-wide (`_download_slot`). With
  WORKER_THREADS=2, two workers would otherwise run two yt-dlp downloads and
  two ffmpeg transcodes across four 900MHz cores sharing 1GB of RAM — slower
  than doing them in sequence, and a good way to start swapping.
* Every subprocess call carries an explicit `timeout=`. The previous version's
  pip upgrade had none, and an untimed child holding the only worker thread is
  one of the few genuinely unbounded waits in the old code
  (docs/CODE-AUDIT.md A6).

Availability comes from yt-dlp's **structured** `availability` field, with the
title placeholder only as a fallback. The previous version compared
`title == '[Private Video]'` with a capitalisation that does not occur in the
wild, so every private entry was silently filed as UNAVAILABLE (A13).
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

#: How long a single socket operation may stall before yt-dlp gives up. yt-dlp
#: applies a 20s default of its own; setting it explicitly makes the value
#: visible and tunable in one place, which is what A6 asked for as
#: belt-and-braces around the paths that can wedge a worker.
SOCKET_TIMEOUT = 20

#: Minimum gap between heartbeats. A progress hook fires several times a
#: second and a heartbeat is a database write; on an SD card that is not free.
HEARTBEAT_INTERVAL = 30.0

#: A pip install on an ARMv7 box with a cold wheel cache is slow but bounded.
PIP_TIMEOUT = 600
VERSION_TIMEOUT = 30

#: SQLite's default host-parameter limit is 999; a large playlist would blow
#: through it in a single `pk__in`.
_ID_CHUNK = 400


@dataclass(frozen=True)
class PlaylistEntry:
    """One flat playlist entry, normalized.

    Frozen because nothing downstream should be editing a listing in place, and
    `duration` is an int rather than `int | None` on purpose: a None duration
    reaching a `<` comparison is what raised TypeError at a distance in the
    previous version (A14). Unknown is 0 here and everywhere.
    """

    video_id: str
    title: str
    uploader: str
    duration: int
    url: str
    availability: str


# --------------------------------------------------------------------------
# Availability classification (A13)
# --------------------------------------------------------------------------
#
# yt-dlp exposes `availability` on flat entries. It is the robust signal: the
# title placeholders are YouTube InnerTube strings passed through verbatim, so
# their exact casing is not ours to rely on — which is exactly how the previous
# version's `title == '[Private Video]'` came to match nothing.

_STRUCTURED_AVAILABILITY = {
    "public": Availability.AVAILABLE,
    # An unlisted video downloads perfectly well once you hold its id, and a
    # playlist entry is exactly that. It is AVAILABLE, not a third state.
    "unlisted": Availability.AVAILABLE,
    "private": Availability.PRIVATE,
    # Age- or account-gated. We cannot fetch it, and the reason is access
    # rather than removal, which is what PRIVATE means on the dashboard.
    "needs_auth": Availability.PRIVATE,
    "premium_only": Availability.UNAVAILABLE,
    "subscriber_only": Availability.UNAVAILABLE,
}

#: Compared case-folded, so `[Private video]`, `[Private Video]` and
#: `[PRIVATE VIDEO]` all land in the same place.
_TITLE_MARKERS = {
    "[private video]": Availability.PRIVATE,
    "[deleted video]": Availability.DELETED,
}


def classify_availability(entry: dict) -> str:
    """Map one raw yt-dlp entry onto an `Availability` value.

    Structured field first, title placeholder second, and only then a
    conservative guess. The guess defaults to AVAILABLE: filing a downloadable
    video as UNAVAILABLE means it is never attempted at all, whereas attempting
    a dead one costs a single failed job that retries with backoff. The old
    heuristic ("no duration means unavailable") had it the other way round and
    would strand live streams, which legitimately report no duration.
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
    """Seconds as a non-negative int. Unknown is 0, never None (A14)."""
    if value is None:
        return 0
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return 0
    return seconds if seconds > 0 else 0


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------


def list_playlist(url: str) -> list[PlaylistEntry]:
    """Fetch a playlist's entries without downloading anything.

    Errors propagate. The previous version caught `DownloadError` and returned
    an empty list, which turned "YouTube blocked us" into "the playlist is
    empty" — indistinguishable in the log, and the reason A15's latent risk
    would have been silent. Here the job engine records the failure and retries
    with backoff.
    """
    if not url:
        raise ValueError("a playlist URL is required")

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # 'in_playlist' asks the playlist extractor for flat entries and stops
        # there — one round trip per page instead of one per video.
        #
        # Deliberately absent: `force_generic_extractor`. It is deprecated, and
        # on the extract_info() path it was never read at all, so the previous
        # version's copy of it was pure misdirection (A15).
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
                # Truncated to what the columns hold. SQLite would store an
                # over-long value happily and it would fail on any other
                # backend, which is the worst of both worlds.
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
    """Yield flat video entries, descending into nested playlists.

    A channel URL returns a playlist *of playlists*, and yt-dlp yields None for
    entries it could not read at all. Both are handled once, here, rather than
    by every caller.
    """
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
    content changed**, not rows touched — so a second run over an unchanged
    playlist reports 0 and the number is worth reading.

    `last_seen_at` is refreshed for every seen row in one statement rather than
    by saving each row individually. N writes per sync for N unchanged rows is
    precisely the kind of idle SD-card cost this rewrite exists to remove, and
    freshness is not a content change.
    """
    entries = list_playlist(url)

    # A playlist can legitimately contain the same video twice; a duplicate
    # would make bulk_create raise on the primary key.
    unique: dict[str, PlaylistEntry] = {entry.video_id: entry for entry in entries}

    now = timezone.now()
    # Chunked because SQLite before 3.32 caps host parameters at 999, and
    # Raspberry Pi OS Buster ships 3.27 — a 1000-video playlist would otherwise
    # fail on exactly the deployment this is written for.
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
            # raises rather than silently re-INSERTing it (A5).
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

    `retry_at__isnull=True` is included on purpose. Both automated paths in the
    previous version filtered on `retry_at <= now` alone, so a failed row that
    never received a `retry_at` was invisible to every scheduler forever (A4).
    """
    queryset = (
        YoutubeVideo.objects.filter(
            availability=Availability.AVAILABLE, track__isnull=True
        )
        .filter(Q(retry_at__isnull=True) | Q(retry_at__lte=timezone.now()))
        .order_by("created_at")
    )
    return queryset[:limit] if limit else queryset


# --------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------

#: Held for the whole of one download+transcode. See `_download_slot`.
_slot = threading.Lock()


def download_audio(
    video: YoutubeVideo,
    dest_dir: Path,
    *,
    heartbeat: Callable[[], None] | None = None,
) -> Path:
    """Download one video's audio into `dest_dir`; return the file written.

    The returned path is **the one yt-dlp reports**, not `<id>.mp3` assembled
    from the output template. The previous version assembled it, and raised
    FileNotFoundError whenever the postprocessor picked a different extension
    or yt-dlp sanitised the name — the most common download failure in the
    journal, and never actually a download problem.

    `heartbeat` is called from yt-dlp's progress and postprocessor hooks, at
    most once every `HEARTBEAT_INTERVAL` seconds, so a twenty-minute download
    on a Pi 2 keeps extending its job lease instead of being reclaimed by the
    reaper and run a second time.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    url = video.url or WATCH_URL.format(video.video_id)
    hook = _heartbeat_hook(heartbeat)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(dest_dir / f"{video.video_id}.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(settings.AUDIO_QUALITY),
            }
        ],
        # The previous version left this False, so every download streamed
        # yt-dlp's progress bar into journald: thousands of lines per track,
        # every one of them a write to the SD card.
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
        # ffmpeg is the slow half on this hardware and it runs *after* the last
        # progress hook fires; without this the lease could expire mid-transcode.
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

    Asked in order of authority: the per-download record — which postprocessors
    update in place, so it names the finished .mp3 rather than the source
    .webm — then the info dict, and only as a last resort the directory itself.
    Never rebuilt from the output template; that assumption is what broke.
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

    # Nothing usable came back (an older yt-dlp, or an unexpected shape). Look
    # at what landed instead of guessing at a name. Matching on the stem skips
    # `<id>.mp3.part` and `<id>.f251.webm` leftovers for free.
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


def _heartbeat_hook(heartbeat: Callable[[], None] | None) -> Callable[[dict], None]:
    """Wrap `heartbeat` in a yt-dlp hook that fires at most once per interval.

    yt-dlp calls progress hooks several times a second. Each heartbeat is a
    database write, so an unthrottled hook turns one download into thousands of
    pointless SD-card writes.
    """
    state = {"last": 0.0}

    def hook(status: dict) -> None:
        if heartbeat is None:
            return
        now = time.monotonic()
        if state["last"] and now - state["last"] < HEARTBEAT_INTERVAL:
            return
        state["last"] = now
        _beat(heartbeat)

    return hook


def _beat(heartbeat: Callable[[], None] | None) -> None:
    """A lease extension must never be the thing that aborts a download."""
    if heartbeat is None:
        return
    try:
        heartbeat()
    except Exception:
        log.exception("heartbeat failed; continuing the download")


@contextmanager
def _download_slot(heartbeat: Callable[[], None] | None):
    """Serialize downloads process-wide.

    yt-dlp plus ffmpeg is the heaviest thing this box runs. Two at once on four
    900MHz cores sharing 1GB is slower than two in sequence and risks swapping,
    so the constraint is enforced here rather than by hoping nobody raises
    WORKER_THREADS above 1.

    The wait itself heartbeats: a worker blocked behind another download for
    twenty minutes would otherwise have its lease expire, be reclaimed by the
    reaper, and end up running the same download twice.
    """
    while not _slot.acquire(timeout=HEARTBEAT_INTERVAL):
        log.debug("waiting for the download slot")
        _beat(heartbeat)
    try:
        yield
    finally:
        _slot.release()


# --------------------------------------------------------------------------
# Version management
# --------------------------------------------------------------------------


def ytdlp_version() -> str:
    """The yt-dlp version *this process* is using.

    Read from the imported module rather than a subprocess, because that is the
    code actually doing the work. After `upgrade_ytdlp` this keeps reporting
    the old version until the service restarts — which is exactly why the
    restart is required, and why it is the caller's call and not this
    function's (A7).
    """
    try:
        module = _ytdlp()
    except ImportError:
        return "not installed"
    version = getattr(getattr(module, "version", None), "__version__", "")
    return str(version) if version else "unknown"


def upgrade_ytdlp() -> str:
    """pip-install the latest yt-dlp; return the version now on disk.

    Three deliberate choices:

    * **An explicit timeout.** The previous version's pip call had none, and an
      untimed child holding the only worker thread is one of the few genuinely
      unbounded waits in the old code (A6).
    * **`check=True`.** A failed upgrade must fail the job loudly rather than
      report success and leave the operator wondering why nothing changed.
    * **No restart.** The new package only takes effect in a fresh process, but
      restarting from inside the job that is running kills it before its own
      terminal state is persisted — the precise race in A7. The caller writes
      its state first, then restarts.
    """
    command = [
        settings.PIP_PATH, "install", "--upgrade",
        # The Pi's root filesystem is an SD card; pip's wheel cache is pure
        # cost there for a package upgraded a handful of times a year.
        "--no-cache-dir",
        # Saves pip an extra network round trip on a slow link, and stops it
        # printing an upgrade notice into the job's captured output.
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
    # "Requirement already satisfied" — nothing changed, so what is running is
    # what is installed.
    return ytdlp_version()


# --------------------------------------------------------------------------
# The yt-dlp boundary
# --------------------------------------------------------------------------


def _ydl(opts: dict):
    """Construct a YoutubeDL.

    Every yt-dlp call in the app goes through this one function, for two
    reasons: the import stays lazy, and the test suite has exactly one name to
    patch in order to stay off the network.
    """
    return _ytdlp().YoutubeDL(opts)


def _ytdlp():
    import yt_dlp  # noqa: PLC0415 — lazy on purpose; see the module docstring

    return yt_dlp
