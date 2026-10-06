# music-dupes

Self-hosted duplicate-music finder, shipped as a Docker image. README.md is the user-facing install guide and explains every decision rule; the code is `app/app.py` (FastAPI, single file) and `app/index.html` (vanilla JS, no build).

## Safety invariants (never weaken)
1. Never remove every copy of a track. Enforced in the UI and again server-side in `/api/quarantine`.
2. Nothing is deleted except by an explicit per-batch "Delete permanently" with a confirm. Everything else is a reversible rename into the quarantine folder.
3. Every file operation goes through `library_path()`: inside the library, outside quarantine, no symlinks anywhere in the path (realpath must equal the path). `/api/audio` uses it too. Batch IDs are validated by regex.
4. Only paths from the current scan results can be quarantined or upgraded; client-supplied paths are never trusted.
5. Review (manual) clusters and "Keep both" clusters never pre-select anything.
6. Restore never overwrites; an occupied path leaves the file in quarantine with an error.
7. Quarantine and upgrade run under `MLOCK` and re-check the disk: moved files must match the scan, and an unchanged copy must survive. The batch is recorded in `quarantine.json` before anything moves.
8. `quarantine.json` is never reset when unreadable; quarantine, restore and purge refuse to run instead.
9. No auth. The request guard (Host allowlist + `ALLOWED_HOSTS`, JSON-only POSTs, no cross-site requests) keeps other websites from driving the API through the browser. Keep it in front of every route.

## Owner preferences
- Lossless first: hi-res beats CD quality, lossless beats lossy. 44.1 and 48 kHz at the same bit depth count as equal (`qclass`).
- Different release years: keep both unless raw data proves the same audio; then suggest which to remove.
- Deluxe/expanded over standard; a complete album over a partial copy of it.
- Remastered over regular, but only as a tiebreak (`PREFER_REMASTERS`, after quality, completeness, source and untouched tags): remasters aren't reliably better. Different masters of one recording get a "Different master" chip.
- The same recording on different albums (album vs single, compilation, best-of) is not a duplicate: both stay ("Other albums", nothing selected). Edition rules only run when every edition is the same album (`same_album`).
- Different album artists, or anything the app can't confirm: Review, nothing selected, with diffs, raw tags and the player.
- Within a folder, keep the copy that fits the folder (naming, download batch, filename agrees with its tags).
- Show which pipeline each file came from (Lidarr history, Tidarr container tags, user rules).
- Acoustic fingerprints (fpcalc, full length, files in duplicate groups only) confirm or veto a group's match; they never pick the keeper or override different album artists / different albums. Clean vs explicit (advisory tag or "(Clean)") always goes to Review. `FINGERPRINT=report` is the default until it's tuned on real libraries.
- Prefer Tidal/Tidarr metadata over Lidarr/MusicBrainz retags (`PREFER_SOURCES=Tidarr`, untouched tags beat retagged). Newer isn't better on its own; a filename that matches its title and an ISRC come first.
- One-click cleanup, always reversible first.

## Hard rules
- Keep the app runnable from just app/app.py and app/index.html: no frontend build step, no new runtime deps without asking. deploy/compose.no-build.yaml depends on this.
- Pinned deps live in requirements.txt: fastapi==0.115.6 uvicorn==0.34.0 mutagen==1.47.0. Python 3.12. Keep the pins in deploy/compose.no-build.yaml in sync.
- Never use FLAC bitrate as a quality signal (it's only used as an integrity check for truncated files).
- Grouping (`group_keys`/`group_files`) only finds candidates; confirmation and keeper choice happen in `row_evidence`, `classify` and `keep_rank`. Folder fit never confirms a duplicate.
- Bump the tag cache table name (files_v2 -> files_v3) whenever the cached dict shape changes. Fingerprints have their own table (prints_v1); bump that when their format changes.
- No secrets in the repo; placeholders only. TrueNAS Custom Apps don't read .env, so deploy/truenas.yaml inlines placeholder values.
- The image must run as any UID (users set `user:` to match their library owner). Don't write anywhere but /config, /music and /tmp.
- Frontend API calls use relative paths (`api/...`) so sub-path reverse proxies work, and every POST sends `Content-Type: application/json`.

## Conventions
- UI copy: sentence case, plain verbs, no em dashes, no emojis, no all-caps labels, no middle-dot separators.
- Keep the quality chip as the single loud visual element; light and dark themes via CSS variables.
- Every destructive action gets an Undo toast where possible.

## Testing
- `pytest` (needs ffmpeg; fingerprint tests also need fpcalc from libchromaprint-tools). tests/mkfix.py builds the fixture library; expected results are `EXPECTED` in tests/test_classify.py. tests/test_safety.py covers each safety fix.
- tests/e2e.py is a Playwright flow against a running server on a fresh fixture library. Check light and dark, desktop and 390px wide.
