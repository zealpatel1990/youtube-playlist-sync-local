# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Hard rules

**Git — never write, only read.** Do not run any command that mutates the index, working tree, refs, history, stashes, or remotes: `add`, `commit`, `checkout`, `switch`, `restore`, `rebase`, `reset`, `revert`, `merge`, `cherry-pick`, `pull`, `push`, `stash`, `rm`, `mv`, `clean`, `branch -d/-m`, `tag -a/-d`, `config`, `worktree`, `submodule`. The user owns version control. Read-only git is encouraged: `status`, `log`, `diff`, `show`, `blame`, `branch` (listing), `reflog`, `ls-files`, `rev-parse`, `stash list`.

This is a **behavioral rule, not just a config rule.** [.claude/settings.json](.claude/settings.json) denies these patterns as a backstop, but pattern matching cannot catch every form (`git -C <path> commit`, aliases, commands composed inside `sh -c`, a subcommand not yet enumerated). Follow the rule because it is the rule. When a task seems to need a git write, hand it to the user: *"these files are staged — run `git restore --staged <paths>` yourself."*

The session runs in `permissions.defaultMode: "auto"`, where a classifier auto-approves commands it judges generically safe. It knows nothing about this project's rules, so the deny list plus this instruction are the only things encoding them.

**Delivery target is a Raspberry Pi 2.** 4×900MHz Cortex-A7 (32-bit **ARMv7**), 1GB RAM shared with the OS, slow SD/USB IO. The app runs there 24/7 as a systemd service. Development is on a Windows laptop — judge every change by idle CPU, RAM, and IO on the Pi.

**Idle cost is the primary design constraint.** This app is doing nothing most of the time, so "what does this cost when nothing is happening" outranks throughput. Two invariants encode that, and breaking either silently undoes the whole rewrite:

- **Never add a polling loop.** Workers block on `threading.Event` ([music/jobs/worker.py](music/jobs/worker.py)); `enqueue()` wakes them. `WORKER_IDLE_WAKE_SECONDS` is a safety net for out-of-process work, not a poll interval.
- **Never query the database from the SSE path.** Live updates compare an in-memory revision counter ([music/core/events.py](music/core/events.py)). Any code that changes user-visible state calls `events.bump(topic)`. An idle dashboard must cost zero queries per tick regardless of tab count.

**Single process, enforced.** The event wakeup, the revision counter, the keyed locks, and requeue-orphans-at-boot are all in-process and are only correct with `gunicorn --workers 1`. [music/core/runtime.py](music/core/runtime.py) takes a lock file at startup to make that checkable. Never add `--preload` — threads do not survive gunicorn's fork.

**"Less connecting, more proper code."** Stability and deliberate error handling over features. No new processes, brokers, or dependencies where direct code will do. Before adding any dependency, check it has an **armv7** wheel on piwheels — not just aarch64 on PyPI. Two current dependencies carry compiled extensions (`pydantic-core` via google-genai, and `websockets`), so the Pi needs Python 3.11 with piwheels configured or it will try to compile Rust on a 900MHz core.

**yt-dlp is the only dependency the app upgrades itself**, from the dashboard or on the `YTDLP_AUTO_UPDATE_HOURS` timer, because it is the only one that rots on someone else's schedule — YouTube changes and downloads stop. Everything else is pinned in `requirements.txt` and moves when a human decides. Don't add unattended upgrades for other packages: bouncing a 24/7 service to install a tagging-library change nobody asked for is a bad trade.

**Gemini is free-tier with a very low quota.** It is the last resort in the identification chain, behind a rate limiter *and* a hard `DailyBudget`. Never move it earlier or call it in a loop.

## Commands

Settings require `DJANGO_SECRET_KEY` and `LIBRARY_ROOT`; copy `.env.example` to `.env` first. Missing config raises `ImproperlyConfigured` with a message saying what to fix, not a bare `KeyError`.

```bash
python manage.py runserver 0.0.0.0:8000
python manage.py migrate
python manage.py collectstatic --noinput

# Tests — ALWAYS scope to the app:
python manage.py test music
python manage.py test music.tests.test_plex
python manage.py test music.tests.test_jobs.JobEngineTests.test_name

# Pipeline
python manage.py scan_library                 # discover audio files
python manage.py organize                     # dry run: print the planned moves
python manage.py organize --apply             # perform them
python manage.py sync_youtube
python manage.py identify_track --path FILE   # one file, prints the result
python manage.py migrate_legacy --apply       # import rows from the old schema

# ARM test image — linux/arm/v7, matching the Pi 2 (NOT arm64):
docker buildx build --platform linux/arm/v7 -t music-manager:pi --load .
```

