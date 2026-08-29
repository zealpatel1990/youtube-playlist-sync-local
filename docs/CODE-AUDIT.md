# Code Audit — verified defect register

Produced by a full parallel read of every subsystem, followed by an adversarial verification pass in which each claim was handed to an independent reviewer instructed to **refute** it and to mark CONFIRMED only where the code unambiguously supported every load-bearing part.

**Result: 7 CONFIRMED, 8 PARTIAL, 0 refuted outright.** Every PARTIAL has a *Correction* — read it before fixing, because in several cases the real mechanism differs from the obvious one and fixing the obvious thing would leave the bug in place (A4 especially).

Severity is rated against the actual deployment: a single-user, LAN-only, unauthenticated dashboard on a Raspberry Pi 2.

| # | Finding | Verdict | Severity |
|---|---------|---------|----------|
| A1 | Committed systemd unit cannot boot the app | CONFIRMED | High |
| A2 | SSE pins a thread per tab and full-scans both tables per tick | CONFIRMED | High |
| A3 | Tagging failure sets `TAGGING`, never `FAILED` | CONFIRMED | Medium |
| A4 | Tagging failures are never retried automatically | PARTIAL | Medium |
| A5 | DELETE races an in-flight DOWNLOAD and resurrects the row | CONFIRMED | Medium |
| A6 | No job timeout; dedup pins to a wedged RUNNING job | PARTIAL | Medium |
| A7 | Update-yt-dlp restart races the job's own SUCCESS write | CONFIRMED | Medium |
| A8 | `.env` rewritten non-atomically from the running app | CONFIRMED | Medium |
| A9 | Settings form accepts values that break the service | PARTIAL | Medium |
| A10 | `test_tagger.py` runs live side effects during test discovery | CONFIRMED | Medium |
| A11 | Stored XSS via video title in toast messages | PARTIAL | Medium |
| A12 | `DEBUG=True` in production | PARTIAL | Medium |
| A13 | Private videos misclassified as UNAVAILABLE | PARTIAL | Low |
| A14 | `None` duration raises in the Gemini fallback | PARTIAL | Low |
| A15 | Deprecated `force_generic_extractor` flag | PARTIAL | Low |

---

## A1 — Committed systemd unit cannot boot the app · CONFIRMED · High

