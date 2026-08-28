# Architecture — the reformed app

A rewrite of the YouTube playlist sync into a **music library manager** that treats YouTube as one *source* among others, and Plex-convention organization as the shared destination for everything.

## The central idea

The old app's root object was a `Video`, with a local file hanging off it as an afterthought. That is why organizing existing MP3s did not fit: files that never came from YouTube had nowhere to live.

The new root object is a **`Track` — one audio file on disk.** Everything converges on it:

```
YouTube playlist ──download──┐
                             ├──► Track ──► identify ──► tag ──► organize (Plex layout)
Existing HDD folders ──scan──┘
```

A `Track` from a disk scan and a `Track` from a YouTube download are the same thing after ingestion, so identification, tagging and organization are written once and both sources get them. `YoutubeVideo` becomes a thin source record pointing at a `Track`.

## Efficiency: the design targets idle cost first

The Pi runs this 24/7, so what it costs when *nothing is happening* matters more than throughput. The old design's idle cost was substantial and grew with playlist size and open browser tabs. Two changes remove essentially all of it:

**1. Workers wait on an event, not on the database.** Old: every worker thread issued a SQLite query every `WORKER_POLL_SECONDS` forever. New: `enqueue()` sets a `threading.Event`; workers block on it. An idle system runs **zero queries**. Jobs still live in the DB for durability across restarts — the event is only the wakeup, with a long timeout as a safety net for anything enqueued by another process.

**2. SSE compares an in-memory counter, not the tables.** Old: each connected tab fetched *every* `Video` and *every* `LocalTrack` row, sorted both in Python, every 2–5 seconds — cost scaling with library size × open tabs. New: a module-level revision integer that any writer bumps, and a `threading.Condition` that streams wait on. An idle dashboard costs **zero queries per tick regardless of tab count**, and a change wakes all tabs at once instead of each discovering it independently.

This is only sound because the app is single-process. That invariant is now *enforced* at startup rather than left as a comment (see `core/runtime.py` guard), because both mechanisms silently break under `--workers 2`.

Everything else follows the same principle: hashing reads in 1 MB blocks instead of 4 KB; tags are written in a single `mutagen` save instead of two `eyed3` passes; the identification chain tries the free local source before any network call; indexes exist on the columns actually filtered.

## Job engine

`Job` rows carry a **lease** (`lease_expires_at`), which is the fix for the old design's worst failure mode — a wedged job that stayed `RUNNING` forever, blocking dedup so the work could never be re-queued until someone restarted the service.

- Claim: `UPDATE ... SET state=RUNNING, lease_expires_at=now+N WHERE id=? AND state=QUEUED` — still one atomic statement, still relying on nothing but SQLite.
- Long-running handlers call `job.heartbeat()` to extend the lease.
- A reaper (running in the scheduler thread, no extra thread) reclaims expired leases back to `QUEUED`, so wedges self-heal.
- Every job kind declares `max_attempts`; exhausted jobs go to `FAILED` with the error retained, rather than looping.
- Work that touches one track takes a **per-key lock** (`core/locks.py`), which closes the DELETE-during-DOWNLOAD race that could resurrect a deleted row.

## Identification chain — cheapest source first

Ordered so the expensive, rate-limited providers only ever see what the cheap ones could not resolve:

| Order | Provider | Cost | Handles |
|---|---|---|---|
| 1 | `tags` | Free, local | Files already carrying artist + album + title |
| 2 | `acoustid` | Free key, ~3 req/s | Bulk identification — Chromaprint fingerprint → MusicBrainz |
| 3 | `shazam` | Unofficial API, CPU-heavy | Rips, remixes, YouTube-only audio |
| 4 | `gemini` | **Free tier, very low limit** | Last resort inference from title/context |

Each provider returns a `TrackMetadata` with a confidence score, or `None` to pass. The chain stops at the first result clearing the provider's threshold. Providers are independently switchable, and every one is wrapped in a timeout and a token-bucket rate limiter — the old code had neither, and its untimed cover-art fetch was the most plausible way to hang a worker on flaky Pi networking.

