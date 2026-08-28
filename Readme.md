# Music Manager

A small Django app for a Raspberry Pi that keeps a music library in order.

It does two things, through one pipeline:

1. **Mirrors a YouTube playlist** — tracks each video, downloads the audio with yt-dlp.
2. **Organizes your existing music** — scans the folders you already have and files everything into the layout a Plex music library expects.

Both paths converge on the same object — an audio file on disk — so identification, tagging and organization are written once and serve both.

```
YouTube playlist ──download──┐
                             ├──► Track ──► identify ──► tag ──► organize
Existing folders ──────scan──┘
```

## What "organized" means

Files are laid out the way [Plex documents](https://support.plex.tv/articles/200265296-adding-music-media-from-folders/):

```
Music/
  Radiohead/
    OK Computer/
      01 - Airbag.mp3
      02 - Paranoid Android.mp3
  Various Artists/
    Now That's What I Call Music 42/
      01 - Some Song.mp3
```

Multi-disc albums prepend the disc number to the track number, so disc 3 track 2 becomes `302 - Track Name.mp3`. Compilations go under `Various Artists` with the real performer kept in each track's `artist` tag. The tags themselves are corrected too, because Plex reads tags over filenames.

**Nothing moves until you say so.** A scan computes a plan; you review the whole manifest at `/review/`; applying it is a separate, explicit action that records where every file came from, so it can be reverted. No file is ever deleted.

## How tracks get identified

Cheapest source first, so the expensive and rate-limited ones only see what the cheap ones could not resolve:

| Order | Source | Notes |
|---|---|---|
| 1 | Existing tags | Free. Most of a well-kept library stops here. |
| 2 | **AcoustID** | Free API key. Fingerprints audio with `fpcalc`, resolves via MusicBrainz. Does the bulk of the work, and is the only source that returns track and disc numbers — which the Plex layout needs. |
| 3 | Shazam | For rips, remixes and YouTube-only audio that AcoustID cannot match. |
| 4 | Gemini | Last resort, inference from the title. Rate-limited *and* capped by a hard daily budget, because the free tier's quota is small. |

Each provider can be switched off. The chain is `IDENTIFY_CHAIN` in `.env`.

## Why it stays quiet on a Pi

The app is designed around what it costs when nothing is happening, because that is most of the time:

- **Workers block on an event, not on the database.** Enqueuing work wakes them instantly; an idle system runs no queries at all.
- **The dashboard's live updates compare an in-memory counter**, not the database. An idle dashboard costs nothing per tick no matter how many tabs are open, and one change wakes them all at once.
- Hashing reads in 1 MiB blocks, tags are written in a single pass, and a rescan of unchanged files does no IO beyond `stat`.

Everything runs in **one process** — no Redis, no Celery, no second service. That is enforced at startup with a lock file rather than left to a comment.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# At minimum set DJANGO_SECRET_KEY and LIBRARY_ROOT.
# Generate a key:  python -c "import secrets; print(secrets.token_urlsafe(50))"

python manage.py migrate
python manage.py collectstatic --noinput
```

For AcoustID identification you also need the fingerprinting binary and a free key from [acoustid.org](https://acoustid.org/new-application):

```bash
sudo apt install libchromaprint-tools ffmpeg   # provides fpcalc and ffmpeg
```

## Run

**Development**

```bash
python manage.py runserver 0.0.0.0:8000
```

**Production (Raspberry Pi, systemd)**

```bash
sudo cp deploy/music_manager.service /etc/systemd/system/
sudo visudo -f /etc/sudoers.d/music-manager   # paste deploy/sudoers-music-manager
sudo systemctl daemon-reload
sudo systemctl enable --now music_manager
```

Keep `--workers 1`. Background concurrency is `WORKER_THREADS` in `.env`; on a Pi 2, leave it at 1 or 2, since one ffmpeg transcode already saturates a core. Never add `--preload` — worker threads do not survive gunicorn's fork.

## Command line

```bash
python manage.py scan_library                 # find audio files
python manage.py organize                     # dry run: print the planned moves
python manage.py organize --apply             # perform them
python manage.py sync_youtube                 # refresh the playlist, queue downloads
python manage.py identify_track --path FILE   # identify one file, print the result
python manage.py migrate_legacy --apply       # import rows from the previous version
```

## Tests

```bash
python manage.py test music
```

Always scope to `music`. A bare `manage.py test` discovers stray `test*.py` files at the repo root.
