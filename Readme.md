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

Each source is asked in turn, and the first answer that survives the checks below wins:

| Order | Source | Notes |
|---|---|---|
| 1 | **AcoustID** | Free API key. Fingerprints the audio with `fpcalc`, resolves via MusicBrainz. The only source that returns **track and disc numbers**, which the Plex layout is built from — which is why it goes first even though it matches less often than Shazam. |
| 2 | Shazam | Matches far more of a non-Western catalogue, but returns no track number. |
| 3 | Gemini | Reads the *title*, not the audio, so it names things no fingerprint can — regional, devotional and older recordings that are in neither commercial index. Rate-limited *and* capped by a hard daily budget, because the free tier's quota is small. |
| 4 | Existing tags | Last, not first: a file's own tags are believed only when nothing else could identify it, so a mis-tagged file is not simply rubber-stamped. |

Each provider can be switched off. The chain is `IDENTIFY_CHAIN` in `.env`.

**An answer has to agree with the file.** A result is rejected — and the next
source asked — when it looks like a cover of the track rather than the track,
when it shares no word with the upload's title, or when the title names one
artist and the answer credits another. That last check exists because a
fingerprint match can be confidently wrong: a Billie Eilish download came back
as a Sons of Serendip cover at 0.98, because that is the only recording
MusicBrainz has linked to those fingerprints. If every source is rejected, the
best of them is kept anyway and flagged for review — an answer they all agreed
on beats no answer at all.

**To force one source**, long-press (or right-click) a track's Identify button
and pick it. Useful when you can see the chain has settled on the wrong answer.

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

## Testing against a simulated Pi

The app is developed on a laptop and deployed to 32-bit ARMv7, which is a
genuinely different platform — not just a slower one. `docker-compose.yml`
builds and runs the real thing under emulation:

```bash
docker compose up --build       # first build is slow: everything runs under QEMU
```

Then open <http://localhost:8000/>.

This reproduces three things a native run cannot:

- **The architecture.** ARMv7, so armv7 wheel availability is exercised for
  real. Two dependencies carry native code (`pydantic-core`'s Rust core, and
  `websockets`); if piwheels lacks a wheel for the Pi's Python version, the
  build discovers it here rather than on the Pi.
- **The memory ceiling.** 1 GB with no swap, as on the hardware. An OOM in the
  container is an OOM on the Pi.
- **The external binaries.** `ffmpeg` and `fpcalc` are installed, so downloads
  and AcoustID fingerprinting run end to end instead of failing at the first
  missing executable.

Your `GEMINI_API_KEY`, `ACOUSTID_API_KEY` and `PLAYLIST_URL` pass through from
the shell; an unset key simply skips that provider.

```bash
docker compose logs -f                                   # follow it
docker compose exec app sh                               # a shell inside
docker compose run --rm app python manage.py test music  # the suite, on ARM
docker compose down                                      # stop
```

## The local sandbox

`_devdata/` mirrors the Pi's layout so local testing and the real thing differ
only in paths. It is gitignored, and `scripts/devdata.py` rebuilds it:

```bash
python scripts/devdata.py seed            # create the tree and sample tracks
python scripts/devdata.py status          # what is in there now
python scripts/devdata.py clean-staging   # drop half-finished downloads
python scripts/devdata.py reset --yes     # wipe it and start over
```

| Sandbox | Stands in for |
|---|---|
| `_devdata/music/` | `/media/pi/MUSIC/Music` |
| `_devdata/youtube_music/` | `/media/pi/MUSIC/Youtube_Music` |
| `_devdata/library/` | `LIBRARY_ROOT` |
| `_devdata/staging/` | `DOWNLOAD_STAGING` |

The sample tracks are synthesised MPEG frames, not copyrighted audio, and cover
the cases that actually break things: a flat dump with no album folders, a
multi-disc set, a compilation with per-track performers, an untagged file, a
title full of illegal characters, and a byte-identical duplicate.

## Tests

```bash
python manage.py test music
```

Always scope to `music`. A bare `manage.py test` discovers stray `test*.py`
files at the repo root.