AcoustID does the heavy lifting precisely because it returns *track and disc numbers* from MusicBrainz, which Shazam does not, and which the Plex layout requires.

## Plex organization

Path computation is a pure function (`services/plex.py`) — trivially testable, no IO:

```
<LIBRARY_ROOT>/<Album Artist>/<Album>/<NN> - <Title>.<ext>
<LIBRARY_ROOT>/Various Artists/<Album>/<NN> - <Title>.<ext>     # compilations
```

with multi-disc numbering folded in as `<disc><NN>` (disc 3 track 2 → `302`), matching the Plex guide. Disc and track numbers are also written to the tags, because Plex reads those over filenames.

Moves are **planned before they are applied**. A scan produces a manifest the user reviews; applying is a separate explicit action, records `previous_path` on every track, and is therefore reversible. Nothing is deleted, ever — organizing is a move, and duplicates are resolved by policy (`DUPLICATE_POLICY`: `keep-best-bitrate`, `keep-both`, or `report-only`, defaulting to the last).

## Layout

One Django app, packages inside it — fewer moving parts than several apps wired together.

```
music_manager/          settings.py, env.py (validated env readers), urls, wsgi
music/
  models.py             Track, YoutubeVideo, Job, ScanRoot
  plex.py               pure path computation — no IO, no ORM, no settings
  core/                 events, locks, runtime guard, atomic file IO, rate limits
  jobs/                 engine, registry, worker pool, scheduler
    handlers/           one module per domain: library, identify, organize,
                        youtube, maintenance
  identify/             base chain + tags / acoustid / shazam / gemini
  library/              scanner, organizer, tagio (mutagen)
  ingest/               youtube listing + download
  management/commands/  scan_library, organize, sync_youtube, identify_track,
                        migrate_legacy
  templates/ static/    htmx + SSE dashboard
```

`plex.py` takes no dependency on Django at all, so the naming policy that
decides where thousands of real files end up is testable in isolation.

## Cutover

The new code lands alongside the old `playlist/` app, which is simply **unwired from `INSTALLED_APPS`** rather than deleted — nothing coexists at runtime (no glue, no double workers), but the old code stays on disk for one release so it can be diffed and rolled back. A management command migrates existing `Video`/`LocalTrack` rows into `Track`/`YoutubeVideo` so no download history is lost. Delete `playlist/` once the new service has run clean.

The systemd unit keeps its name and `music_manager.wsgi:application` entrypoint, so the deployment does not change — **except** that the committed unit gains the `EnvironmentFile=` line it has always been missing (audit A1), which is why the current unit cannot boot from a clean checkout.

## What this fixes from the audit

| Audit | Fixed by |
|---|---|
| A1 unit cannot boot | `EnvironmentFile=` committed; settings validate and fail with a readable message |
| A2 SSE thread pin + full scan | In-memory revision counter; bounded long-poll; higher thread count |
| A3/A4 tagging failures stranded | One `FAILED` state with `retry_at` for every failure path, set in one place |
| A5 delete/download race | Per-key locks; all saves use `update_fields` |
| A6 wedged jobs | Job leases + reaper; timeouts on every network call |
| A7 restart race | Terminal state persisted before restart; `systemctl --no-block`; `sudo -n` fails loudly |
| A8 `.env` corruption | Lock + temp file + `fsync` + `os.replace` |
| A9 bad settings brick service | Per-field validation and clamping at both the form and settings layer |
| A10 test discovery side effects | Manual script becomes a management command |
| A11 toast XSS | `textContent`, never `innerHTML` |
| A12 DEBUG in production | `DEBUG`, `SECRET_KEY`, `ALLOWED_HOSTS` all from env |
| A13/A14/A15 | Structured `availability` field; `None`-safe duration; deprecated flag dropped |
