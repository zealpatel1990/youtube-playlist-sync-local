# Music Library Merge Plan

**Goal:** merge the two MP3 collections on the Pi's USB HDD into one library organized to Plex conventions, with correct embedded tags, identified by audio fingerprinting.

**Sources** (both on the same HDD, so moves stay within one filesystem):
- `/media/pi/500gb hdd/Music` — the existing collection
- `/media/pi/500gb hdd/Youtube_Music` — yt-dlp output, including this app's **live** sync directory `Youtube_Music/playlist_sync_django` (`OUTPUT_DIRECTORY` in `.env`)

## Target layout

Per the [Plex music naming guide](https://support.plex.tv/articles/200265296-adding-music-media-from-folders/):

```
Music/
  Artist Name/
    Album Name/
      01 - Track Name.mp3
      02 - Track Name.mp3
  Various Artists/
    Compilation Album/
      01 - Track Name.mp3
```

- Track files: `TrackNumber - TrackName.ext`.
- **Multi-disc:** prepend the disc number to the track number — disc 3 track 2 becomes `302 - Track Name.mp3`. The disc number must *also* be set in the embedded tags.
- **Compilations:** a folder literally named `Various Artists`; embedded **Album Artist** = `Various Artists`, **Artist** = the actual performer for each track.
- Plex matches on ID3 tags plus sonic fingerprinting, and strongly encourages albums-in-folders over flat file lists even when tags are complete. **Accurate embedded tags matter more than filenames.**

The app's current output does **not** match this: [tagger_service.py](../playlist/services/tagger_service.py) writes `Artist - Title.mp3` flat in one directory, with no album folder, no track number, and no disc number. Closing that gap is decision 2 below.

## Identification strategy — ordered by cost

| Tier | Source | Cost | Use for |
|------|--------|------|---------|
| 1 | Existing embedded tags | Free, local | Files already carrying artist + album + track — validate, don't re-identify |
| 2 | **AcoustID** (Chromaprint `fpcalc` → MusicBrainz) | Free key, ~3 req/sec guideline | Bulk identification of the whole library — the purpose-built tool for exactly this |
| 3 | Shazam (shazamio) | Unofficial API; fingerprinting is CPU-heavy on ARMv7 | Files AcoustID can't match: rips, remixes, YouTube-only audio |
| 4 | Gemini | Free tier, **very low rate limit** | Last resort — metadata inference from title/context when audio matching fails |

AcoustID is the key addition. It needs `pyacoustid`, the `fpcalc` binary (`libchromaprint-tools` in the Raspberry Pi OS repos), and a free API key from acoustid.org. MusicBrainz lookups return canonical artist, album, track number and disc number — precisely the fields the Plex layout requires and the fields Shazam does not reliably give you.

Ordering matters for more than politeness: Shazam and Gemini are both rate- and CPU-constrained, so a full-library pass driven by either would take days on the Pi. AcoustID does the bulk, and the expensive tiers handle only the remainder.

## Constraints

- **Streaming, not batch-in-memory.** Iterate files one at a time; never materialize the library listing. Fingerprint one file at a time.
- **Resumable and idempotent.** Persist per-file state (a SQLite table keyed by path + md5 is the natural fit, reusing the existing DB) so a power cut or restart resumes rather than restarts. Never re-fingerprint a file already identified.
- **Non-destructive by default.** A dry-run mode that emits the complete move/retag manifest for review; moves happen only on explicit apply. Record the original path for every file so the whole operation can be rolled back.
- **Don't fight the live service.** `playlist_sync_django` is actively written by the running app. Either pause the service for a pass over that directory, or skip any file whose `LocalTrack` row is not `COMPLETED`. Note also audit finding A5 — concurrent writers to the same track already race in the existing code, so adding a third writer needs the per-video lock in place first.
- **Rate limits and timeouts.** AcoustID ~3 req/sec; Gemini batched and cached; every network call gets an explicit timeout (see audit A6 — the existing cover-art fetch has none, and that is the most likely hang on Pi networking).
- Moves are `os.rename` within the one filesystem; if that ever stops being true, copy → verify hash → delete.

## Decisions — resolved by the rewrite

Two of the four open questions were settled by building this into the app rather than beside it:

- **The app writes Plex-convention paths going forward.** Every track, from either source, flows through the same identify → tag → organize pipeline, so the library does not drift back out of convention with each sync. The merge is genuinely one-time.
- **The tool lives inside the app** — `music/library/` plus `manage.py scan_library` / `manage.py organize`, reusing the models, the job queue with its leases and retries, and the identification chain. No second process, no duplicated logic.

Two remain configurable rather than decided, because they are yours to choose per run:

- **Merged root** is `LIBRARY_ROOT` in `.env`. Pointing it at `/media/pi/500gb hdd/Music` absorbs everything in place; pointing it at a fresh `Plex_Music/` builds the new tree while leaving both sources untouched until you have verified it. The fresh root is safer and costs disk headroom — worth it for the first run.
- **Duplicate policy** is `DUPLICATE_POLICY`, defaulting to `report-only` (find them, change nothing). `keep-best` keeps the highest bitrate and moves the rest to `LIBRARY_ROOT/.duplicates/`; `keep-both` disambiguates with a suffix. Nothing is ever deleted under any policy.

## Suggested first run

```bash
python manage.py scan_library     # discover everything; no files are touched
python manage.py organize         # dry run: prints every planned move
```

Then read the manifest — in the terminal, or at `/review/` in the dashboard, which shows source → destination per track. Spot-check a few albums, especially compilations and anything multi-disc. Only then:

```bash
python manage.py organize --apply
```

Every move records `previous_path`, so an individual track can be reverted from the dashboard and a bad batch is recoverable.