`manage.py` sets `MUSIC_MANAGER_DISABLE_WORKERS=1`, so a management command never starts a second worker pool competing with the live service.

## Architecture

Full rationale in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The shape:

**`Track` is the root object — one audio file on disk.** A disk scan and a YouTube download both produce Tracks, so identification, tagging and organization are written once and both sources get them. `YoutubeVideo` is a thin source record pointing at a `Track`. This is why organizing pre-existing MP3s fits at all; the previous schema rooted everything in a `Video` and files that never came from YouTube had nowhere to live.

**Pipeline:** `scan`/`download` → `identify` → `tag` → `plan` → *user reviews* → `apply`.

**Job engine** ([music/jobs/](music/jobs/)): rows in `Job`, durable across restarts, but woken by an in-process event. Claims carry a **lease**; the scheduler's reaper reclaims expired ones, so a wedged handler self-heals instead of blocking the queue until someone restarts the service. Dedup is a **partial unique index** on `dedup_key` over active states, so it is a database guarantee rather than a check-then-create race. Handlers register with the `@job(...)` decorator in [music/jobs/registry.py](music/jobs/registry.py) — adding a kind never touches a central dispatch table.

**Identification chain** ([music/identify/](music/identify/)): `tags` → `acoustid` → `shazam` → `gemini`, ordered cheapest first, each returning `TrackMetadata` with a confidence score or `None` to pass. AcoustID does the bulk work because it is the only provider returning track *and disc* numbers, which the Plex layout needs. Every provider is lazily imported, so a missing optional dependency disables one provider rather than breaking app import.

**Plex layout** ([music/plex.py](music/plex.py)): pure functions, no IO, no ORM, no Django import — the policy deciding where thousands of real files land is exhaustively unit-testable. `<Album Artist>/<Album>/<NN> - <Title>.<ext>`, disc number prepended only when ≥ 2, `Various Artists` for compilations.

**Organization is non-destructive.** Planning sets `planned_path`; applying is a separate explicit action that records `previous_path` so it can be reverted. `AUTO_ORGANIZE` defaults off. Nothing is ever deleted — duplicates are resolved by `DUPLICATE_POLICY`, defaulting to `report-only`.

## Conventions

- Business logic in `music/{identify,library,ingest}/`; job handlers in `music/jobs/handlers/` are thin glue; views only enqueue.
- **Every DB write uses `save(update_fields=[...])`.** A bare `save()` on a row a concurrent job deleted silently re-INSERTs it, because the PK is still set.
- Work touching one track takes `track_locks.acquire(f"track:{pk}")` from [music/core/locks.py](music/core/locks.py).
- Every network call and subprocess takes an explicit timeout. No exceptions.
- Unknown numbers are `0`, never `NULL` — `duration`, `track_no`, `disc_no` are compared against thresholds throughout, and a `None` reaching a comparison raises at a distance.
- Failures go through one path: `Track.mark_failed()` sets `FAILED` *and* `retry_at` together, so a failed track can never be invisible to the retry query.
- Long handlers call `engine.heartbeat(job)` to extend their lease.
- Optional dependencies are imported lazily inside functions.
- htmx fragments are underscore-prefixed partials; assets vendored under `music/static/vendor/` (no CDN, no build step).

## Cutover status

The old `playlist/` app is still on disk but **unwired from `INSTALLED_APPS`** — nothing of it runs. It stays for one release so it can be diffed and rolled back; delete it once the new service has run clean. `python manage.py migrate_legacy` imports its rows.

`test_tagger.py` at the repo root is a leftover from the old app and should be deleted — it is replaced by `manage.py identify_track`.

## Project docs

- [Readme.md](Readme.md) — user-facing setup and usage.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — design rationale and the audit-fix map.
- [docs/CODE-AUDIT.md](docs/CODE-AUDIT.md) — the verified defect register of the previous version; every entry is a mistake not to reintroduce.
- [docs/MUSIC-LIBRARY-MERGE.md](docs/MUSIC-LIBRARY-MERGE.md) — the library merge plan and open decisions.