[deploy/music_manager.service](deploy/music_manager.service) contains only `Environment="PATH=..."` and a placeholder `Environment='ENVNAME="value"'`. There is **no `EnvironmentFile=`** anywhere in the unit. [manage.py:14](manage.py#L14) holds the repo's only `load_dotenv` call; [wsgi.py](music_manager/wsgi.py) never imports dotenv; [settings.py:181,185](music_manager/settings.py#L181) do `os.environ["PLAYLIST_URL"]` / `os.environ["GEMINI_API_KEY"]` at import. There is no `gunicorn.conf.py` or `asgi.py` loading the environment another way.

Under gunicorn the worker fails during app import, the arbiter exits with "Worker failed to boot", `Restart=always`/`RestartSec=3` retries, and `StartLimitBurst=5`/`StartLimitIntervalSec=100` then halts the unit permanently. The repo contradicts itself about this: [.env.example:1](.env.example#L1) says "systemd loads this via `EnvironmentFile=`" and [manage.py:13](manage.py#L13) references "the Pi's EnvironmentFile", but the directive was never committed.

**The live Pi must therefore be running a hand-edited unit that is not in this repo** — worth recovering and committing, since the repo currently cannot reproduce the running deployment.

**Fix:** add `EnvironmentFile=/home/pi/music-manager/.env` to `[Service]` and delete the placeholder. For belt-and-braces, mirror `manage.py` in `wsgi.py` with `load_dotenv(BASE_DIR / ".env")` before `get_wsgi_application()`.

## A2 — SSE pins a thread per tab and full-scans both tables per tick · CONFIRMED · High

[views.py:279-285](playlist/views.py#L279) `_state_signature()` returns `tuple(sorted(Video.objects.values_list("id","status")))` and the same for every `LocalTrack` row, plus a third query over active Jobs. No `LIMIT`, no aggregate — **every row of both tables is materialized into Python and sorted**, per tick, per connected client, with nothing shared or cached across connections ([views.py:288-307](playlist/views.py#L288) builds a fresh generator per request). Default cadence 5s ([settings.py:198](music_manager/settings.py#L198)); `.env.example` ships 2s.

The endpoint is a plain sync WSGI generator returned via `StreamingHttpResponse`; its own docstring concedes that each open dashboard holds a thread. The unit runs `--workers 1 --threads 4`, and a gthread worker iterates the response body in its pooled thread until a yield fails after disconnect. **Four open tabs consume the entire request pool of the single worker process** — every other request, including the htmx fragment refetches the SSE `update` event itself triggers, then queues indefinitely. A phone plus a forgotten desktop tab gets you halfway there.

**Fix:** two independent changes. (1) Cheapen and share the signature: replace the row dump with aggregates (per-status `Count` plus `Max(updated_at)`) and cache it module-level, refreshed at most once per `SSE_POLL_SECONDS` under a lock — or better, have the worker threads bump an in-memory change counter, so N tabs cost zero extra queries. (2) Unpin the threads: either raise `--threads` (sleeping threads are cheap even on a Pi 2) or convert `/events/` to a bounded long-poll that returns after the first change or ~30s so threads recycle.

## A3 — Tagging failure sets `TAGGING`, never `FAILED` · CONFIRMED · Medium

[tagger_service.py:40-44](playlist/services/tagger_service.py#L40) — `_mark_as_failed(self, error_message)` sets `processing_status = TAGGING`, increments `fail_count`, saves. The `error_message` parameter is **never referenced**, `retry_at` is never set, and `FAILED` is never used. Compare [downloader_service.py:79-82](playlist/services/downloader_service.py#L79), which correctly sets FAILED + `retry_at`.

Aggravators: the parent DOWNLOAD job still reports SUCCESS "Downloaded and tagged" because `tag_and_rename_track` swallows the exception ([job_runner.py:116-119](playlist/services/job_runner.py#L116)); the dashboard only shows fail counts when status is `FAILED` ([_video_row.html:33-38](playlist/templates/playlist/_video_row.html#L33)), so the row displays "Tagging with Shazam" forever; and `LocalTrack` has no `error_message` field, so the message is discarded entirely. `fail_count` is incremented but read nowhere.

**Fix:** mirror the downloader — set `FAILED`, log `error_message` (at minimum `logger.error`), set `retry_at = now + timedelta(days=2*fail_count)`. Consider adding an `error_message` field so failures are diagnosable from the UI instead of the journal.

## A4 — Tagging failures are never retried automatically · PARTIAL · Medium

Both automated retry paths — [sync_playlist.py:48](playlist/management/commands/sync_playlist.py#L48) and `_videos_needing_download` at [job_runner.py:87-89](playlist/services/job_runner.py#L87), used by AUTO_SYNC — filter on `FAILED AND retry_at <= now`. Tracks that fail during tagging match neither condition, so nothing the scheduler runs ever picks them up.

> **Correction — read before fixing.** The original claim was that NULL `retry_at` is the blocker, since `retry_at <= now` is never true for NULL. That is a *latent* second blocker, not the operative one. The **status** condition fails first: A3 leaves the row at `TAGGING`, not `FAILED`. Fixing only the NULL handling would change nothing. Also, "permanently invisible" applies only to automated paths — the manually triggered TAG_ALL job ([views.py:209](playlist/views.py#L209) → [job_runner.py:126-131](playlist/services/job_runner.py#L126)) does select `TAGGING`/`DOWNLOADED` and recovers these tracks. Nothing enqueues it on a schedule.

**Fix:** A3's fix resolves the primary blocker. Then defensively widen both filters to `Q(retry_at__isnull=True) | Q(retry_at__lte=now)` so a FAILED row with NULL `retry_at` is retried rather than orphaned. Optionally have `_handle_auto_sync` enqueue a TAG_ALL when any track has a `local_path` but no COMPLETED status — retagging an already-downloaded file avoids re-downloading it, and existing dedup prevents stacking.

## A5 — DELETE races an in-flight DOWNLOAD and resurrects the row · CONFIRMED · Medium

Dedup is per-type ([job_service.py:24-28](playlist/services/job_service.py#L24) filters on `job_type=job_type`), so a DELETE never matches an active DOWNLOAD for the same video, and `claim_next_job` has no per-video exclusion. With `WORKER_THREADS=2`, one thread can run DELETE while another is mid-DOWNLOAD on the same video. The tagging phase runs for tens of seconds after `local_path` is saved (3 Shazam attempts with 5s sleeps), so `_handle_delete` removing the file lands mid-job.

The nastiest part: `LocalTrack`'s PK is its OneToOne `video` field, and the post-download/tagger `save()` calls pass no `update_fields`. On Django 5.2.16 a zero-row UPDATE falls through to an INSERT with the same PK ([base.py:1145-1169](/.venv/Lib/site-packages/django/db/models/base.py)) — **silently resurrecting the row the DELETE just removed**, with the `Video` FK still present because delete only sets `status=DELETED`.

TAG_ALL has the same shape: it selects exactly the `TAGGING`/`DOWNLOADED` states an in-flight DOWNLOAD passes through, with no lock — producing duplicate Shazam calls, concurrent `eyed3` saves on one file, and a rename race yielding either `FileNotFoundError` or a stray `Artist - Title (1).mp3`.

**Fix:** everything is in one process, so a module-level `dict` of `threading.Lock` keyed by `video_id` is sufficient — acquired by `_handle_download`, `_handle_delete`, and the per-track body of `_handle_tag_all` (the latter with `blocking=False`, skipping busy tracks). Separately, pass `update_fields=` on the post-download saves so a write against a deleted row raises instead of silently re-inserting.

## A6 — No job timeout; dedup pins to a wedged RUNNING job · PARTIAL · Medium

Confirmed mechanism: `run_job` calls handlers synchronously with no watchdog; dedup returns any active job including a wedged RUNNING one ([job_service.py:24-32](playlist/services/job_service.py#L24)), so re-enqueues are absorbed; and stale RUNNING rows are requeued **only** at boot ([worker.py:39-43](playlist/services/worker.py#L39)). With `WORKER_THREADS=2`, two wedged jobs halt all processing including AUTO_SYNC, recoverable only by restart.

> **Correction.** The claimed trigger — "a hung yt-dlp or Shazam call blocks forever" — is wrong. Installed yt-dlp applies `DEFAULT_TIMEOUT = 20` seconds to all its own HTTP I/O, and shazamio inherits aiohttp's default `ClientTimeout(total=300)` with capped retries. Ordinary network stalls in those two therefore raise, get caught, and finish the job as FAILED with backoff. The **genuinely unbounded** paths are narrower and elsewhere: the untimed `urlopen(cover_url)` cover-art fetch at [tagger_service.py:152](playlist/services/tagger_service.py#L152) (most plausible on flaky Pi networking — a half-open TCP connection hangs indefinitely, and a hang is not an exception so the surrounding `try/except` never fires), a wedged `ffmpeg` child inside yt-dlp's postprocessor, and the untimed `pip` subprocess in UPDATE_YTDLP. When one of those fires, the rest of the claim holds exactly.

**Fix:** add `timeout=30` to the `urlopen` call and a `timeout=` to the pip `subprocess.run`; set an explicit `socket_timeout` in `ydl_opts` as belt-and-braces. For the wedge itself, add a stale-job reaper to the existing scheduler loop: any Job RUNNING with `started_at` older than a cap (say 2 hours) is flipped to QUEUED or FAILED by a single conditional UPDATE. No new thread or process, and it unpins dedup as a side effect.

## A7 — Update-yt-dlp restart races the job's own SUCCESS write · CONFIRMED · Medium

[job_runner.py:170-172](playlist/services/job_runner.py#L170) spawns `Popen(["sh","-c", f"sleep 3; sudo systemctl restart {SYSTEMD_SERVICE}"])` and returns; `_finish` persists SUCCESS only *after* the handler returns. The code comment admits the design — the 3-second sleep is the only guard. But `PRAGMA busy_timeout=5000` ([settings.py:92](music_manager/settings.py#L92)) means a contended write can legitimately block **longer than 3 seconds**, so the process is killed with the job still RUNNING; boot recovery requeues it; the upgrade and restart run again. Bounded in practice, but a real loop while the contention recurs.

The `Popen` result is never checked — no `wait()`, no `returncode`. Under systemd there is no tty, so without the NOPASSWD sudoers rule `sudo` fails inside the detached shell and **the restart silently never happens while the job records SUCCESS**.

**Fix:** persist the terminal state *before* triggering the restart (`Job.objects.filter(pk=...).update(status=SUCCESS, ...)`), then run `subprocess.run(["sudo","-n","systemctl","restart","--no-block", SYSTEMD_SERVICE], check=True)` synchronously. `--no-block` returns as soon as the restart is enqueued, removing the need for the sleep entirely; `-n` plus `check=True` turns a missing sudoers rule into a loud FAILED job instead of a silent no-op.

## A8 — `.env` rewritten non-atomically from the running app · CONFIRMED · Medium

[env_file.py:34,47](playlist/utils/env_file.py#L34) is a read-modify-write with no lock, no temp-file-plus-rename, and no `fsync`; `write_text` truncates in place, so a power cut mid-save can leave `.env` empty or partial — which, given A1, means the service will not boot again. The only caller is `update_env_view` under threaded gunicorn, so two overlapping settings POSTs can interleave and lose one save. Duplicate keys compound it: `update_env` rewrites the **first** matching line while `read_env` (and `load_dotenv`) are last-line-wins, so a stale duplicate shadows the update. Values are split at `" #"` before quote-stripping and written back unquoted, so any value containing `" #"` is silently truncated.

> **Correction.** `update_env` never creates duplicate keys itself — that path needs a hand-edited `.env`. And the one secret it handles (`GEMINI_API_KEY`) can't contain `" #"`, so truncation realistically threatens `OUTPUT_DIRECTORY`/`PLAYLIST_URL`. Medium rather than high because `.env` is written only on an occasional admin save, making the window small — but the worst case (truncated `.env` on SD, unrecoverable API key, service won't start) is severe.

**Fix:** guard `update_env` with a module-level `threading.Lock` (sufficient for a single-process deployment), write to a temp file in the same directory, `flush` + `os.fsync`, then `os.replace()` over `.env`. While rewriting, replace every line matching a key rather than just the first, and stop splitting on `" #"` for quoted values (or quote on write).

## A9 — Settings form accepts values that break the service · PARTIAL · Medium

[views.py:240-244](playlist/views.py#L240) validates numeric fields with a bare `float()` — no range check, no integer check — so `0`, `-3`, and `2.5` all save. **`SSE_POLL_SECONDS=0` is the confirmed hazard:** after restart, `time.sleep(0)` inside the `while True` turns each connected client's stream into a zero-sleep loop running full-table fingerprints (A2) — a genuine per-client busy loop on the Pi.

> **Correction — three of the four claimed consequences are wrong.** A **negative** `SSE_POLL_SECONDS` does *not* busy-loop: `time.sleep(-1)` raises `ValueError`, and that line sits **outside** the generator's `try/except` (which guards only `_state_signature`), so each stream dies on its first tick and the client reconnects every 3s per `retry: 3000` — broken live updates and reconnect churn, negligible CPU. **`WORKER_THREADS='2.5'`** does brick the service, but at `int()` in [settings.py:195](music_manager/settings.py#L195) during settings import, *not* in `start_workers` — whose call is wrapped in `try/except` in `wsgi.py` and therefore could never kill the service. **`WORKER_THREADS='0'`** causes no failure at all: [worker.py:45](playlist/services/worker.py#L45) clamps with `max(1, ...)`.

**Fix:** validate per field rather than with a blanket `float()` — parse `WORKER_THREADS` with `int()` requiring ≥ 1, and require the `*_SECONDS` fields to parse as float with a sane floor (≥ 0.5 for `SSE_POLL_SECONDS`), reusing the existing `invalid` notify path. As a guard against hand-edited `.env` files, clamp in `settings.py` too: `SSE_POLL_SECONDS = max(0.5, float(...))`, `WORKER_THREADS = max(1, int(float(...)))`.

## A10 — `test_tagger.py` runs live side effects during test discovery · CONFIRMED · Medium

[test_tagger.py](test_tagger.py) calls `django.setup()` at module level with no `__main__` guard, queries the real DB for a `DOWNLOADED`/`FAILED` track, and runs `asyncio.run(service.tag_and_rename_track())` — all at import. That service does live Shazam recognition, a Gemini fallback, real `track.save()` writes, an `os.rename`, and in-place ID3 rewrites.

Django's runner defaults `test_labels` to `['.']` and discovers `test*.py` from the repo root, and `build_suite` (which imports modules) runs **before** `setup_databases`. So a bare `python manage.py test` mutates `db.sqlite3` and real files before the test database exists.

**Fix:** wrap the body in `if __name__ == "__main__":`, or better, move it out of discovery's reach entirely — a management command (`playlist/management/commands/tag_one.py`) or `scripts/manual_tag.py`.

## A11 — Stored XSS via video title in toast messages · PARTIAL · Medium

[views.py:185,198](playlist/views.py#L185) interpolate `video.title` into `_hx_notify(f"Queued download: {video.title}")`; `_hx_notify` JSON-encodes it into the `HX-Trigger` header (JSON escaping only — `<` and `>` survive); [base.html:83-87](playlist/templates/playlist/base.html#L83) then concatenates the message straight into `el.innerHTML`. Titles are stored verbatim from YouTube, so uploader-controlled HTML reaches `innerHTML` when the user clicks Download/Retry/Delete. The chain is real.

> **Correction.** A `<script>` payload does **not** work — script elements inserted via `innerHTML` are inert per the HTML spec. The working vector is event-handler HTML: `<img src=x onerror=...>` or `<svg onload=...>`. Severity is tempered by deployment: an unauthenticated single-user LAN dashboard with no credentials worth stealing. The payload gains same-origin JS whose worst use is silently driving app endpoints, but `ENV_FIELDS` excludes the path and command keys, so there is no direct escalation to RCE.

**Fix:** in `showToast`, build the toast with `createElement` and set the message via `textContent` instead of string-concatenating into `innerHTML`. Optionally also wrap the message in `django.utils.html.escape()` inside `_hx_notify`.

## A12 — `DEBUG=True` in production · PARTIAL · Medium

[settings.py:24,27,29](music_manager/settings.py#L24) hardcode `SECRET_KEY` (`django-insecure-…`), `DEBUG = True`, and `ALLOWED_HOSTS = ["*"]`. With DEBUG on, every connection wraps its cursor in a debug cursor and appends each query to `queries_log`; `reset_queries`/`close_old_connections` fire only on request signals, which never fire mid-stream, and `connections` is thread-local — so the SSE thread's log does accumulate for the life of the stream.

> **Correction.** The "unbounded memory growth / OOM on a 1GB Pi" framing is **wrong**. Since Django 1.8 `queries_log` is a `deque(maxlen=9000)`. At 3 queries per 5s tick it fills in roughly 4 hours and then plateaus at a few MB per connection — bounded and modest, not an OOM path. The real cost of `DEBUG=True` here is the verbose error pages (source, paths, settings — though `SafeExceptionReporterFilter` does mask `SECRET_KEY` and `GEMINI_API_KEY`) plus a small constant CPU/memory tax. The committed `SECRET_KEY` and `ALLOWED_HOSTS=['*']` remain genuine hardening issues in their own right.

**Fix:** read both from the environment — `DEBUG = os.getenv("DJANGO_DEBUG","0") == "1"`, `SECRET_KEY` from env with no insecure default — and pin `ALLOWED_HOSTS` to the Pi's hostname/IP. Turning DEBUG off disables the debug cursor entirely, which is the whole fix.

## A13 — Private videos misclassified as UNAVAILABLE · PARTIAL · Low

[youtube_service.py:66](playlist/services/youtube_service.py#L66) compares `title == '[Private Video]'` (capital V) while line 68 correctly matches `'[Deleted video]'` (lowercase) — the file is internally inconsistent. Private entries have `duration=None`, so they fall through to UNAVAILABLE. The repo's own fixture at [test_services.py:31](playlist/tests/test_services.py#L31) bakes in the same wrong casing, which is why tests pass.

> **Correction.** The string `'[Private video]'` does **not** exist in installed yt-dlp in any casing — it is YouTube InnerTube data passed through verbatim, so it cannot be confirmed from `.venv` as originally assumed. yt-dlp instead exposes a structured `availability: 'private'` field on flat entries, which is the robust thing to key on. Impact is **cosmetic only**: download eligibility gates on `AVAILABLE`, so PRIVATE and UNAVAILABLE behave identically — the only difference is the dashboard label and pill count.

**Fix:** `if entry.get("availability") == "private" or (title or "").casefold() == "[private video]":` → PRIVATE, and correct the test fixture to the real-world `[Private video]`.

## A14 — `None` duration raises in the Gemini fallback · PARTIAL · Low

[metadata_parser_service.py:77](playlist/services/metadata_parser_service.py#L77) does `if video.duration < 500:` with no `None` guard, while `duration` is nullable and stored as `None` for deleted/private videos. `None < 500` raises `TypeError`.

> **Correction.** The claim that the error is silently swallowed is wrong. The `try` block starts at line 115, **after** the comparison, so the file's own `except Exception` never sees it. The `TypeError` propagates to [tagger_service.py:100](playlist/services/tagger_service.py#L100), whose broad `except` calls `logger.exception` (full traceback) and `_mark_as_failed` — logged and retried, not lost. Reachability is narrow too: downloads are enqueued only for AVAILABLE videos, so a `None` duration at tagging time requires the video to be deleted or privated between download and a later TAG_ALL pass, *plus* a Shazam miss. Practical cost is a small set of tracks deterministically re-failing each TAG_ALL run — including 3 Shazam attempts with 5s sleeps each time.

Two sub-claims confirmed as real dead code: `def init(self)` at line 66 is a typo for `__init__` and never runs, and the `<500` and `500-800` branches both assign `gemini-flash-latest` — only `800-3000` differs.

**Fix:** normalize once (`duration = video.duration or 0`) or, better, route `None` to the title-only path, since a `None` duration means the video is unavailable and the `file_uri` upload would fail anyway. Rename or delete `init`, and collapse the redundant branches.

## A15 — Deprecated `force_generic_extractor` flag · PARTIAL · Low

[youtube_service.py:25](playlist/services/youtube_service.py#L25) sets `'force_generic_extractor': True` in `ydl_opts`.

> **Correction — the opposite of the concern is true.** The generic extractor is **not** being forced. In installed yt-dlp the params-level flag is read only inside `YoutubeDL.download()` and for `additional_urls`; `extract_info()` honors only its own keyword argument, which this code never passes. So the real YouTube tab extractor runs and returns proper flat entries with ids. The code works today not through a fragile quirk but because **the flag is dead code on this call path**, and it is documented as deprecated upstream.

The latent risk is small but real: the flag is misleading to readers, and if a future yt-dlp honored it — or the call path changed to `download()` — extraction would break, and the broad `except` would turn that into a silent empty sync rather than a crash.

**Fix:** delete the line. Optionally change `'extract_flat': True` to `'extract_flat': 'in_playlist'`.

---

## Suggested order of work

1. **A1** — commit a working systemd unit; without it the repo cannot reproduce production.
2. **A3 + A4 together** — one bug with two locks on it; fixing either alone leaves tracks stranded.
3. **A2** — the everyday failure: the UI stops responding with a few tabs open, and it is also the biggest steady-state CPU/IO cost on the Pi.
4. **A6 (timeouts) + A7 + A8** — cheap, self-contained robustness fixes to the paths that can wedge or brick the service.
5. **A5** — needs the per-video lock; do it when touching the job layer.
6. **A12 + A11 + A9** — hardening, sensible as one pass.
7. **A10, A13, A14, A15** — small, independent, safe to batch.
