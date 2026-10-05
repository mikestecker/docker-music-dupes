# music-dupes: project handoff

A self-hosted web utility that finds duplicate music files in a TrueNAS library, explains *why* they look like duplicates using raw tag and stream data, and lets the user quarantine redundant copies in one click (with undo). This document is the complete context for continuing development in Claude Code: environment, architecture, every decision rule, the owner's stated preferences, test tooling, known gaps, and the full source.

Suggested first step in Claude Code: create the repo layout in [Recommended repo layout](#recommended-repo-layout), paste the source files from the appendix, and save the "Working agreements" section as `CLAUDE.md`.

---

## 1. Why this exists

The owner (Mike, a staff frontend engineer comfortable with React/TypeScript) runs a TrueNAS SCALE media server. Music arrives from several pipelines:

- **Tidarr** (Tidal downloader, uses tiddl under the hood). The main source of duplicates: it re-downloads albums when Tidal lists a reissue, deluxe edition, or a release with slightly different capitalization or punctuation, and it doesn't recognize the existing folder.
- **Lidarr** with **slskd** (via the Tubifarry plugin), **SABnzbd** and **qBittorrent** as download clients.
- Some older Apple Music / iTunes purchases (AAC `.m4a`).

Real duplicate patterns observed in the owner's library (from screenshots during development):

| Pattern | Example | Desired handling |
|---|---|---|
| Same album, different release year | Blindside *A Thought Crushed My Mind* (2000) vs (2007) | Keep both unless raw data proves same audio; if proven, suggest removing one |
| Standard vs deluxe/expanded | Kutless *Hearts of the Innocent* vs *(Special Edition)*; Flyleaf *Memento Mori* vs *(Expanded)* | Suggest keeping deluxe |
| Hi-res vs CD of same release | Chris Renzema *Manna* (2023, 24/48) vs (2024, 16/44.1) | Compare tags first (year differs); if same recording, keep hi-res |
| Different album artist | Kings Kaleidoscope vs Kings Kaleidoscope Hymns | Always manual, nothing selected |
| Capitalization / punctuation / typo variants | *I Know a Ghost* vs *I Know A Ghost*; curly vs straight apostrophe; *Fractured* vs *Fractioned Heart* | Show diffs; suggest only if proven same audio |
| Possibly different recordings | Lynyrd Skynyrd *Free Bird* 9:07 FLAC vs 10:07 AAC; Josh Garrels 2011 vs 2024 (1-2s drift) | Manual with inline player, nothing selected |
| Same folder, two copies | FLAC next to AAC; `Song.flac` and `Song (1).flac` | Suggest removing the lesser copy |

---

## 2. Environment

### Hardware / OS
- TrueNAS SCALE **25.10.7** (Docker Engine v27+). Apps deployed as **Custom Apps via "Install via YAML"** in the TrueNAS UI.
- NAS static IP: **10.0.9.101**. Separate compose projects can't resolve each other by container name; always use `http://10.0.9.101:<port>`.
- Service account **`svc_apps`, UID/GID 3000:3000**. All app containers run as this user; folders under `/mnt/ssd-pool/apps/*` are owned by it.
- **TrueNAS Custom Apps do NOT load `.env` files.** `${VAR}` substitution resolves to empty strings. Secrets must be inlined in the compose YAML.

### Storage paths
| Host path | Purpose |
|---|---|
| `/mnt/tank/media/music` | Music library (RAIDZ2 HDD pool, dataset `tank/media`, plain folders inside) |
| `/mnt/ssd-pool/apps/music-dupes/app` | This app's source (`app.py`, `index.html`), mounted read-only |
| `/mnt/ssd-pool/apps/music-dupes/config` | This app's state (tag cache, manifests, rules) |
| `/mnt/ssd-pool/apps/navidrome` | Navidrome data (only needed for the DB fallback) |

`tank/media` has **daily snapshots with ~2 week retention**, so permanently deleted files keep consuming space until snapshots expire.

### Neighbouring apps (ports on 10.0.9.101)
| App | Port | How the library looks inside its container |
|---|---|---|
| Navidrome | 4533 | `/music` (read-only) |
| Lidarr | 8686 | `/data/media/music` |
| music-dupes | **8095** | `/music` (read-write) |

Other ports already taken on the box (avoid): 222, 3000, 3003, 3010, 4533, 5030, 5031, 5055, 5800, 6246, 6767, 6881, 7474, 7476, 7878, 7879, 8080, 8081, 8265, 8266, 8686, 8989, 8990, 9090, 9696, 11011, 50300.

### Deployment model
No image build. The compose file runs stock `python:3.12-slim` as `3000:3000`, pip-installs pinned deps into `/tmp/deps` at startup (about 10s), and serves the bind-mounted source with uvicorn. Pinned deps: `fastapi==0.115.6 uvicorn==0.34.0 mutagen==1.47.0` (Starlette 0.41.x, whose `FileResponse` supports HTTP Range, which the audio player relies on).

Updating = copy new `app.py` / `index.html` into `/mnt/ssd-pool/apps/music-dupes/app/` and restart the app.

---

## 3. Architecture

```
browser (index.html, vanilla JS, no build)
   │  fetch /api/*
   ▼
FastAPI (app.py, single file)
   ├── scan thread ──► walk library OR ask Navidrome ──► read raw tags (mutagen) ──► SQLite tag cache
   │                    └► Lidarr history (optional) ──► source labels
   ├── grouping: per-track groups ──► album "clusters" (by folder set) ──► classification
   ├── quarantine: rename into /music/.dupe-quarantine/<batch>/<rel path>
   └── /api/audio: streams files (Range) for the inline player
```

Single-process, in-memory scan state (`STATE`), guarded by `LOCK`. One scan at a time. Results live in memory until the next scan; quarantine/restore mutate `moved` flags in place.

### Why these choices
- **Single files, no build step**: deployable by copying two files onto the NAS, no registry, no CI. Keep this property unless the owner opts out.
- **Quarantine instead of delete**: quarantine is an atomic rename inside the same bind mount (instant, reversible). Permanent deletion is a separate explicit action per batch. The quarantine folder lives *inside* the music mount on purpose; a second bind mount would turn renames into cross-device copies.
- **Hidden quarantine folder**: Navidrome skips dot-folders; the app also writes `.ndignore` containing `*` there for safety.
- **Tag cache keyed on (path, mtime, size)**: first scan reads every file; later scans only touch changed files. Table name is versioned (`files_v2`) so schema changes invalidate cleanly. Bump to `files_v3` if the cached dict shape changes.

---

## 4. Scan pipeline in detail

### 4.1 Match modes (`mode`)
| Mode | UI label | Group key | Notes |
|---|---|---|---|
| `loose` (default) | Artist and title | `(norm(artist), norm(title))` | Includes same-folder dupes. Recommended. |
| `same-folder` | Same folder only | `(folder, disc, track, norm(title or filename minus " (n)"))` | |
| `cross-folder` | Same album tags | `(norm(albumartist or artist), norm(album), disc, track, norm(title))` | Groups must span 2+ folders. Misses editions whose album names differ. |
| `navidrome` | Navidrome albums | `(album_id, disc, track, norm(title))` from Navidrome | Only grouped files get their tags read. |

`norm()` = NFKC, casefold, curly quotes to straight, non-word runs to single spaces. Untagged files are skipped in cross-folder modes (too ambiguous).

### 4.2 Raw tag reading (`read_file`)
Uses `mutagen.File(path)` (not easy mode) and normalizes every text tag into `{lowercase name: value}`:
- **Vorbis comments** (FLAC/Ogg/Opus) and ASF: keys lowercased as-is; multi-values joined with `; `.
- **MP4**: known atoms mapped (`©nam`→title, `aART`→albumartist, `apID`→"itunes account", etc.); freeform `----:com.apple.iTunes:X` → `x`.
- **ID3**: frame IDs mapped (`TSRC`→isrc, `TPE2`→albumartist...); `TXXX` uses its description; `COMM` → `comment`; `UFID` → `ufid <owner>`.
- Cover art excluded from tags, recorded as `art: bool`. Values truncated at 300 chars (lyrics).

Stream data captured: `ext, codec, lossless, bits, rate, kbps, channels, secs (3dp), samples, md5, vendor, art`. Derived: `title, artist, albumartist, album, date, track, disc, isrc (uppercase alnum)`.

**FLAC `md5`** is the STREAMINFO MD5 of the *decoded* audio. Equal MD5 = bit-identical audio regardless of tags or container. This is the strongest duplicate signal.

### 4.3 Quality scoring
```python
score(f) = (1, bits, rate, 0) if lossless else (0, 0, 0, kbps)
```
FLAC bitrate is deliberately ignored (it only reflects compressibility). An earlier version used it as a tiebreak and produced false "Lower quality" labels between identical-format FLACs; don't reintroduce that. ALAC detection: `codec == "alac"` (mutagen) or, for Navidrome-sourced metadata with no codec, m4a with bitrate >= 500 kbps.

Tiers for the UI chip colour: `hires` (lossless and >16-bit or >48 kHz), `lossless`, `lossy`.

### 4.4 Per-track evidence (`row_evidence`)
For each group of 2+ copies of one track:
- `identical`: every copy has an MD5 and they're all equal.
- `same_isrc` / `isrc_conflict`: all copies have ISRCs and they're equal / differ.
- `spread`: max minus min duration.
- `same_slot`: same (disc, track) for all copies.
- **blocked** = `isrc_conflict or spread > LEN_TOL` (`LEN_TOL = 2.5s`).
- **confirmed** (first match wins): `identical` → "identical"; blocked → none; `same_isrc and spread <= 1.5` → "recording"; single folder and `same_slot and spread <= 1` → "slot".

Chips shown to the user (tone good / neutral / warn) with a tooltip explaining each: Identical audio, Audio differs (same lossless format, different MD5 = different master), Same ISRC, ISRC differs, ISRC on some copies only, Same length, Length off by Xs.

### 4.5 Album clusters
Track groups are bucketed by the sorted tuple of folders they span. Each bucket is one **cluster** (an album pair, or one folder for same-folder dupes). Per folder an **edition** summary is computed: album, album artist, year (date tag, else `(YYYY)` in folder name), deluxe flag, audio file count in the folder, best quality, majority source, latest mtime, mean tag count.

Deluxe regex (album tag + folder name): `deluxe|expanded|special edition|super deluxe|bonus tracks?|collector'?s|anniversary|extended|complete edition`.

### 4.6 Classification (`classify`), first match wins
1. **Multiple folders with different normalized album artists** → manual, "Different album artists".
2. **Any track with ISRC conflict** → manual, "Different recordings".
3. **Any track with spread > 2.5s** → manual, "Track lengths differ".
4. **Single folder**: all tracks confirmed → suggested, "Duplicate files in one folder"; else manual, "Couldn't confirm duplicates".
5. **Some (not all) editions deluxe** → suggested, "Deluxe edition covers the standard". Keeper = deluxe edition with most tracks, then most tags.
6. **All tracks identical or same recording** → suggested, "Identical audio" or "Same recordings". Keeper ranked by: number of tracks where it holds the best quality, folder track count, mean tag count, earliest year. Detail text notes when release years differ but audio is the same.
7. Otherwise → manual, "Couldn't confirm same recordings".

**Post-check:** for suggested clusters, each track keeps the best copy *in the keeper folder* (ranked by score, then fit with the folder, no ` (n)` filename suffix, more tags, older mtime). Fit counts the other audio files in the folder, excluding the copies being compared, that share the copy's track-number prefix shape and extension (`01-03 ` → `99-99 `), plus those written within an hour of it (same download batch). A copy scoring under half the best fit in its folder is flagged `stray` and shown as "Doesn't match the folder". Fit only picks the keeper; it never confirms a duplicate. If any other copy in that track is higher quality than the kept one, the whole cluster is downgraded to manual: "Better quality in the edition we'd remove". This is what protects a hi-res standard edition from a CD-quality deluxe.

Manual clusters never pre-select anything.

### 4.7 Sources (`detect_source`), first match wins
1. **Lidarr import history** (optional, `LIDARR_URL` + `LIDARR_API_KEY`): `GET /api/v1/history?eventType=3` (trackFileImported), paged 1000, ascending, up to 200 pages. `data.importedPath` minus `LIDARR_MUSIC_ROOT` → rel path; label `"<downloadClientName or downloadClient> via Lidarr"`. Renames after import are not followed.
2. **User rules** in `/config/sources.json`: `{"rules":[{"label","tag","pattern"}]}`. `tag` = a lowercase tag name, `"path"` for the rel path, or omitted for "any tag". Case-insensitive regex.
3. Built-ins: iTunes purchase atoms → "iTunes Store"; any tag value containing "tidal" → "Tidal"; any MusicBrainz key → "MusicBrainz-tagged"; Lidarr configured → "Not from Lidarr"; else "Unknown".

**Known gap:** there is no verified Tidarr/tiddl tag fingerprint. The plan is for the owner to inspect a Tidarr file's tags in the Compare panel and add a rule. If Claude Code can inspect real Tidarr output (or tiddl's source on GitHub), adding a reliable built-in detector is a good task.

### 4.8 Navidrome integration
- **API (preferred)**: Subsonic token auth (`t = md5(password + salt)`), client name `music-dupes`, `getScanStatus` for total, then `search3` with empty query paged 500 songs. Requires the owner to enable **Settings → Players → music-dupes → Report Real Path** in Navidrome; otherwise paths are fake tag-built ones. The app errors out with instructions if fewer than half of 20+ reported paths exist on disk.
- **DB fallback** (`NAVIDROME_DB`): copies `navidrome.db` plus `-wal`/`-shm` to a temp dir and queries the copy. Handles pre-0.55 (absolute paths) and 0.55+ (relative paths, `library` table, `missing` column) schemas.
- Paths are translated with `NAVIDROME_MUSIC_ROOT` (Navidrome's view, `/music`) to the app's own mount.
- Navidrome is only used for grouping; tags are still read from files (cache makes this cheap).

---

## 5. Safety invariants (do not break)

1. **Never remove every copy of a track.** Enforced client-side (checkbox, edition select-all, card actions) *and* server-side in `/api/quarantine` (skips any track where all live copies are requested).
2. **Never delete without an explicit per-batch "Delete permanently"** with a confirm dialog. Everything else is a reversible rename.
3. **Path containment**: every file operation goes through `library_path()` (realpath must be inside `MUSIC` and outside `QDIR`). `/api/audio` uses the same check. Batch IDs validated by regex `\d{8}-\d{6}-[0-9a-f]{4}`.
4. **Quarantine only paths present in the current scan results.** Arbitrary paths from the client are ignored.
5. **Manual (Review) clusters never pre-select.** Clusters the user marked "Keep both" never pre-select.
6. Restore never overwrites: if the original path is occupied, the file stays in quarantine and an error is reported.
7. Navidrome's live DB is never opened directly; only a copy.
8. No auth exists. The app must stay LAN-only; if proxied through Nginx Proxy Manager, put an Access List in front of it. The request guard (Host allowlist, JSON-only POSTs, no cross-site requests) keeps other websites from driving the API through the browser.
9. Symlinks are never copies and nothing is moved through one (`library_path()` requires realpath == path).
10. Quarantine is serialized under `MLOCK` and re-checks the disk: every moved file must match the scan, and an unchanged copy must survive.
11. `quarantine.json` is never reset when unreadable; quarantine/restore/purge refuse to run instead.

---

## 6. HTTP API

| Method | Path | Body / params | Returns |
|---|---|---|---|
| GET | `/` | | `index.html` |
| GET | `/api/info` | | `{music, quarantine, navidrome: "api"\|"db"\|null, lidarr: bool}` |
| GET | `/api/scan` | | Full `STATE` (status, scan_id, mode, phase, scanned, total, unreadable, started, finished, error, warnings, clusters) |
| POST | `/api/scan` | `{mode}` | Starts background scan. 409 if running, 400 for bad mode / unconfigured Navidrome |
| GET | `/api/audio` | `?rel=` | File stream with correct MIME, Range supported. 400 outside library, 404 missing |
| POST | `/api/keep-both` | `{key, kept}` | Adds/removes cluster key in `kept-both.json`, updates in-memory flag |
| POST | `/api/quarantine` | `{paths: [rel]}` | `{batch, moved, bytes, errors}` |
| GET | `/api/quarantine` | | Batches newest first with surviving files |
| POST | `/api/restore` | `{batch}` | `{restored, errors}`; also un-marks `moved` in current results |
| POST | `/api/purge` | `{batch}` | `{bytes}`; permanently deletes the batch folder |

### Cluster payload shape
```json
{
  "key": "sorted rel paths joined by \\n (stable identity for Keep both)",
  "kind": "suggested | manual",
  "ignored": false,
  "reason": "Deluxe edition covers the standard",
  "detail": "Human-readable explanation",
  "artist": "...", "album": "...",
  "editions": [{
    "folder": "Artist/Album (2006)", "artist": "...", "album": "...", "year": "2006",
    "deluxe": false, "tracks": 12, "format": "FLAC", "detail": "16/44.1", "tier": "lossless",
    "source": "slskd via Lidarr", "added": 1759600000.0, "tag_count": 14.2, "keep": true
  }],
  "rows": [{
    "title": "...", "track": 3, "disc": 1,
    "evidence": [{"tone": "good", "text": "Same ISRC", "why": "..."}],
    "files": [{
      "rel": "...", "name": "03 Song.flac", "ed": 0,
      "format": "FLAC", "detail": "24/48", "tier": "hires",
      "secs": 205.36, "size": 51234567, "mtime": 1759600000.0,
      "source": "...", "source_why": "...",
      "quality": "best | lower", "suggested": false, "moved": false,
      "props": {"Format": "...", "Audio MD5": "...", "...": "..."},
      "tags": {"title": "...", "isrc": "...", "...": "..."}
    }]
  }]
}
```
"Keep both" keys are the full set of rel paths, so a new duplicate appearing later changes the key and the album resurfaces. This is intentional.

### Files in `/config`
| File | Purpose |
|---|---|
| `tags.db` | SQLite tag cache (`files_v2`) |
| `quarantine.json` | `{batch_id: {created, files: [{rel, size, quality}]}}` |
| `kept-both.json` | Sorted list of cluster keys |
| `sources.json` | Source rules; created with `_help` and `_example` on first start |

---

## 7. Frontend (`index.html`)

Vanilla JS, no framework, no build, no external requests. One global state object `S`:
`clusters, scanId, selected (Set of rel), section ("suggested"|"manual"|"kept"), query, reason, limit, open (Map key→bool), tags (Set of "key|rowIndex"), allFields (Set), player ({rel})`.

### Structure
- **Header**: title, library path, Duplicates / Quarantine tabs.
- **Controls**: match-mode segmented radios (Navidrome disabled unless `/api/info` says configured), Scan button, mode help text, status with progress meter (phase-aware), warnings line.
- **Sticky toolbar**: section tabs with counts (Suggested / Review / Kept both), reason filter, search (`/` to focus), Expand all / Collapse all.
- **Album card** (`cardHtml`): album title, artist, reason pill (green suggested, amber manual, grey kept), detail sentence, stats, actions (Select suggested, Select all removable, Deselect all, Keep both / Show again).
  - **Matrix**: one CSS grid with columns `# | Track | edition A | edition B | ...`. Header row = edition cards (role label Keep / Remove matching tracks / Edition A..., folder name diffed against the reference edition, parent path diffed, quality chip, year, track count, deluxe flag, source, added date, "Select all N from this edition" tri-state checkbox).
  - **Track rows**: number, title, evidence chips, Compare tags toggle, then one cell per edition with file entries (checkbox, quality chip, play button, duration, Lower quality marker, diffed filename, source with tooltip, added date). Selected files are struck through.
  - **Tag panel**: table of Audio props then Tags, one column per copy; differing cells highlighted and word-diffed; matching fields hidden unless "Show matching fields"; Format, Length, Audio MD5, ISRC always shown.
  - Suggested and Kept clusters start collapsed (edition header still visible); Review starts expanded.
- **Dock** (fixed bottom): player row (now playing, native `<audio>`, Switch copy, Close) above the action bar (selection count and size, note if it includes Review picks, Clear selection, Quarantine N files).
- **Toast** with optional action (Undo for quarantine, Keep both, Clear selection); positioned above the dock.
- **Quarantine tab**: batches with Restore and Delete permanently (confirm).

### Behaviours worth knowing
- Selection persists to `localStorage` under `music-dupes:sel:<scan_id>`; default is all suggested files outside Kept both.
- After a scan, if Suggested is empty but Review isn't, the Review tab opens.
- Card-level re-render (`rerenderCard`) on every interaction, then focus is restored to the triggering control. `data-indet="1"` is applied as `.indeterminate` after render.
- Word-level LCS diff (`diffPair`) tokenizes on Unicode letter/number runs, whitespace, and single other characters, so curly vs straight apostrophes and case changes highlight.
- Player: Switch copy (`B` key) loads the next live copy in the same track at the current position and keeps play state. `Esc` closes the player. Quarantining the playing file closes the player first.
- Paging: 30 clusters at a time with "Show N more".
- Mobile (<760px): matrix stacks, each cell gets an "Edition X: folder" label.

### Design tokens
Light: bg `#eef1f0`, panel `#ffffff`, sunk `#f6f8f7`, ink `#16202b`, muted `#5d6873`, line `#d5dbda`, hires `#4338a8`, lossless/keep `#0b7a5e`, lossy/remove `#b0542b`, warn `#9a5b00`, danger `#b42318`, diff `#f6dc94`.
Dark (prefers-color-scheme): bg `#12171c`, panel `#1a2128`, sunk `#161c22`, ink `#e6eaed`, muted `#96a1ab`, line `#2b353f`, hires `#a59cf2`, lossless/keep `#4fc59f`, lossy/remove `#e28b5f`, warn `#e7b25a`, danger `#f0786b`, diff `rgba(231,178,90,.32)`.
System UI font, tabular numerals. The quality chip (outlined, coloured by tier, format small + numbers large) is the deliberate visual signature; keep everything else quiet. Sentence-case labels, no all-caps eyebrows, no middle-dot meta strings, no emoji.

---

## 8. Owner preferences and decisions (from the conversation)

- Lossless-first listener. Hi-res beats CD quality; lossless beats lossy.
- **Keep both editions with different release years** unless raw data proves the same audio (one might be a re-recording). When proven, suggest which to delete.
- **Deluxe/expanded over standard**, using the suggested flow.
- **Different album artists (even similar names) → keep both, show unselected** for manual decision.
- **Anything the app can't determine → Review section, nothing selected**, with diffs, full raw tags and an inline player.
- Albums grouped with select-all/deselect-all per album and per edition; visually distinct from a flat track list.
- Wants to know which pipeline (Tidarr/Tidal, slskd, Usenet, torrents) each file came from.
- Wants one-click cleanup, but with reversible quarantine.
- Communication style: conversational, direct, no em dashes, no emojis; explain with concrete examples.

---

## 9. Configuration reference (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `MUSIC_DIR` | `/music` | Library mount inside the container |
| `QUARANTINE_DIR` | `$MUSIC_DIR/.dupe-quarantine` | Must stay inside the same mount |
| `CONFIG_DIR` | `/config` | State files |
| `NAVIDROME_URL` / `NAVIDROME_USER` / `NAVIDROME_PASSWORD` | unset | Enables Navidrome API mode |
| `NAVIDROME_MUSIC_ROOT` | `/music` | Library path as Navidrome sees it |
| `NAVIDROME_DB` | `/navidrome/navidrome.db` | DB fallback if API vars unset |
| `LIDARR_URL` / `LIDARR_API_KEY` | unset | Enables Lidarr source lookup |
| `LIDARR_MUSIC_ROOT` | `/data/media/music` | Library path as Lidarr sees it |
| `ALLOWED_HOSTS` | unset | Extra hostnames accepted by the request guard (`*` disables it) |

---

## 10. Testing

Everything below was used during development; tests passed at handoff.

### 10.1 Fixture library
`tests/mkfix.py` (appendix) uses ffmpeg to generate 26 short sine-wave files with exact tags covering every scenario in section 1: identical reissue, deluxe vs standard, different album artist, hi-res vs CD with same ISRC, a 7s length gap (FLAC vs AAC), different ISRCs, typo folder with no ISRC, curly vs straight apostrophe in one folder, FLAC + AAC in one folder, and a CD-quality deluxe vs hi-res standard. Identical sine frequency + duration produces identical FLAC MD5s.

Expected classification in `loose` mode:

| Artist | Kind | Reason | Keep |
|---|---|---|---|
| Band | suggested | Duplicate files in one folder | FLAC, AAC selected |
| Blindside | suggested | Identical audio | 2000 |
| Chris Renzema | suggested | Same recordings | Manna (2023) hi-res |
| Flyleaf | suggested | Duplicate files in one folder | one apostrophe variant |
| Gable Price | manual | Couldn't confirm same recordings | |
| Hres | manual | Better quality in the edition we'd remove | |
| Josh Garrels | manual | Different recordings | |
| Kings Kaleidoscope | manual | Different album artists | |
| Kutless | suggested | Deluxe edition covers the standard | Special Edition |
| Lynyrd Skynyrd | manual | Track lengths differ | |

### 10.2 Other checks performed
- Range request on `/api/audio` returns 206; `../../etc/passwd` returns 400.
- Navidrome API against a mock Subsonic server: real paths, fake paths (error with Report Real Path instructions), wrong password, unreachable server, paging.
- Navidrome DB fallback against fake pre-0.55 and 0.55+ schemas.
- Lidarr history mock with both `downloadClientName` and `downloadClient` field shapes; user rule by `path`.
- Playwright end-to-end (`tests/e2e.py`): scan, expand, Review tab, tag panel, play, edition select-all guard (second edition skipped with toast), Keep both moves the card, quarantine 8 files, Undo restores 8, mobile viewport, dark mode, zero console errors.

### 10.3 Suggested test hardening for Claude Code
Convert the ad-hoc scripts into pytest: a session fixture that builds the library once (requires ffmpeg), parametrized expectations from the table above, API tests via `fastapi.testclient`, and Playwright tests behind a marker.

---

## 11. Known limitations and open questions

- **Tidarr detection** has no verified fingerprint (see 4.7).
- **Lidarr renames** after import aren't tracked (only `trackFileImported` events).
- **Deluxe detection** is keyword-based; editions named only by year or label won't be recognized as deluxe and fall through to the ISRC/MD5 path.
- **Lossy pairs** (AAC vs MP3) have no MD5; confirmation relies on ISRC or same-folder slot.
- **AAC encoder padding** makes AAC durations slightly longer than FLAC; the 1.5s/2.5s tolerances absorb this, but very short tracks could be edge cases.
- **Loose mode** can pair different songs with the same title by the same artist (e.g. "Intro"). Evidence routes these to Review, never to Suggested, unless ISRC/MD5 confirm.
- **Scan state is in memory**: restarting the container loses results (selection persists in the browser per scan_id but becomes stale). Persisting the last scan to `/config` would be a nice improvement.
- **No auth**, LAN-only by design.
- First scan of a large library reads every file over HDD; expect minutes. Later scans are cache hits.
- The Navidrome API path has only been verified against mocks, not a live server. Verify field names (`bitDepth`, `samplingRate`, `path` with Report Real Path on 0.55+) on the real instance.
- The Lidarr history API path has only been verified against mocks.

---

## 12. Backlog ideas (not built)

- Built-in Tidarr detector once tag fingerprint is known.
- "Prefer source" setting (e.g. prefer Tidal over slskd when quality ties).
- Persist last scan results to disk; show "scanned X ago, N new files since".
- AcoustID / Chromaprint fingerprints as a third confirmation signal for lossy files (adds `fpcalc` dependency).
- Album-level decisions remembered across scans ("always keep deluxe for this artist").
- Waveform or loudness preview in the player for spotting remasters.
- Move folders, not just files: after quarantining every track of an edition, offer to quarantine leftover cover art / cue / log files and remove the empty folder.
- Trigger a Navidrome rescan via API after quarantine/restore.
- Optional basic auth for proxied access.
- Migrate the frontend to a typed setup (TypeScript + Vite or Preact) **only if** the owner accepts a build step; the no-build property is currently a feature.

---

## 13. Deployment steps (TrueNAS)

```bash
sudo mkdir -p /mnt/ssd-pool/apps/music-dupes/{app,config}
# copy app.py and index.html into /mnt/ssd-pool/apps/music-dupes/app/
sudo chown -R svc_apps:svc_apps /mnt/ssd-pool/apps/music-dupes
```

1. In Navidrome, create a non-admin user (e.g. `dupes`).
2. Copy Lidarr's API key from Settings → General → Security.
3. Paste `compose.yaml` into **Apps → Discover Apps → ⋮ → Install via YAML** as a Custom App named `music-dupes`, with real values inlined.
4. Open `http://10.0.9.101:8095`, run a Navidrome-mode scan once (it fails), then in Navidrome enable **Settings → Players → music-dupes → Report Real Path**, and scan again.
5. Optional: add Tidarr rules to `/mnt/ssd-pool/apps/music-dupes/config/sources.json`.

---

## Recommended repo layout

```
music-dupes/
├── CLAUDE.md              # section "Working agreements" below
├── HANDOFF.md             # this document
├── compose.yaml
├── app/
│   ├── app.py
│   └── index.html
├── tests/
│   ├── mkfix.py           # fixture library generator (needs ffmpeg)
│   ├── check_classify.py  # prints classification per mode
│   ├── serve.py           # runs the app against the fixtures on :8095
│   └── e2e.py             # Playwright flow
└── scripts/
    └── music_dupes.py     # original standalone CLI (optional, superseded)
```

Note: the test scripts below use absolute paths from the development sandbox (`/tmp/t2`, `/mnt/user-data/outputs/music-dupes`). Make them repo-relative when importing.

## Working agreements (paste into CLAUDE.md)

```markdown
# music-dupes

Self-hosted duplicate-music finder for a TrueNAS library. Read HANDOFF.md for full context.

## Hard rules
- Keep the app deployable by copying app/app.py and app/index.html: no build step, no new runtime deps without asking.
- Pinned deps: fastapi==0.115.6 uvicorn==0.34.0 mutagen==1.47.0. Python 3.12.
- Never weaken the safety invariants in HANDOFF.md section 5 (one copy always survives, quarantine before delete, path containment, manual clusters never pre-select).
- Never use FLAC bitrate as a quality signal.
- Bump the tag cache table name (files_v2 -> files_v3) whenever the cached dict shape changes.
- Secrets go inline in compose.yaml (TrueNAS Custom Apps don't read .env), never into the repo; use placeholders in git.

## Conventions
- UI copy: sentence case, plain verbs, no em dashes, no emojis, no all-caps labels, no middle-dot separators.
- Keep the quality chip as the single loud visual element; light and dark themes via CSS variables.
- Every destructive action gets an Undo toast where possible.

## Testing
- tests/mkfix.py builds the fixture library (ffmpeg required); expected results are in HANDOFF.md section 10.1.
- Run API checks with fastapi.testclient; Playwright for UI flows; check both light and dark, desktop and 390px wide.

---

## Appendix A: compose.yaml

````yaml
services:
  music-dupes:
    image: python:3.12-slim
    container_name: music-dupes
    restart: unless-stopped
    user: "3000:3000"
    working_dir: /app
    environment:
      - TZ=America/Los_Angeles
      - HOME=/tmp
      - PYTHONPATH=/tmp/deps
      - MUSIC_DIR=/music
      - CONFIG_DIR=/config
      # Optional Navidrome mode, via its API. Inline real values here, TrueNAS
      # Custom Apps don't load .env. NAVIDROME_MUSIC_ROOT is the music path as
      # Navidrome's container sees it (/music in the music app).
      - NAVIDROME_URL=http://10.0.9.101:4533
      - NAVIDROME_USER=dupes
      - NAVIDROME_PASSWORD=change_me
      - NAVIDROME_MUSIC_ROOT=/music
      # Optional: shows which download client each file came from, using
      # Lidarr's import history. LIDARR_MUSIC_ROOT is the music path as
      # Lidarr's container sees it (/data/media/music in the music app).
      - LIDARR_URL=http://10.0.9.101:8686
      - LIDARR_API_KEY=paste_lidarr_key_here
      - LIDARR_MUSIC_ROOT=/data/media/music
    # Installs deps into /tmp on each start (~10s), so there's no image to build.
    command: >
      sh -c "pip install --quiet --no-cache-dir --disable-pip-version-check
      --target /tmp/deps fastapi==0.115.6 uvicorn==0.34.0 mutagen==1.47.0
      && python -m uvicorn app:app --host 0.0.0.0 --port 8095"
    volumes:
      - /mnt/ssd-pool/apps/music-dupes/app:/app:ro
      - /mnt/ssd-pool/apps/music-dupes/config:/config
      # Read-write: quarantine is a rename inside this mount. Keep it ONE mount
      # so moves stay instant (two bind mounts = cross-device copy).
      - /mnt/tank/media/music:/music
      # Fallback only, used if the three NAVIDROME_URL/USER/PASSWORD vars are
      # unset. Snapshots the DB to /tmp, never writes here.
      # - /mnt/ssd-pool/apps/navidrome:/navidrome:ro
    ports:
      - 8095:8095
````

## Appendix B: app/app.py

````python
"""music-dupes: find, review and quarantine duplicate tracks.

Duplicates are grouped per track, then per album pair ("cluster"). Each cluster
is classified from raw tag and stream data:

  suggested  the app is confident which copies are redundant and pre-selects them
  manual     the app can't safely decide (different artists, different
             recordings, unconfirmable) and selects nothing

Nothing is deleted except by an explicit "Delete permanently" on a quarantine
batch. Quarantine is a rename into a hidden folder inside the library mount.
"""
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter, defaultdict
from statistics import mean

import mutagen
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from mutagen.id3 import ID3
from mutagen.mp4 import MP4Tags
from pydantic import BaseModel

# ---------- configuration ----------

MUSIC = os.path.realpath(os.environ.get("MUSIC_DIR", "/music"))
QDIR = os.path.realpath(
    os.environ.get("QUARANTINE_DIR", os.path.join(MUSIC, ".dupe-quarantine")))
CONFIG = os.environ.get("CONFIG_DIR", "/config")
HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(CONFIG, "quarantine.json")
IGNORED = os.path.join(CONFIG, "kept-both.json")
SOURCES = os.path.join(CONFIG, "sources.json")

# Navidrome: API preferred, DB snapshot as fallback. ND_ROOT is the music path
# as Navidrome's container sees it.
ND_DB = os.environ.get("NAVIDROME_DB", "/navidrome/navidrome.db")
ND_ROOT = os.path.normpath(os.environ.get("NAVIDROME_MUSIC_ROOT", "/music"))
ND_URL = os.environ.get("NAVIDROME_URL", "").rstrip("/")
ND_USER = os.environ.get("NAVIDROME_USER", "")
ND_PASS = os.environ.get("NAVIDROME_PASSWORD", "")
ND_CLIENT = "music-dupes"

# Lidarr (optional): its import history says which download client each file
# came from. LIDARR_ROOT is the music path as Lidarr's container sees it.
LIDARR_URL = os.environ.get("LIDARR_URL", "").rstrip("/")
LIDARR_KEY = os.environ.get("LIDARR_API_KEY", "")
LIDARR_ROOT = os.path.normpath(os.environ.get("LIDARR_MUSIC_ROOT", "/data/media/music"))

AUDIO_EXT = {".flac", ".m4a", ".mp3", ".ogg", ".opus", ".aac",
             ".wav", ".aiff", ".aif", ".wma"}
LOSSLESS_EXT = {"flac", "wav", "aiff", "aif"}
MIME = {"flac": "audio/flac", "m4a": "audio/mp4", "mp3": "audio/mpeg",
        "ogg": "audio/ogg", "opus": "audio/ogg", "aac": "audio/aac",
        "wav": "audio/wav", "aiff": "audio/aiff", "aif": "audio/aiff"}
MODES = {"same-folder", "cross-folder", "loose", "navidrome"}
BATCH_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
DELUXE_RE = re.compile(
    r"\b(deluxe|expanded|special edition|super deluxe|bonus tracks?|"
    r"collector'?s|anniversary|extended|complete edition)\b", re.I)
YEAR_RE = re.compile(r"\((\d{4})\)")
COPY_RE = re.compile(r"\s*\(\d+\)$")
LEN_TOL = 2.5  # seconds; beyond this two files aren't treated as the same take


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"[^\w]+", " ", s).strip()


def num(s):
    m = re.match(r"\s*(\d+)", s or "")
    return int(m.group(1)) if m else None


# ---------- raw tag reading ----------

ID3_NAMES = {"TIT2": "title", "TPE1": "artist", "TPE2": "albumartist",
             "TALB": "album", "TDRC": "date", "TYER": "date",
             "TDOR": "originaldate", "TRCK": "tracknumber",
             "TPOS": "discnumber", "TSRC": "isrc", "TCON": "genre",
             "TCOM": "composer", "TSSE": "encoder", "TENC": "encodedby",
             "TPUB": "label", "TCOP": "copyright", "TMED": "media",
             "TBPM": "bpm", "TLEN": "length"}
MP4_NAMES = {"\xa9nam": "title", "\xa9ART": "artist", "aART": "albumartist",
             "\xa9alb": "album", "\xa9day": "date", "trkn": "tracknumber",
             "disk": "discnumber", "\xa9gen": "genre", "\xa9wrt": "composer",
             "\xa9too": "encoder", "cprt": "copyright", "\xa9cmt": "comment",
             "\xa9lyr": "lyrics", "apID": "itunes account", "ownr": "itunes owner",
             "purd": "purchase date", "cnID": "itunes catalog id",
             "atID": "itunes artist id", "plID": "itunes album id",
             "sfID": "itunes storefront", "rtng": "advisory", "tmpo": "bpm",
             "cpil": "compilation", "pgap": "gapless", "stik": "media kind",
             "soal": "sort album", "soar": "sort artist", "sonm": "sort title",
             "soaa": "sort albumartist", "xid ": "xid"}
SKIP_TAGS = {"metadata_block_picture", "coverart", "coverartmime"}


def _s(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, tuple):  # MP4 track/disc pairs: (3, 12) -> 3/12
        return "/".join(str(x) for x in v if x)
    return str(v)


def raw_tags(a):
    """Every text tag on the file as {lowercase name: value}, art excluded."""
    out, art = {}, bool(getattr(a, "pictures", None))
    t = a.tags
    if t is None:
        return out, art

    def put(k, v):
        v = (v or "").strip()
        if not v:
            return
        if len(v) > 300:
            v = v[:300] + "\u2026"
        out[k] = f"{out[k]}; {v}" if k in out else v

    if isinstance(t, ID3):
        for fr in t.values():
            fid = fr.FrameID
            if fid == "APIC":
                art = True
                continue
            if fid == "UFID":
                put(f"ufid {fr.owner}", _s(fr.data))
                continue
            if fid == "TXXX":
                k = fr.desc.lower()
            elif fid == "COMM":
                k = "comment" + (f" {fr.desc.lower()}" if fr.desc else "")
            elif fid == "USLT":
                k = "lyrics"
            else:
                k = ID3_NAMES.get(fid, fid.lower())
            text = getattr(fr, "text", None)
            if text is None:
                text = getattr(fr, "url", "")
            put(k, "; ".join(_s(x) for x in text) if isinstance(text, list) else _s(text))
    elif isinstance(t, MP4Tags):
        for k, vals in t.items():
            if k == "covr":
                art = True
                continue
            name = k.split(":")[-1].lower() if k.startswith("----") else MP4_NAMES.get(k, k)
            vals = vals if isinstance(vals, list) else [vals]
            put(name, "; ".join(_s(v) for v in vals))
    else:  # Vorbis comments (FLAC, Ogg, Opus) and ASF
        for k, v in t:
            k = k.lower()
            if k in SKIP_TAGS:
                art = art or k == "metadata_block_picture"
                continue
            put(k, _s(v))
    return out, art


def read_file(path):
    try:
        a = mutagen.File(path)
    except Exception:
        return {"bad": True}
    if a is None:
        return {"bad": True}
    tags, art = raw_tags(a)
    info = a.info
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    codec = str(getattr(info, "codec", "") or ext).lower()
    md5 = getattr(info, "md5_signature", 0) or 0
    g = lambda *ks: next((tags[k] for k in ks if tags.get(k)), "")
    return {
        "ext": ext, "codec": codec,
        "lossless": ext in LOSSLESS_EXT or codec == "alac",
        "bits": getattr(info, "bits_per_sample", 0) or 0,
        "rate": getattr(info, "sample_rate", 0) or 0,
        "kbps": (getattr(info, "bitrate", 0) or 0) // 1000,
        "channels": getattr(info, "channels", 0) or 0,
        "secs": round(float(getattr(info, "length", 0) or 0), 3),
        "samples": getattr(info, "total_samples", 0) or 0,
        # FLAC stores an MD5 of the decoded audio: equal means bit-identical
        "md5": f"{md5:032x}" if md5 else "",
        "vendor": getattr(a.tags, "vendor", "") or "" if a.tags is not None else "",
        "art": art,
        "title": g("title"), "artist": g("artist"),
        "albumartist": g("albumartist", "album artist", "album_artist"),
        "album": g("album"), "date": g("date", "year", "originaldate"),
        "track": num(g("tracknumber", "track")),
        "disc": num(g("discnumber", "disc")) or 1,
        "isrc": re.sub(r"[^A-Z0-9]", "", g("isrc").upper().split(";")[0]),
        "tags": tags,
    }


def score(f):
    # FLAC bitrate only reflects compressibility, so lossless ties stay ties
    if f["lossless"]:
        return (1, f["bits"], f["rate"], 0)
    return (0, 0, 0, f["kbps"])


def label(f):
    if f["lossless"]:
        name = "ALAC" if f["codec"] == "alac" else f["ext"].upper()
        return name, f"{f['bits']}/{f['rate'] / 1000:g}"
    name = "AAC" if f["ext"] in ("m4a", "aac") else f["ext"].upper()
    return name, f"{f['kbps']}k"


def tier(f):
    if not f["lossless"]:
        return "lossy"
    return "hires" if f["bits"] > 16 or f["rate"] > 48000 else "lossless"


def group_key(f, mode):
    title = norm(f["title"])
    if mode == "navidrome":
        return (f["nd_album"], f["disc"], f["track"], title)
    if mode == "same-folder":
        if not title:
            stem = os.path.splitext(os.path.basename(f["rel"]))[0]
            title = norm(COPY_RE.sub("", stem))
        return (f["folder"], f["disc"], f["track"], title)
    if not title:
        return None
    if mode == "loose":
        return (norm(f["artist"]), title)
    return (norm(f["albumartist"] or f["artist"]), norm(f["album"]),
            f["disc"], f["track"], title)


# ---------- caches and small JSON stores ----------

class TagCache:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        # v2: full raw tags + stream data. Old v1 rows are simply ignored.
        self.db.execute("CREATE TABLE IF NOT EXISTS files_v2 ("
                        "path TEXT PRIMARY KEY, mtime REAL, size INTEGER, data TEXT)")
        self.lock = threading.Lock()

    def get(self, rel, mtime, size):
        with self.lock:
            row = self.db.execute(
                "SELECT data FROM files_v2 WHERE path=? AND mtime=? AND size=?",
                (rel, mtime, size)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, rel, mtime, size, data):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO files_v2 VALUES (?,?,?,?)",
                            (rel, mtime, size, json.dumps(data)))

    def commit(self):
        with self.lock:
            self.db.commit()


def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


def load_file(rel):
    """Our file dict for a library-relative path, or None if unreadable/gone."""
    p = os.path.join(MUSIC, rel)
    try:
        st = os.stat(p)
    except OSError:
        return None
    info = CACHE.get(rel, st.st_mtime, st.st_size)
    if info is None:
        info = read_file(p)
        CACHE.put(rel, st.st_mtime, st.st_size, info)
    if info.get("bad"):
        return None
    return dict(info, rel=rel, folder=os.path.dirname(rel),
                size=st.st_size, mtime=st.st_mtime)


# ---------- where a file came from ----------

def fetch_lidarr_sources():
    """{rel: download client name} for every file Lidarr imported."""
    if not (LIDARR_URL and LIDARR_KEY):
        return {}, None
    out, page = {}, 1
    try:
        while page <= 200:
            q = urllib.parse.urlencode({
                "page": page, "pageSize": 1000, "eventType": 3,  # trackFileImported
                "sortKey": "date", "sortDirection": "ascending"})
            req = urllib.request.Request(f"{LIDARR_URL}/api/v1/history?{q}",
                                         headers={"X-Api-Key": LIDARR_KEY})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            recs = data.get("records", [])
            for rec in recs:
                d = rec.get("data") or {}
                p = d.get("importedPath")
                if not p:
                    continue
                rel = os.path.relpath(os.path.normpath(p), LIDARR_ROOT)
                if not rel.startswith(".."):
                    out[rel] = (d.get("downloadClientName") or d.get("downloadClient")
                                or "unknown client")
            if not recs or page * 1000 >= data.get("totalRecords", 0):
                break
            page += 1
        return out, None
    except Exception as e:
        return out, f"Couldn't read Lidarr's history ({e}), so download sources are partial."


def detect_source(f, lidarr, rules):
    """(label, why) for a file. Evidence first, guesses last."""
    if f["rel"] in lidarr:
        return f"{lidarr[f['rel']]} via Lidarr", "Lidarr's import history"
    tags = f["tags"]
    for r in rules:
        try:
            tag = (r.get("tag") or "").lower()
            if tag == "path":
                hay = f["rel"]
            elif tag:
                hay = tags.get(tag, "")
            else:
                hay = "\n".join(f"{k}={v}" for k, v in tags.items())
            if hay and re.search(r["pattern"], hay, re.I):
                where = tag or "any tag"
                return r["label"], f"Your rule: {where} matches /{r['pattern']}/"
        except (KeyError, re.error, TypeError):
            continue
    if {"itunes account", "itunes owner", "purchase date"} & tags.keys():
        return "iTunes Store", "iTunes purchase tags (apID / ownr / purd)"
    if "tidal" in " ".join(tags.values()).lower():
        return "Tidal", "A tag value mentions Tidal"
    if any("musicbrainz" in k for k in tags):
        return "MusicBrainz-tagged", "Has MusicBrainz IDs (Lidarr, Picard or beets wrote these)"
    if lidarr:
        return "Not from Lidarr", "Not in Lidarr's import history"
    return "Unknown", "No identifying tags. Add a rule in sources.json."


# ---------- Navidrome sources (used only for grouping) ----------

def navidrome_source():
    if ND_URL and ND_USER and ND_PASS:
        return "api"
    if os.path.isfile(ND_DB):
        return "db"
    return None


def nd_api(endpoint, **params):
    salt = secrets.token_hex(8)
    q = {"u": ND_USER, "s": salt, "v": "1.16.1", "c": ND_CLIENT, "f": "json",
         "t": hashlib.md5((ND_PASS + salt).encode()).hexdigest(), **params}
    url = f"{ND_URL}/rest/{endpoint}?{urllib.parse.urlencode(q)}"
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            resp = json.load(r)["subsonic-response"]
    except urllib.error.URLError as e:
        raise RuntimeError(f"can't reach Navidrome at {ND_URL} ({e.reason})")
    if resp.get("status") != "ok":
        raise RuntimeError(resp.get("error", {}).get("message", "request failed"))
    return resp


def nd_songs_api():
    update(total=nd_api("getScanStatus").get("scanStatus", {}).get("count", 0))
    page, offset = 500, 0
    while True:
        songs = nd_api("search3", query="", artistCount=0, albumCount=0,
                       songCount=page, songOffset=offset
                       ).get("searchResult3", {}).get("song", [])
        for s in songs:
            yield {"path": s.get("path"), "album_id": s.get("albumId"),
                   "title": s.get("title"), "track": s.get("track"),
                   "disc": s.get("discNumber")}
        offset += len(songs)
        update(scanned=offset)
        if len(songs) < page:
            break


def nd_songs_db():
    tmp = tempfile.mkdtemp()
    try:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(ND_DB + suffix):
                shutil.copy2(ND_DB + suffix, os.path.join(tmp, "nd.db" + suffix))
        db = sqlite3.connect(os.path.join(tmp, "nd.db"))
        db.row_factory = sqlite3.Row
        cols = {r["name"] for r in db.execute("PRAGMA table_info(media_file)")}
        if not cols:
            raise RuntimeError("Navidrome's database has no media_file table.")
        lib = "library_id" if "library_id" in cols else "0 AS library_id"
        sql = (f"SELECT path, album_id, title, track_number, disc_number, {lib} "
               "FROM media_file")
        if "missing" in cols:
            sql += " WHERE missing = 0"
        libs = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='library'").fetchone():
            libs = {r["id"]: r["path"] for r in db.execute("SELECT id, path FROM library")}
        rows = db.execute(sql).fetchall()
        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    update(total=len(rows))
    for r in rows:
        p = r["path"]
        if not os.path.isabs(p):
            p = os.path.join(libs.get(r["library_id"], ND_ROOT), p)
        yield {"path": p, "album_id": r["album_id"], "title": r["title"],
               "track": r["track_number"], "disc": r["disc_number"]}


def navidrome_groups():
    """Groups of rels Navidrome shows as the same track in the same album."""
    source = navidrome_source()
    songs = nd_songs_api() if source == "api" else nd_songs_db()
    buckets, seen, found = defaultdict(list), 0, 0
    for s in songs:
        seen += 1
        p = os.path.normpath(s["path"] or "")
        rel = os.path.relpath(p, ND_ROOT) if os.path.isabs(p) else p
        if rel.startswith("..") or not os.path.exists(os.path.join(MUSIC, rel)):
            continue
        found += 1
        key = (s["album_id"], s["disc"] or 1, s["track"], norm(s["title"]))
        buckets[key].append((rel, s["album_id"]))
    if source == "api" and seen >= 20 and found < seen / 2:
        raise RuntimeError(
            f"only {found} of {seen} paths Navidrome reported exist under "
            f"{MUSIC}. In Navidrome, open Settings > Players, find "
            f"'{ND_CLIENT}', turn on Report Real Path, then scan again. "
            "If that's already on, check NAVIDROME_MUSIC_ROOT.")
    return [g for g in buckets.values() if len(g) > 1]


# ---------- analysis ----------

def fmt_len(s):
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}" if s >= 60 else f"{s}s"


def year_of(f):
    m = re.match(r"\s*(\d{4})", f["date"] or "")
    if m:
        return m.group(1)
    m = YEAR_RE.search(os.path.basename(f["folder"]))
    return m.group(1) if m else ""


def row_evidence(g, single):
    md5s = [f["md5"] for f in g]
    identical = all(md5s) and len(set(md5s)) == 1
    isrcs = [f["isrc"] for f in g]
    same_isrc = all(isrcs) and len(set(isrcs)) == 1
    isrc_conflict = all(isrcs) and len(set(isrcs)) > 1
    secs = [f["secs"] for f in g]
    spread = max(secs) - min(secs)
    same_slot = len({(f["disc"], f["track"]) for f in g}) == 1
    same_fmt = len({score(f) for f in g if f["lossless"]}) == 1 and all(f["lossless"] for f in g)
    chips = []
    add = lambda tone, text, why="": chips.append({"tone": tone, "text": text, "why": why})
    if identical:
        add("good", "Identical audio", "The FLAC audio checksums match, so the decoded audio is bit-for-bit the same.")
    elif all(md5s) and same_fmt:
        add("neutral", "Audio differs", "Same format, but the decoded audio isn't bit-identical. Usually a different master, remaster or edit.")
    if same_isrc:
        add("good", "Same ISRC", f"Both carry recording code {isrcs[0]}.")
    elif isrc_conflict:
        add("warn", "ISRC differs", " vs ".join(sorted(set(isrcs))) + ". Different codes normally mean a different recording.")
    elif any(isrcs):
        add("neutral", "ISRC on some copies only", "Can't compare recordings by ISRC.")
    if spread <= 1:
        add("good", "Same length")
    elif spread <= LEN_TOL:
        add("neutral", f"Length off by {spread:.1f}s", "Small gaps usually mean a different master or different padding.")
    else:
        add("warn", f"Length off by {fmt_len(spread)}", "Probably a different version, edit or recording.")
    blocked = isrc_conflict or spread > LEN_TOL
    if identical:
        confirmed = "identical"
    elif blocked:
        confirmed = None
    elif same_isrc and spread <= 1.5:
        confirmed = "recording"
    elif single and same_slot and spread <= 1:
        confirmed = "slot"  # same track slot in the same album folder
    else:
        confirmed = None
    return {"chips": chips, "identical": identical, "isrc_conflict": isrc_conflict,
            "spread": spread, "blocked": blocked, "confirmed": confirmed,
            "has_isrc": all(isrcs)}


def keep_rank(f):
    """Within one folder: better quality, no ' (1)' suffix, more tags, older."""
    stem = os.path.splitext(os.path.basename(f["rel"]))[0]
    return (score(f), not COPY_RE.search(stem), len(f["tags"]), -f["mtime"])


def file_props(f):
    fmt, detail = label(f)
    return {
        "Format": f"{fmt} {detail}",
        "Codec": f["codec"].upper(),
        "Bit depth": str(f["bits"]) if f["lossless"] else "",
        "Sample rate": f"{f['rate']} Hz" if f["rate"] else "",
        "Bitrate": f"{f['kbps']} kbps",
        "Channels": str(f["channels"] or ""),
        "Length": f"{f['secs']:.3f} s",
        "Samples": str(f["samples"] or ""),
        "Audio MD5": f["md5"],
        "Encoder (vendor)": f["vendor"],
        "Cover art": "Embedded" if f["art"] else "None",
        "File size": f"{f['size']:,} bytes",
        "Modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(f["mtime"])),
    }


def build_cluster(folders, rows, ignored, folder_counts):
    rows.sort(key=lambda g: (min(f["disc"] for f in g),
                             min(f["track"] or 999 for f in g), norm(g[0]["title"])))
    single = len(folders) == 1
    eds = []
    for folder in folders:
        fs = [f for g in rows for f in g if f["folder"] == folder]
        first, best = fs[0], max(fs, key=score)
        if folder not in folder_counts:
            try:
                folder_counts[folder] = sum(
                    1 for n in os.listdir(os.path.join(MUSIC, folder))
                    if not n.startswith(".") and os.path.splitext(n)[1].lower() in AUDIO_EXT)
            except OSError:
                folder_counts[folder] = 0
        fmt, detail = label(best)
        eds.append({
            "folder": folder,
            "artist": first["albumartist"] or first["artist"] or "Unknown artist",
            "album": first["album"] or os.path.basename(folder),
            "year": year_of(first),
            "deluxe": bool(DELUXE_RE.search(f"{first['album']} {os.path.basename(folder)}")),
            "tracks": folder_counts[folder],
            "format": fmt, "detail": detail, "tier": tier(best),
            "source": Counter(f["source"] for f in fs).most_common(1)[0][0],
            "added": max(f["mtime"] for f in fs),
            "tag_count": round(mean(len(f["tags"]) for f in fs), 1),
            "keep": False,
        })
    evs = [row_evidence(g, single) for g in rows]
    kind, reason, detail, keeper = classify(eds, rows, evs, single)

    # Pre-select per row: keep the best copy in the keeper folder; everything
    # else in the row goes only if it isn't better than what we keep.
    picks = []
    if keeper is not None:
        for g in rows:
            mine = [f for f in g if f["folder"] == keeper]
            keep = max(mine, key=keep_rank)
            others = [f for f in g if f is not keep]
            if any(score(f) > score(keep) for f in others):
                kind, keeper, picks = "manual", None, []
                reason = "Better quality in the edition we'd remove"
                detail = ("The edition that looks like the keeper has lower-quality "
                          "copies of some tracks. Pick per track.")
                break
            picks.extend(f["rel"] for f in others)
    if keeper is not None:
        for e in eds:
            e["keep"] = e["folder"] == keeper
    picks = set(picks)

    out_rows = []
    for g, ev in zip(rows, evs):
        best = max(score(f) for f in g)
        files = []
        for f in sorted(g, key=lambda f: (folders.index(f["folder"]), -score(f)[1])):
            fmt, det = label(f)
            files.append({
                "rel": f["rel"], "name": os.path.basename(f["rel"]),
                "ed": folders.index(f["folder"]),
                "format": fmt, "detail": det, "tier": tier(f),
                "secs": f["secs"], "size": f["size"], "mtime": f["mtime"],
                "source": f["source"], "source_why": f["source_why"],
                "quality": "best" if score(f) == best else "lower",
                "suggested": f["rel"] in picks, "moved": False,
                "props": file_props(f), "tags": f["tags"],
            })
        h = g[0]
        out_rows.append({"title": h["title"] or os.path.basename(h["rel"]),
                         "track": h["track"], "disc": h["disc"],
                         "evidence": ev["chips"], "files": files})
    key = "\n".join(sorted(f["rel"] for g in rows for f in g))
    return {"key": key, "kind": kind, "reason": reason, "detail": detail,
            "ignored": key in ignored, "editions": eds, "rows": out_rows,
            "artist": eds[0]["artist"], "album": eds[0]["album"]}


def classify(eds, rows, evs, single):
    """-> (kind, reason, detail, keeper folder or None)"""
    if not single and len({norm(e["artist"]) for e in eds}) > 1:
        return ("manual", "Different album artists",
                "The same tracks are filed under different artists. Kept separate "
                "unless you decide otherwise.", None)
    n_conflict = sum(e["isrc_conflict"] for e in evs)
    if n_conflict:
        return ("manual", "Different recordings",
                f"ISRCs differ on {n_conflict} of {len(evs)} tracks. That usually "
                "means a re-recording or a different version. Listen to compare.", None)
    n_len = sum(e["spread"] > LEN_TOL for e in evs)
    if n_len:
        return ("manual", "Track lengths differ",
                f"Lengths differ by more than {LEN_TOL:g}s on {n_len} of {len(evs)} "
                "tracks, so these probably aren't the same take. Listen to compare.", None)
    if single:
        if all(e["confirmed"] for e in evs):
            return ("suggested", "Duplicate files in one folder",
                    "Same track slot in the same album folder with matching length. "
                    "The best copy stays.", eds[0]["folder"])
        return ("manual", "Couldn't confirm duplicates",
                "Same folder, but the lengths or recordings don't line up.", None)

    deluxe = [e for e in eds if e["deluxe"]]
    if deluxe and len(deluxe) < len(eds):
        keeper = max(deluxe, key=lambda e: (e["tracks"], e["tag_count"]))
        return ("suggested", "Deluxe edition covers the standard",
                f"Keeping {keeper['album']}, which has {keeper['tracks']} tracks. "
                "Matching tracks in the standard edition are selected.", keeper["folder"])

    if all(e["confirmed"] in ("identical", "recording") for e in evs):
        best_count = Counter()
        for g in rows:
            top = max(score(f) for f in g)
            for folder in {f["folder"] for f in g if score(f) == top}:
                best_count[folder] += 1
        keeper = max(eds, key=lambda e: (best_count[e["folder"]], e["tracks"],
                                         e["tag_count"], -int(e["year"] or 9999)))
        ident = all(e["identical"] for e in evs)
        reason = "Identical audio" if ident else "Same recordings"
        why = ("Every track's decoded audio is bit-for-bit identical."
               if ident else "Every track carries the same ISRC with matching length.")
        years = {e["year"] for e in eds if e["year"]}
        if len(years) > 1:
            why += f" Release years differ ({', '.join(sorted(years))}), but it's the same audio, not a re-recording."
        return ("suggested", reason,
                f"{why} Keeping the edition with the best quality, then the most "
                "tracks, then the richest tags, then the earliest year.", keeper["folder"])

    missing = sum(not e["has_isrc"] and not e["identical"] for e in evs)
    return ("manual", "Couldn't confirm same recordings",
            f"There's no shared ISRC to compare on {missing or 'some'} of {len(evs)} "
            "tracks, and the audio checksums don't match. Listen to compare.", None)


# ---------- scan ----------

STATE = {"status": "idle", "scan_id": None, "mode": None, "phase": "",
         "scanned": 0, "total": 0, "unreadable": 0, "started": None,
         "finished": None, "error": None, "warnings": [], "clusters": []}
LOCK = threading.Lock()


def update(**kw):
    with LOCK:
        STATE.update(kw)


def walk_audio():
    for d, dirs, files in os.walk(MUSIC):
        dirs[:] = [x for x in dirs
                   if not x.startswith(".") and os.path.join(d, x) != QDIR]
        for n in files:
            if not n.startswith(".") and os.path.splitext(n)[1].lower() in AUDIO_EXT:
                yield os.path.join(d, n)


def run_scan(mode):
    try:
        warnings = []
        update(phase="Reading Lidarr history" if LIDARR_URL else "Listing files")
        lidarr, warn = fetch_lidarr_sources()
        if warn:
            warnings.append(warn)
        rules = load_json(SOURCES, {}).get("rules", [])
        groups, bad = [], 0

        if mode == "navidrome":
            update(phase="Asking Navidrome")
            nd = navidrome_groups()
            update(phase="Reading tags", scanned=0, total=sum(len(g) for g in nd))
            i = 0
            for g in nd:
                fs = []
                for rel, album_id in g:
                    i += 1
                    f = load_file(rel)
                    if f is None:
                        bad += 1
                        continue
                    f["nd_album"] = album_id
                    fs.append(f)
                if len(fs) > 1:
                    groups.append(fs)
                if i % 100 < len(g):
                    update(scanned=i)
        else:
            update(phase="Listing files")
            rels = [os.path.relpath(p, MUSIC) for p in walk_audio()]
            update(phase="Reading tags", total=len(rels))
            buckets = defaultdict(list)
            for i, rel in enumerate(rels, 1):
                f = load_file(rel)
                if f is None:
                    bad += 1
                else:
                    k = group_key(f, mode)
                    if k is not None:
                        buckets[k].append(f)
                if i % 250 == 0:
                    update(scanned=i, unreadable=bad)
                    CACHE.commit()
            for g in buckets.values():
                if len(g) < 2:
                    continue
                if mode == "cross-folder" and len({f["folder"] for f in g}) < 2:
                    continue
                groups.append(g)
        CACHE.commit()

        update(phase="Comparing")
        for g in groups:
            for f in g:
                f["source"], f["source_why"] = detect_source(f, lidarr, rules)
        by_folders = defaultdict(list)
        for g in groups:
            by_folders[tuple(sorted({f["folder"] for f in g}))].append(g)
        ignored = set(load_json(IGNORED, []))
        counts = {}
        clusters = [build_cluster(list(folders), rows, ignored, counts)
                    for folders, rows in by_folders.items()]
        clusters.sort(key=lambda c: (c["artist"].casefold(), c["album"].casefold()))
        update(status="done", phase="", scanned=STATE["total"], unreadable=bad,
               clusters=clusters, warnings=warnings, finished=time.time())
    except Exception as e:
        update(status="error", phase="", error=str(e), finished=time.time())


# ---------- file moves ----------

def inside(path, root):
    return os.path.commonpath([path, root]) == root


def library_path(rel):
    p = os.path.realpath(os.path.join(MUSIC, rel))
    if not inside(p, MUSIC) or inside(p, QDIR):
        raise ValueError("path is outside the library")
    return p


def move(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.move(src, dst)


def prune_empty(root):
    for d, _, _ in os.walk(root, topdown=False):
        try:
            if not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass


MLOCK = threading.Lock()

# ---------- app ----------

os.makedirs(QDIR, exist_ok=True)
_ndignore = os.path.join(QDIR, ".ndignore")
if not os.path.exists(_ndignore):
    with open(_ndignore, "w") as fh:
        fh.write("*\n")
if not os.path.exists(SOURCES):
    save_json(SOURCES, {
        "_help": ("Rules run top to bottom, first match wins. 'tag' is a tag name "
                  "as shown in the app's tag table (lowercase), 'path' to match the "
                  "file path, or leave it out to search every tag. 'pattern' is a "
                  "case-insensitive regex."),
        "_example": {"label": "Tidarr", "tag": "comment", "pattern": "tidal"},
        "rules": []})
CACHE = TagCache(os.path.join(CONFIG, "tags.db"))

app = FastAPI(title="music-dupes")


class ScanReq(BaseModel):
    mode: str = "same-folder"


class PathsReq(BaseModel):
    paths: list[str]


class BatchReq(BaseModel):
    batch: str


class KeepBothReq(BaseModel):
    key: str
    kept: bool = True


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "index.html"))


@app.get("/api/info")
def info():
    return {"music": MUSIC, "quarantine": QDIR, "navidrome": navidrome_source(),
            "lidarr": bool(LIDARR_URL and LIDARR_KEY)}


@app.get("/api/scan")
def scan_state():
    with LOCK:
        return dict(STATE)


@app.post("/api/scan")
def start_scan(req: ScanReq):
    if req.mode not in MODES:
        raise HTTPException(400, f"Unknown mode: {req.mode}")
    if req.mode == "navidrome" and not navidrome_source():
        raise HTTPException(400, "Navidrome isn't configured. Set NAVIDROME_URL, "
                                 "NAVIDROME_USER and NAVIDROME_PASSWORD.")
    with LOCK:
        if STATE["status"] == "scanning":
            raise HTTPException(409, "A scan is already running.")
        STATE.update(status="scanning", scan_id=uuid.uuid4().hex, mode=req.mode,
                     phase="Starting", scanned=0, total=0, unreadable=0,
                     started=time.time(), finished=None, error=None,
                     warnings=[], clusters=[])
    threading.Thread(target=run_scan, args=(req.mode,), daemon=True).start()
    return {"ok": True}


@app.get("/api/audio")
def audio(rel: str):
    try:
        p = library_path(rel)
    except ValueError:
        raise HTTPException(400, "That path is outside the library.")
    if not os.path.isfile(p):
        raise HTTPException(404, "That file isn't there anymore.")
    ext = os.path.splitext(p)[1].lower().lstrip(".")
    return FileResponse(p, media_type=MIME.get(ext, "application/octet-stream"))


@app.post("/api/keep-both")
def keep_both(req: KeepBothReq):
    with MLOCK:
        kept = set(load_json(IGNORED, []))
        (kept.add if req.kept else kept.discard)(req.key)
        save_json(IGNORED, sorted(kept))
    with LOCK:
        for c in STATE["clusters"]:
            if c["key"] == req.key:
                c["ignored"] = req.kept
    return {"ok": True}


@app.post("/api/quarantine")
def quarantine(req: PathsReq):
    with LOCK:
        if STATE["status"] != "done":
            raise HTTPException(409, "Run a scan first.")
        rows = [r for c in STATE["clusters"] for r in c["rows"]]
    wanted = set(req.paths)
    batch = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    moved, errors = [], []
    for r in rows:
        live = [f for f in r["files"] if not f["moved"]]
        picks = [f for f in live if f["rel"] in wanted]
        if not picks:
            continue
        if len(picks) >= len(live):  # server-side guard, never trust the client
            errors.append(f"Skipped {r['title']}: every copy was selected, so nothing would be left.")
            continue
        for f in picks:
            try:
                move(library_path(f["rel"]), os.path.join(QDIR, batch, f["rel"]))
                f["moved"] = True
                moved.append({"rel": f["rel"], "size": f["size"],
                              "quality": f"{f['format']} {f['detail']}"})
            except Exception as e:
                errors.append(f"{f['rel']}: {e}")
    if moved:
        with MLOCK:
            m = load_json(MANIFEST, {})
            m[batch] = {"created": time.time(), "files": moved}
            save_json(MANIFEST, m)
    return {"batch": batch, "moved": len(moved),
            "bytes": sum(f["size"] for f in moved), "errors": errors}


@app.get("/api/quarantine")
def list_quarantine():
    with MLOCK:
        m = load_json(MANIFEST, {})
    out = []
    for bid in sorted(m, reverse=True):
        files = [f for f in m[bid]["files"]
                 if os.path.exists(os.path.join(QDIR, bid, f["rel"]))]
        if files:
            out.append({"batch": bid, "created": m[bid]["created"], "files": files,
                        "bytes": sum(f["size"] for f in files)})
    return out


def get_batch(m, bid):
    if not BATCH_RE.fullmatch(bid) or bid not in m:
        raise HTTPException(404, "That quarantine batch doesn't exist.")
    return m[bid]


@app.post("/api/restore")
def restore(req: BatchReq):
    with MLOCK:
        m = load_json(MANIFEST, {})
        b = get_batch(m, req.batch)
        restored, remaining, errors, back = 0, [], [], set()
        for f in b["files"]:
            src = os.path.join(QDIR, req.batch, f["rel"])
            if not os.path.exists(src):
                continue
            dst = library_path(f["rel"])
            if os.path.exists(dst):
                errors.append(f"{f['rel']}: a file already exists there, left in quarantine.")
                remaining.append(f)
                continue
            move(src, dst)
            back.add(f["rel"])
            restored += 1
        bdir = os.path.join(QDIR, req.batch)
        if remaining:
            b["files"] = remaining
            prune_empty(bdir)
        else:
            del m[req.batch]
            shutil.rmtree(bdir, ignore_errors=True)
        save_json(MANIFEST, m)
    with LOCK:  # restored files show up in the current results again
        for c in STATE["clusters"]:
            for r in c["rows"]:
                for f in r["files"]:
                    if f["rel"] in back:
                        f["moved"] = False
    return {"restored": restored, "errors": errors}


@app.post("/api/purge")
def purge(req: BatchReq):
    with MLOCK:
        m = load_json(MANIFEST, {})
        b = get_batch(m, req.batch)
        freed = sum(f["size"] for f in b["files"]
                    if os.path.exists(os.path.join(QDIR, req.batch, f["rel"])))
        shutil.rmtree(os.path.join(QDIR, req.batch), ignore_errors=True)
        del m[req.batch]
        save_json(MANIFEST, m)
    return {"bytes": freed}
````

## Appendix C: app/index.html

````html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Duplicate tracks</title>
<style>
  :root {
    --bg: #eef1f0; --panel: #ffffff; --sunk: #f6f8f7; --ink: #16202b; --muted: #5d6873;
    --line: #d5dbda; --hires: #4338a8; --lossless: #0b7a5e; --lossy: #b0542b;
    --warn: #9a5b00; --danger: #b42318; --focus: #4338a8;
    --diff: #f6dc94; --keep: #0b7a5e; --remove: #b0542b;
    color-scheme: light dark;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #12171c; --panel: #1a2128; --sunk: #161c22; --ink: #e6eaed; --muted: #96a1ab;
      --line: #2b353f; --hires: #a59cf2; --lossless: #4fc59f; --lossy: #e28b5f;
      --warn: #e7b25a; --danger: #f0786b; --focus: #a59cf2;
      --diff: rgba(231, 178, 90, .32); --keep: #4fc59f; --remove: #e28b5f;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
    font-variant-numeric: tabular-nums;
  }
  main { max-width: 1240px; margin: 0 auto; padding: 32px 20px 200px; }
  button, input, select { font: inherit; color: inherit; }
  :focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
  [hidden] { display: none !important; }

  header { display: flex; flex-wrap: wrap; gap: 16px; align-items: end; justify-content: space-between; }
  h1 { font-size: 30px; line-height: 1.1; margin: 0; letter-spacing: -0.02em; }
  .lib { color: var(--muted); font-size: 13px; margin-top: 6px; }
  .tabs { display: flex; gap: 4px; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 3px; }
  .tabs button { border: 0; background: none; padding: 6px 14px; border-radius: 7px; cursor: pointer; color: var(--muted); }
  .tabs button[aria-selected="true"] { background: var(--ink); color: var(--bg); }

  .controls { margin-top: 28px; display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
  .modes { display: flex; flex-wrap: wrap; border: 1px solid var(--line); border-radius: 10px; overflow: hidden; background: var(--panel); }
  .modes label { padding: 8px 14px; cursor: pointer; color: var(--muted); border-right: 1px solid var(--line); }
  .modes label:last-child { border-right: 0; }
  .modes input { position: absolute; opacity: 0; pointer-events: none; }
  .modes label:has(input:checked) { background: var(--bg); color: var(--ink); font-weight: 600; }
  .modes label:has(input:disabled) { opacity: .45; cursor: not-allowed; }
  .modes label:has(input:focus-visible) { outline: 2px solid var(--focus); outline-offset: -2px; }
  .mode-help { color: var(--muted); font-size: 13px; flex-basis: 100%; max-width: 80ch; margin: 0; }

  .btn { border: 1px solid var(--line); background: var(--panel); padding: 7px 14px; border-radius: 9px; cursor: pointer; white-space: nowrap; }
  .btn:disabled { opacity: .5; cursor: default; }
  .btn.primary { background: var(--ink); color: var(--bg); border-color: var(--ink); font-weight: 600; }
  .btn.small { padding: 4px 10px; font-size: 13px; }
  .btn.danger { color: var(--danger); }
  .link { border: 0; background: none; padding: 0; color: var(--muted); text-decoration: underline; text-underline-offset: 3px; cursor: pointer; font-size: 13px; }
  .link:hover { color: var(--ink); }

  .status { margin-top: 18px; color: var(--muted); }
  .warnings { color: var(--warn); font-size: 13px; margin-top: 6px; }
  .meter { height: 6px; background: var(--line); border-radius: 3px; overflow: hidden; margin-top: 8px; max-width: 420px; }
  .meter > div { height: 100%; background: var(--ink); transition: width .3s; }

  /* section switcher + filters stay reachable while scrolling */
  .toolbar { position: sticky; top: 0; z-index: 5; background: var(--bg); padding: 14px 0 12px; margin-top: 18px; border-bottom: 1px solid var(--line); display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  .sections { display: flex; gap: 6px; }
  .sections button { border: 1px solid var(--line); background: var(--panel); border-radius: 9px; padding: 7px 14px; cursor: pointer; }
  .sections button[aria-selected="true"] { border-color: var(--ink); box-shadow: inset 0 0 0 1px var(--ink); font-weight: 600; }
  .sections .n { color: var(--muted); font-weight: 400; margin-left: 4px; }
  .toolbar .spacer { flex: 1; }
  .search { padding: 7px 12px; border: 1px solid var(--line); border-radius: 9px; background: var(--panel); min-width: 240px; }
  .reason-filter { padding: 7px 10px; border: 1px solid var(--line); border-radius: 9px; background: var(--panel); }
  .section-help { color: var(--muted); font-size: 14px; margin: 14px 0 0; max-width: 80ch; }

  /* ---------- album card ---------- */
  .album { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; margin-top: 18px; overflow: hidden; }
  .album-head { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: start; padding: 16px 18px 10px; }
  .album-id { flex: 1 1 280px; min-width: 0; }
  .album-id h2 { font-size: 19px; margin: 0; line-height: 1.25; letter-spacing: -0.01em; }
  .album-id .by { color: var(--muted); }
  .reason { display: inline-block; margin-top: 8px; font-size: 13px; font-weight: 600; padding: 2px 9px; border-radius: 999px; border: 1.5px solid currentColor; }
  .reason.suggested { color: var(--keep); }
  .reason.manual { color: var(--warn); }
  .reason.kept { color: var(--muted); }
  .detail { margin: 6px 0 0; color: var(--muted); font-size: 14px; max-width: 78ch; }
  .album-side { display: flex; flex-direction: column; align-items: end; gap: 8px; }
  .album-stats { font-size: 13px; color: var(--muted); display: flex; gap: 12px; }
  .album-actions { display: flex; flex-wrap: wrap; gap: 6px; justify-content: end; }

  .matrix { overflow-x: auto; border-top: 1px solid var(--line); }
  .mgrid { display: grid; grid-template-columns: 3rem minmax(11rem, 15rem) repeat(var(--n), minmax(15rem, 1fr)); min-width: min-content; }
  .mhead > div { padding: 12px 12px; background: var(--sunk); border-bottom: 1px solid var(--line); }
  .mhead .h-label { color: var(--muted); font-size: 13px; align-self: end; }

  .ed { border-left: 1px solid var(--line); display: flex; flex-direction: column; gap: 4px; }
  .ed-role { font-size: 13px; font-weight: 700; color: var(--muted); }
  .ed.keep { box-shadow: inset 3px 0 0 var(--keep); }
  .ed.keep .ed-role { color: var(--keep); }
  .ed.remove .ed-role { color: var(--remove); }
  .ed-name { font-weight: 650; overflow-wrap: anywhere; }
  .ed-path { font-size: 13px; color: var(--muted); overflow-wrap: anywhere; }
  .ed-meta { display: flex; flex-wrap: wrap; gap: 4px 12px; align-items: center; font-size: 13px; color: var(--muted); }
  .ed-all { display: flex; gap: 8px; align-items: center; font-size: 13px; margin-top: 4px; cursor: pointer; }
  .ed-all input { width: 16px; height: 16px; margin: 0; }

  .mrow > div { padding: 10px 12px; border-bottom: 1px solid var(--line); }
  .mrow:last-of-type > div { border-bottom: 0; }
  .m-num { color: var(--muted); text-align: right; font-size: 14px; padding-top: 12px !important; }
  .m-title .t { font-weight: 600; overflow-wrap: anywhere; }
  .chips { display: flex; flex-wrap: wrap; gap: 4px; margin: 6px 0; }
  .chip { font-size: 12px; padding: 1px 7px; border-radius: 6px; background: var(--sunk); border: 1px solid var(--line); color: var(--muted); cursor: help; }
  .chip.good { color: var(--keep); border-color: color-mix(in srgb, var(--keep) 40%, transparent); }
  .chip.warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 50%, transparent); }
  .mcell { border-left: 1px solid var(--line); display: flex; flex-direction: column; gap: 10px; }
  .none { color: var(--muted); font-size: 13px; }

  .file { display: grid; grid-template-columns: 20px 1fr; gap: 10px; align-items: start; }
  .file > input { width: 18px; height: 18px; margin: 3px 0 0; cursor: pointer; }
  .f-top { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .f-name { font-size: 13px; overflow-wrap: anywhere; margin-top: 4px; }
  .f-meta { font-size: 12px; color: var(--muted); display: flex; flex-wrap: wrap; gap: 2px 10px; margin-top: 2px; }
  .lower { font-size: 12px; color: var(--remove); }
  .file.is-selected .f-name { text-decoration: line-through; color: var(--muted); }
  .file.is-selected .q { opacity: .5; }
  .src { cursor: help; text-decoration: underline dotted; text-underline-offset: 2px; }
  .time { color: var(--muted); font-size: 13px; }
  .play { width: 26px; height: 26px; border-radius: 50%; border: 1px solid var(--line); background: var(--panel); display: inline-grid; place-items: center; cursor: pointer; padding: 0; }
  .play svg { width: 11px; height: 11px; fill: currentColor; }
  .file.is-playing .play { background: var(--ink); color: var(--bg); border-color: var(--ink); }

  /* quality chip: the loud thing */
  .q { display: inline-flex; align-items: baseline; gap: 6px; padding: 2px 8px; border-radius: 6px; border: 1.5px solid currentColor; width: max-content; }
  .q b { font-size: 11px; font-weight: 700; letter-spacing: .04em; }
  .q span { font-size: 14px; font-weight: 650; }
  .q.hires { color: var(--hires); }
  .q.lossless { color: var(--lossless); }
  .q.lossy { color: var(--lossy); }

  mark { background: var(--diff); color: inherit; border-radius: 3px; padding: 0 1px; }

  .tagpanel { grid-column: 1 / -1; background: var(--sunk); border-bottom: 1px solid var(--line); padding: 12px 16px 14px !important; }
  .tp-head { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 8px; font-size: 13px; color: var(--muted); }
  .tp-head label { display: flex; gap: 6px; align-items: center; cursor: pointer; }
  .tp-scroll { overflow-x: auto; }
  table.tags { border-collapse: collapse; font-size: 13px; width: 100%; }
  table.tags th, table.tags td { text-align: left; padding: 5px 10px; border-bottom: 1px solid var(--line); vertical-align: top; overflow-wrap: anywhere; }
  table.tags thead th { color: var(--muted); font-weight: 600; }
  table.tags th[scope="row"] { color: var(--muted); font-weight: 500; white-space: nowrap; width: 1%; }
  table.tags tr.group th { padding-top: 12px; color: var(--ink); font-weight: 650; border-bottom: 0; }
  table.tags td.differs { background: color-mix(in srgb, var(--diff) 35%, transparent); }
  .missing { color: var(--muted); font-style: italic; }

  .expand { display: block; width: 100%; border: 0; border-top: 1px solid var(--line); background: var(--sunk); padding: 9px; cursor: pointer; color: var(--muted); font-size: 14px; }
  .expand:hover { color: var(--ink); }

  .more { margin-top: 18px; }
  .empty { margin-top: 40px; color: var(--muted); max-width: 64ch; }

  /* ---------- player + action bar ---------- */
  .dock { position: fixed; left: 0; right: 0; bottom: 0; background: var(--panel); border-top: 1px solid var(--line); z-index: 10; }
  .dock-inner { max-width: 1240px; margin: 0 auto; padding: 10px 20px; display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: center; }
  .player { border-bottom: 1px solid var(--line); }
  .p-info { min-width: 0; flex: 1 1 260px; }
  .p-info b { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .p-info span { font-size: 13px; color: var(--muted); display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .player audio { flex: 2 1 320px; height: 36px; }
  .count { margin-right: auto; }
  kbd { font: inherit; font-size: 12px; border: 1px solid var(--line); border-bottom-width: 2px; border-radius: 4px; padding: 0 5px; }

  .batch { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; margin-top: 12px; padding: 14px 16px; }
  .batch-head { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  .batch-head .when { font-weight: 650; margin-right: auto; }
  .batch details { margin-top: 8px; color: var(--muted); font-size: 13px; }
  .batch ul { margin: 8px 0 0; padding-left: 18px; }

  .toast { position: fixed; left: 50%; bottom: 150px; transform: translateX(-50%); background: var(--ink); color: var(--bg); padding: 10px 16px; border-radius: 10px; max-width: min(92vw, 600px); opacity: 0; transition: opacity .2s; pointer-events: none; z-index: 20; display: flex; gap: 14px; align-items: center; }
  .toast.show { opacity: 1; pointer-events: auto; }
  .toast button { border: 0; background: none; color: inherit; font-weight: 700; text-decoration: underline; cursor: pointer; padding: 0; }

  @media (max-width: 760px) {
    .mgrid { grid-template-columns: 1fr; min-width: 0; }
    .mhead .h-label, .m-num { display: none; }
    .ed, .mcell { border-left: 0; }
    .mcell::before { content: attr(data-label); font-size: 12px; font-weight: 700; color: var(--muted); }
    .album-side { align-items: start; }
    .search { min-width: 0; flex: 1; }
  }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Duplicate tracks</h1>
      <div class="lib" id="lib"></div>
    </div>
    <div class="tabs" role="tablist">
      <button role="tab" aria-selected="true" data-tab="dupes">Duplicates</button>
      <button role="tab" aria-selected="false" data-tab="quarantine">Quarantine</button>
    </div>
  </header>

  <section id="dupes-view">
    <div class="controls">
      <div class="modes" role="radiogroup" aria-label="How to match tracks">
        <label><input type="radio" name="mode" value="loose" checked><span>Artist and title</span></label>
        <label><input type="radio" name="mode" value="same-folder"><span>Same folder only</span></label>
        <label><input type="radio" name="mode" value="cross-folder"><span>Same album tags</span></label>
        <label id="nd-mode" title="Set NAVIDROME_URL, NAVIDROME_USER and NAVIDROME_PASSWORD to enable this"><input type="radio" name="mode" value="navidrome" disabled><span>Navidrome albums</span></label>
      </div>
      <button class="btn primary" id="scan">Scan library</button>
      <p class="mode-help" id="mode-help"></p>
    </div>
    <div class="status" id="status"></div>
    <div class="warnings" id="warnings"></div>

    <div class="toolbar" id="toolbar" hidden>
      <div class="sections" role="tablist" aria-label="Result sections">
        <button role="tab" data-sec="suggested">Suggested<span class="n" id="n-suggested"></span></button>
        <button role="tab" data-sec="manual">Review<span class="n" id="n-manual"></span></button>
        <button role="tab" data-sec="kept">Kept both<span class="n" id="n-kept"></span></button>
      </div>
      <span class="spacer"></span>
      <select class="reason-filter" id="reason" aria-label="Filter by reason"></select>
      <input class="search" id="search" type="search" placeholder="Filter by artist, album or track (press /)" aria-label="Filter">
      <button class="btn small" id="expand-all">Expand all</button>
      <button class="btn small" id="collapse-all">Collapse all</button>
    </div>
    <p class="section-help" id="section-help"></p>
    <div id="clusters"></div>
  </section>

  <section id="quarantine-view" hidden>
    <p class="status">Quarantined files sit in a hidden folder inside your library that Navidrome ignores. Restore puts them back where they were.</p>
    <div id="batches"></div>
  </section>
</main>

<div class="dock">
  <div class="player" id="player" hidden>
    <div class="dock-inner">
      <div class="p-info"><b id="p-title"></b><span id="p-sub"></span></div>
      <audio id="audio" controls preload="metadata"></audio>
      <button class="btn small" id="p-ab" title="Jump to the other copy at the same spot">Switch copy <kbd>B</kbd></button>
      <button class="btn small" id="p-close">Close</button>
    </div>
  </div>
  <div class="bar" id="bar" hidden>
    <div class="dock-inner">
      <span class="count" id="count"></span>
      <button class="btn" id="clear-all">Clear selection</button>
      <button class="btn primary" id="go">Quarantine selected</button>
    </div>
  </div>
</div>
<div class="toast" id="toast" role="status" aria-live="polite"><span id="toast-msg"></span><button id="toast-act" hidden></button></div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtBytes = b => b >= 1e9 ? (b / 1e9).toFixed(2) + " GB" : (b / 1e6).toFixed(1) + " MB";
const fmtTime = s => { s = Math.round(s); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const fmtDate = t => new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
const plural = (n, w, p) => `${n.toLocaleString()} ${n === 1 ? w : (p || w + "s")}`;
const base = p => p.split("/").pop();
const parent = p => p.split("/").slice(0, -1).join("/");
const LETTERS = "ABCDEFGH";
const PLAY = '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M2 1l7 4-7 4z"/></svg>';
const PAUSE = '<svg viewBox="0 0 10 10" aria-hidden="true"><path d="M2 1h2v8H2zM6 1h2v8H6z"/></svg>';

const HELP = {
  "loose": "Matches the same artist and title anywhere, including inside one folder. Catches reissues, deluxe editions, misspelled folders and stray copies. Tags decide what gets suggested.",
  "same-folder": "Only the same track twice inside one album folder, like a FLAC next to an AAC, or a curly versus straight apostrophe in the filename.",
  "cross-folder": "Only copies with identical artist, album, disc, track and title tags in different folders. Misses editions whose album names differ.",
  "navidrome": "Asks Navidrome which tracks it lists twice in one album, then reads those files' tags. Downloads since Navidrome's last scan won't appear.",
};
const SECTION_HELP = {
  suggested: "The tags make it clear which copies are redundant, so those are pre-selected and struck through. Open an album to check the evidence behind each track.",
  manual: "These need your ears. Nothing is selected. Play copies side by side, compare tags, then select what to remove or choose Keep both.",
  kept: "Albums you chose to keep. They stay out of the other lists on future scans until you show them again.",
};
const PAGE = 30;

const S = {
  clusters: [], scanId: null, selected: new Set(), section: "suggested",
  query: "", reason: "", limit: PAGE,
  open: new Map(), tags: new Set(), allFields: new Set(), player: null,
};

async function api(path, body) {
  const r = await fetch(path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  } : undefined);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `Request failed with status ${r.status}`);
  return data;
}

let toastTimer;
function toast(msg, action, onAction) {
  $("#toast-msg").textContent = msg;
  const b = $("#toast-act");
  b.hidden = !action;
  b.textContent = action || "";
  b.onclick = () => { $("#toast").classList.remove("show"); onAction(); };
  $("#toast").style.bottom = `${document.querySelector(".dock").offsetHeight + 14}px`;
  $("#toast").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("#toast").classList.remove("show"), action ? 9000 : 5000);
}

// ---------- word-level diff, so "of" vs "Of" or ' vs ’ stands out ----------

function tokens(s) { return String(s).match(/[\p{L}\p{N}]+|\s+|./gu) || []; }
function diffPair(a, b) {
  const A = tokens(a), B = tokens(b), n = A.length, m = B.length;
  const L = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const ra = [], rb = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) { ra.push([A[i++], 0]); rb.push([B[j++], 0]); }
    else if (L[i + 1][j] >= L[i][j + 1]) ra.push([A[i++], 1]);
    else rb.push([B[j++], 1]);
  }
  while (i < n) ra.push([A[i++], 1]);
  while (j < m) rb.push([B[j++], 1]);
  return [ra, rb];
}
function marked(parts) {
  let out = "", run = "", on = false;
  const flush = () => { if (run) out += on && run.trim() ? `<mark>${esc(run)}</mark>` : esc(run); run = ""; };
  for (const [t, d] of parts) { if (!!d !== on) { flush(); on = !!d; } run += t; }
  flush();
  return out;
}
// mine highlighted against other; plain if equal or nothing to compare
const diffHtml = (mine, other) => other == null || mine === other ? esc(mine) : marked(diffPair(mine, other)[0]);

// ---------- model helpers ----------

const sectionOf = c => c.ignored ? "kept" : c.kind;
const liveFiles = r => r.files.filter(f => !f.moved);
const liveRows = c => c.rows.map((r, ri) => ({ r, ri })).filter(x => liveFiles(x.r).length > 1);
const isOpen = c => S.open.has(c.key) ? S.open.get(c.key) : sectionOf(c) === "manual";
const sizes = () => new Map(S.clusters.flatMap(c => c.rows.flatMap(r => r.files.map(f => [f.rel, f.size]))));
const refEdition = c => Math.max(0, c.editions.findIndex(e => e.keep));

function edRole(c, e, i) {
  if (c.editions.length === 1) return ["Album folder", ""];
  if (e.keep) return ["Keep", "keep"];
  if (c.editions.some(x => x.keep)) return ["Remove matching tracks", "remove"];
  return [`Edition ${LETTERS[i]}`, ""];
}

function saveSel() {
  try { localStorage.setItem(`music-dupes:sel:${S.scanId}`, JSON.stringify([...S.selected])); } catch {}
}
function suggestedSel() {
  return new Set(S.clusters.filter(c => !c.ignored)
    .flatMap(c => c.rows.flatMap(r => liveFiles(r).filter(f => f.suggested).map(f => f.rel))));
}

// ---------- scan ----------

async function refresh() {
  const s = await api("/api/scan");
  renderStatus(s);
  $("#scan").disabled = s.status === "scanning";
  if (s.status === "scanning") { setTimeout(refresh, 800); return; }
  if (s.status !== "done") { S.clusters = []; renderAll(); return; }
  S.clusters = s.clusters;
  const live = new Set(S.clusters.flatMap(c => c.rows.flatMap(r => liveFiles(r).map(f => f.rel))));
  if (s.scan_id !== S.scanId) {
    S.scanId = s.scan_id;
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(`music-dupes:sel:${s.scan_id}`)); } catch {}
    S.selected = saved ? new Set(saved.filter(r => live.has(r))) : suggestedSel();
    S.limit = PAGE;
    const r = document.querySelector(`input[name=mode][value="${s.mode}"]`);
    if (r && !r.disabled) { r.checked = true; $("#mode-help").textContent = HELP[s.mode]; }
    if (!countIn("suggested") && countIn("manual")) S.section = "manual";
  } else {
    S.selected = new Set([...S.selected].filter(r => live.has(r)));
  }
  saveSel();
  renderAll();
}

function renderStatus(s) {
  const el = $("#status");
  $("#warnings").textContent = (s.warnings || []).join(" ");
  if (s.status === "idle") {
    el.textContent = "No scan yet. Pick how to match tracks, then scan your library.";
  } else if (s.status === "scanning") {
    const pct = s.total ? Math.round(100 * s.scanned / s.total) : 0;
    const what = s.phase === "Reading tags" && s.total
      ? `Reading tags, ${s.scanned.toLocaleString()} of ${s.total.toLocaleString()} files` : (s.phase || "Working");
    el.innerHTML = `${esc(what)}<div class="meter"><div style="width:${s.phase === "Reading tags" ? pct : 4}%"></div></div>`;
  } else if (s.status === "error") {
    el.textContent = `The scan stopped: ${s.error}`;
  } else {
    el.textContent = `Scanned ${plural(s.total, "file")} on ${new Date(s.finished * 1000).toLocaleString()}.` +
      (s.unreadable ? ` ${plural(s.unreadable, "file")} couldn't be read.` : "");
  }
}

// ---------- rendering ----------

const countIn = sec => S.clusters.filter(c => sectionOf(c) === sec && liveRows(c).length).length;

function matches(c) {
  if (S.reason && c.reason !== S.reason) return false;
  const q = S.query.trim().toLowerCase();
  if (!q) return true;
  return [c.artist, c.album, ...c.editions.map(e => e.folder), ...c.rows.map(r => r.title)]
    .join(" ").toLowerCase().includes(q);
}

function renderAll() {
  const any = S.clusters.some(c => liveRows(c).length);
  $("#toolbar").hidden = !any;
  for (const sec of ["suggested", "manual", "kept"]) {
    $(`#n-${sec}`).textContent = countIn(sec);
    $(`[data-sec="${sec}"]`).setAttribute("aria-selected", S.section === sec);
  }
  const reasons = [...new Set(S.clusters.filter(c => sectionOf(c) === S.section && liveRows(c).length).map(c => c.reason))].sort();
  if (!reasons.includes(S.reason)) S.reason = "";
  $("#reason").innerHTML = `<option value="">All reasons</option>` +
    reasons.map(r => `<option ${r === S.reason ? "selected" : ""}>${esc(r)}</option>`).join("");
  $("#section-help").textContent = any ? SECTION_HELP[S.section] : "";
  renderList();
  renderBar();
}

function renderList() {
  if (!S.scanId) { $("#clusters").innerHTML = ""; return; }
  const list = S.clusters.map((c, ci) => [c, ci])
    .filter(([c]) => sectionOf(c) === S.section && liveRows(c).length && matches(c));
  if (!list.length) {
    const empty = {
      suggested: "Nothing left to clean up here. Check Review for albums that need a decision.",
      manual: "Nothing needs a decision right now.",
      kept: "You haven't kept any albums yet. Choose Keep both on an album in Review to park it here.",
    };
    $("#clusters").innerHTML = `<p class="empty">${S.query || S.reason ? "Nothing matches that filter." : empty[S.section]}</p>`;
    return;
  }
  $("#clusters").innerHTML = list.slice(0, S.limit).map(([c, ci]) => cardHtml(c, ci)).join("") +
    (list.length > S.limit ? `<button class="btn more" data-act="more">Show ${Math.min(PAGE, list.length - S.limit)} more of ${list.length - S.limit}</button>` : "");
  afterRender($("#clusters"));
}

function afterRender(root) {
  root.querySelectorAll("input[data-indet='1']").forEach(i => { i.indeterminate = true; });
}

function cardHtml(c, ci) {
  const rows = liveRows(c), sec = sectionOf(c), open = isOpen(c), n = c.editions.length;
  const files = rows.flatMap(x => liveFiles(x.r));
  const sel = files.filter(f => S.selected.has(f.rel));
  const hasSuggest = files.some(f => f.suggested);
  return `<article class="album" data-ci="${ci}" aria-label="${esc(c.album)} by ${esc(c.artist)}">
    <div class="album-head">
      <div class="album-id">
        <h2>${esc(c.album)}</h2>
        <div class="by">${esc(c.artist)}</div>
        <span class="reason ${sec}">${esc(c.reason)}</span>
        <p class="detail">${esc(c.detail)}</p>
      </div>
      <div class="album-side">
        <div class="album-stats">
          <span>${plural(rows.length, "duplicate track")}</span>
          <span>${sel.length ? `${sel.length} selected, ${fmtBytes(sel.reduce((a, f) => a + f.size, 0))}` : "None selected"}</span>
        </div>
        <div class="album-actions">
          ${hasSuggest && sec !== "kept" ? `<button class="btn small" data-act="suggest">Select suggested</button>` : ""}
          ${sec !== "kept" ? `<button class="btn small" data-act="all">Select all removable</button>` : ""}
          <button class="btn small" data-act="none" ${sel.length ? "" : "disabled"}>Deselect all</button>
          ${sec === "kept"
            ? `<button class="btn small" data-act="unkeep">Show again</button>`
            : `<button class="btn small" data-act="keep">Keep both</button>`}
        </div>
      </div>
    </div>
    <div class="matrix">
      <div class="mgrid" style="--n:${n}">
        <div class="mhead" style="display:contents">
          <div class="h-label">#</div><div class="h-label">Track</div>
          ${c.editions.map((e, i) => edHtml(c, e, i, rows)).join("")}
        </div>
        ${open ? rows.map(({ r, ri }) => rowHtml(c, r, ri)).join("") : ""}
      </div>
    </div>
    <button class="expand" data-act="toggle" aria-expanded="${open}">${open ? "Hide tracks" : `Show ${plural(rows.length, "track")}`}</button>
  </article>`;
}

function edHtml(c, e, i, rows) {
  const ref = refEdition(c), n = c.editions.length;
  const other = n < 2 ? null : c.editions[i === ref ? (ref === 0 ? 1 : 0) : ref];
  const [role, cls] = edRole(c, e, i);
  const fs = rows.flatMap(x => liveFiles(x.r).filter(f => f.ed === i));
  const sel = fs.filter(f => S.selected.has(f.rel)).length;
  return `<div class="ed ${cls}">
    ${role ? `<div class="ed-role">${esc(role)}</div>` : ""}
    <div class="ed-name">${diffHtml(base(e.folder), other && base(other.folder))}</div>
    <div class="ed-path">${diffHtml(parent(e.folder), other && parent(other.folder))}</div>
    <div class="ed-meta">
      <span class="q ${e.tier}"><b>${esc(e.format)}</b><span>${esc(e.detail)}</span></span>
      <span>${e.year || "No year tag"}</span>
      <span>${plural(e.tracks, "track")} in folder</span>
      ${e.deluxe ? "<span>Deluxe or expanded</span>" : ""}
    </div>
    <div class="ed-meta"><span>${esc(e.source)}</span><span>Added ${fmtDate(e.added)}</span></div>
    ${n > 1 ? `<label class="ed-all"><input type="checkbox" data-ed="${i}" ${fs.length && sel === fs.length ? "checked" : ""}
       data-indet="${sel && sel < fs.length ? 1 : 0}"> Select all ${fs.length} from this edition</label>` : ""}
  </div>`;
}

function rowHtml(c, r, ri) {
  const live = liveFiles(r), id = `${c.key}|${ri}`, tagsOpen = S.tags.has(id);
  const cells = c.editions.map((e, i) => {
    const fs = live.filter(f => f.ed === i);
    const label = c.editions.length > 1 ? `Edition ${LETTERS[i]}: ${base(e.folder)}` : "";
    return `<div class="mcell" data-label="${esc(label)}">${fs.length
      ? fs.map(f => fileHtml(f, live.find(o => o !== f))).join("")
      : `<span class="none">Not in this edition</span>`}</div>`;
  }).join("");
  return `<div class="mrow" style="display:contents">
      <div class="m-num">${r.disc > 1 ? `${r.disc}-` : ""}${r.track ?? ""}</div>
      <div class="m-title">
        <div class="t">${esc(r.title)}</div>
        <div class="chips">${r.evidence.map(ch => `<span class="chip ${ch.tone}" title="${esc(ch.why)}">${esc(ch.text)}</span>`).join("")}</div>
        <button class="link" data-act="tags" data-ri="${ri}" aria-expanded="${tagsOpen}">${tagsOpen ? "Hide tags" : "Compare tags"}</button>
      </div>
      ${cells}
    </div>${tagsOpen ? tagPanel(c, r, ri, live) : ""}`;
}

function fileHtml(f, other) {
  const sel = S.selected.has(f.rel), playing = S.player && S.player.rel === f.rel;
  return `<div class="file${sel ? " is-selected" : ""}${playing ? " is-playing" : ""}">
    <input type="checkbox" data-rel="${esc(f.rel)}" ${sel ? "checked" : ""} aria-label="Remove ${esc(f.name)}">
    <div>
      <div class="f-top">
        <span class="q ${f.tier}"><b>${esc(f.format)}</b><span>${esc(f.detail)}</span></span>
        <button class="play" data-play="${esc(f.rel)}" aria-label="${playing ? "Pause" : "Play"} ${esc(f.name)}">${playing && !$("#audio").paused ? PAUSE : PLAY}</button>
        <span class="time">${fmtTime(f.secs)}</span>
        ${f.quality === "lower" ? `<span class="lower">Lower quality</span>` : ""}
      </div>
      <div class="f-name">${diffHtml(f.name, other && other.name)}</div>
      <div class="f-meta"><span class="src" title="${esc(f.source_why)}">${esc(f.source)}</span><span>Added ${fmtDate(f.mtime)}</span></div>
    </div>
  </div>`;
}

const TAG_ORDER = ["title", "artist", "albumartist", "album", "date", "originaldate", "tracknumber", "discnumber", "isrc", "label", "copyright", "genre", "encoder", "comment"];
const ALWAYS = new Set(["Format", "Length", "Audio MD5", "isrc"]);

function tagPanel(c, r, ri, live) {
  const id = `${c.key}|${ri}`, all = S.allFields.has(id);
  const tagKeys = [...new Set(live.flatMap(f => Object.keys(f.tags)))]
    .sort((a, b) => (TAG_ORDER.indexOf(a) + 1 || 99) - (TAG_ORDER.indexOf(b) + 1 || 99) || a.localeCompare(b));
  const groups = [["Audio", Object.keys(live[0].props), f => f.props], ["Tags", tagKeys, f => f.tags]];
  let hidden = 0;
  const body = groups.map(([name, keys, get]) => {
    const trs = keys.map(k => {
      const vals = live.map(f => get(f)[k] ?? null);
      const differs = new Set(vals.map(v => v ?? "")).size > 1;
      if (!all && !differs && !ALWAYS.has(k)) { hidden++; return ""; }
      return `<tr><th scope="row">${esc(k)}</th>${vals.map((v, i) => `<td class="${differs ? "differs" : ""}">${
        v == null || v === "" ? `<span class="missing">missing</span>`
        : differs ? diffHtml(v, vals[i === 0 ? 1 : 0] ?? "") : esc(v)}</td>`).join("")}</tr>`;
    }).join("");
    return trs ? `<tr class="group"><th colspan="${live.length + 1}">${name}</th></tr>${trs}` : "";
  }).join("");
  return `<div class="tagpanel">
    <div class="tp-head">
      <span>Highlighted cells differ between copies.${hidden && !all ? ` ${plural(hidden, "matching field")} hidden.` : ""}</span>
      <label><input type="checkbox" data-allfields="${ri}" ${all ? "checked" : ""}> Show matching fields</label>
    </div>
    <div class="tp-scroll"><table class="tags">
      <thead><tr><th></th>${live.map(f => `<th>${c.editions.length > 1 ? `Edition ${LETTERS[f.ed]}: ` : ""}${esc(f.name)}</th>`).join("")}</tr></thead>
      <tbody>${body || `<tr><td colspan="${live.length + 1}" class="missing">Every field matches.</td></tr>`}</tbody>
    </table></div>
  </div>`;
}

function rerenderCard(ci, focusSel) {
  const el = document.querySelector(`.album[data-ci="${ci}"]`);
  const c = S.clusters[ci];
  if (!el) return;
  if (sectionOf(c) !== S.section || !liveRows(c).length) { renderAll(); return; }
  el.outerHTML = cardHtml(c, ci);
  const fresh = document.querySelector(`.album[data-ci="${ci}"]`);
  afterRender(fresh);
  if (focusSel) fresh.querySelector(focusSel)?.focus();
  renderBar();
}

function renderBar() {
  const sz = sizes();
  const n = S.selected.size;
  const bytes = [...S.selected].reduce((a, r) => a + (sz.get(r) || 0), 0);
  const fromReview = S.clusters.filter(c => sectionOf(c) === "manual")
    .flatMap(c => c.rows.flatMap(liveFiles)).filter(f => S.selected.has(f.rel)).length;
  $("#bar").hidden = !S.clusters.some(c => liveRows(c).length) || !$("#quarantine-view").hidden;
  $("#count").textContent = n
    ? `${plural(n, "file")} selected, ${fmtBytes(bytes)}${fromReview ? `, including ${fromReview} you picked in Review` : ""}`
    : "Nothing selected";
  $("#go").disabled = !n;
  $("#clear-all").disabled = !n;
  $("#go").textContent = n ? `Quarantine ${plural(n, "file")}` : "Quarantine selected";
}

// ---------- selection ----------

function canSelect(r, rel) {
  const live = liveFiles(r);
  return live.filter(f => f.rel !== rel && !S.selected.has(f.rel)).length >= 1;
}

function selectWhere(c, pred) {
  let skipped = 0;
  for (const { r } of liveRows(c)) {
    for (const f of liveFiles(r)) {
      if (!pred(f) || S.selected.has(f.rel)) continue;
      if (canSelect(r, f.rel)) S.selected.add(f.rel); else skipped++;
    }
  }
  if (skipped) toast(`Skipped ${plural(skipped, "copy", "copies")} so one copy of every track stays.`);
}

$("#clusters").addEventListener("change", e => {
  const card = e.target.closest(".album");
  if (!card) return;
  const ci = +card.dataset.ci, c = S.clusters[ci];
  const t = e.target;
  if (t.dataset.rel !== undefined) {
    const rel = t.dataset.rel, r = c.rows.find(r => r.files.some(f => f.rel === rel));
    if (t.checked) {
      if (!canSelect(r, rel)) { t.checked = false; toast("Leave at least one copy of every track."); return; }
      S.selected.add(rel);
    } else S.selected.delete(rel);
    saveSel();
    rerenderCard(ci, `[data-rel="${CSS.escape(rel)}"]`);
  } else if (t.dataset.ed !== undefined) {
    const i = +t.dataset.ed;
    if (t.checked) selectWhere(c, f => f.ed === i);
    else liveRows(c).forEach(({ r }) => liveFiles(r).filter(f => f.ed === i).forEach(f => S.selected.delete(f.rel)));
    saveSel();
    rerenderCard(ci, `[data-ed="${i}"]`);
  } else if (t.dataset.allfields !== undefined) {
    const id = `${c.key}|${t.dataset.allfields}`;
    S.allFields.has(id) ? S.allFields.delete(id) : S.allFields.add(id);
    rerenderCard(ci, `[data-allfields="${t.dataset.allfields}"]`);
  }
});

$("#clusters").addEventListener("click", async e => {
  const playBtn = e.target.closest("[data-play]");
  if (playBtn) { togglePlay(playBtn.dataset.play); return; }
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  if (btn.dataset.act === "more") { S.limit += PAGE; renderList(); return; }
  const ci = +btn.closest(".album").dataset.ci, c = S.clusters[ci];
  const files = liveRows(c).flatMap(x => liveFiles(x.r));
  switch (btn.dataset.act) {
    case "toggle":
      S.open.set(c.key, !isOpen(c));
      rerenderCard(ci, "[data-act=toggle]");
      return;
    case "tags": {
      const id = `${c.key}|${btn.dataset.ri}`;
      S.tags.has(id) ? S.tags.delete(id) : S.tags.add(id);
      rerenderCard(ci, `[data-act=tags][data-ri="${btn.dataset.ri}"]`);
      return;
    }
    case "suggest":
      files.forEach(f => S.selected.delete(f.rel));
      files.filter(f => f.suggested).forEach(f => S.selected.add(f.rel));
      break;
    case "all": {
      // everything except the best copy of each track (keeper edition first)
      files.forEach(f => S.selected.delete(f.rel));
      const ref = c.editions.findIndex(e => e.keep);
      for (const { r } of liveRows(c)) {
        const live = liveFiles(r);
        const keep = (ref >= 0 && live.find(f => f.ed === ref && f.quality === "best"))
          || live.find(f => f.quality === "best") || live[0];
        live.filter(f => f !== keep).forEach(f => S.selected.add(f.rel));
      }
      break;
    }
    case "none":
      files.forEach(f => S.selected.delete(f.rel));
      break;
    case "keep":
    case "unkeep": {
      const kept = btn.dataset.act === "keep";
      try {
        await api("/api/keep-both", { key: c.key, kept });
        c.ignored = kept;
        if (kept) files.forEach(f => S.selected.delete(f.rel));
        saveSel();
        renderAll();
        toast(kept ? `Kept both copies of ${c.album}.` : `${c.album} is back in ${c.kind === "manual" ? "Review" : "Suggested"}.`,
          "Undo", async () => {
            await api("/api/keep-both", { key: c.key, kept: !kept });
            c.ignored = !kept;
            renderAll();
          });
      } catch (err) { toast(err.message); }
      return;
    }
  }
  saveSel();
  rerenderCard(ci, `[data-act="${btn.dataset.act}"]`);
});

$("#search").addEventListener("input", e => { S.query = e.target.value; S.limit = PAGE; renderList(); });
$("#reason").addEventListener("change", e => { S.reason = e.target.value; S.limit = PAGE; renderList(); });
document.querySelectorAll("[data-sec]").forEach(b => b.addEventListener("click", () => {
  S.section = b.dataset.sec; S.limit = PAGE; S.reason = ""; renderAll();
  window.scrollTo({ top: $("#toolbar").offsetTop - 4 });
}));
$("#expand-all").addEventListener("click", () => { S.clusters.forEach(c => S.open.set(c.key, true)); renderList(); });
$("#collapse-all").addEventListener("click", () => { S.clusters.forEach(c => S.open.set(c.key, false)); renderList(); });
$("#clear-all").addEventListener("click", () => {
  const prev = new Set(S.selected);
  S.selected.clear(); saveSel(); renderList(); renderBar();
  toast(`Cleared ${plural(prev.size, "selected file")}.`, "Undo", () => { S.selected = prev; saveSel(); renderList(); renderBar(); });
});

document.querySelectorAll("input[name=mode]").forEach(r =>
  r.addEventListener("change", () => { $("#mode-help").textContent = HELP[r.value]; }));

$("#scan").addEventListener("click", async () => {
  try {
    await api("/api/scan", { mode: document.querySelector("input[name=mode]:checked").value });
    refresh();
  } catch (err) { toast(err.message); }
});

$("#go").addEventListener("click", async () => {
  const btn = $("#go");
  btn.disabled = true;
  btn.textContent = "Moving files";
  if (S.player && S.selected.has(S.player.rel)) closePlayer();
  try {
    const r = await api("/api/quarantine", { paths: [...S.selected] });
    const msg = `Quarantined ${plural(r.moved, "file")}, ${fmtBytes(r.bytes)}.` +
      (r.errors.length ? ` ${plural(r.errors.length, "problem")}: ${r.errors[0]}` : "");
    await refresh();
    if (r.moved) toast(msg, "Undo", async () => {
      try {
        const u = await api("/api/restore", { batch: r.batch });
        await refresh();
        toast(`Restored ${plural(u.restored, "file")}.`);
      } catch (err) { toast(err.message); }
    });
    else toast(msg);
  } catch (err) {
    toast(err.message);
    renderBar();
  }
});

// ---------- player ----------

const audio = $("#audio");

function findFile(rel) {
  for (const c of S.clusters) for (const r of c.rows) {
    const f = r.files.find(f => f.rel === rel);
    if (f) return { c, r, f };
  }
  return null;
}

function togglePlay(rel) {
  if (S.player && S.player.rel === rel) {
    audio.paused ? audio.play() : audio.pause();
    return;
  }
  load(rel, 0, true);
}

function load(rel, at, autoplay) {
  const hit = findFile(rel);
  if (!hit) return;
  S.player = { rel };
  audio.src = `/api/audio?rel=${encodeURIComponent(rel)}`;
  audio.onloadedmetadata = () => {
    if (at) audio.currentTime = Math.min(at, audio.duration || at);
    if (autoplay) audio.play().catch(() => {});
  };
  const { c, r, f } = hit;
  $("#p-title").textContent = `${r.title}, ${c.artist}`;
  $("#p-sub").textContent = `${c.editions.length > 1 ? `Edition ${LETTERS[f.ed]}, ` : ""}${f.format} ${f.detail}, ${f.rel}`;
  $("#p-ab").disabled = liveFiles(r).length < 2;
  $("#player").hidden = false;
  syncPlayButtons();
}

function switchCopy() {
  if (!S.player) return;
  const hit = findFile(S.player.rel);
  const live = liveFiles(hit.r);
  const next = live[(live.findIndex(f => f.rel === S.player.rel) + 1) % live.length];
  load(next.rel, audio.currentTime, !audio.paused);
}

function closePlayer() {
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  S.player = null;
  $("#player").hidden = true;
  syncPlayButtons();
}

function syncPlayButtons() {
  document.querySelectorAll("[data-play]").forEach(b => {
    const on = S.player && b.dataset.play === S.player.rel;
    b.closest(".file").classList.toggle("is-playing", !!on);
    b.innerHTML = on && !audio.paused ? PAUSE : PLAY;
  });
}
audio.addEventListener("play", syncPlayButtons);
audio.addEventListener("pause", syncPlayButtons);
audio.addEventListener("error", () => { if (S.player) toast("This browser can't play that file, or it has moved."); });
$("#p-ab").addEventListener("click", switchCopy);
$("#p-close").addEventListener("click", closePlayer);

document.addEventListener("keydown", e => {
  if (e.target.matches("input[type=search], input[type=text], select, textarea")) return;
  if (e.key === "/") { e.preventDefault(); $("#search").focus(); }
  else if ((e.key === "b" || e.key === "B") && S.player) { e.preventDefault(); switchCopy(); }
  else if (e.key === "Escape" && S.player) closePlayer();
});

// ---------- quarantine tab ----------

async function loadBatches() {
  const batches = await api("/api/quarantine");
  $("#batches").innerHTML = batches.length ? batches.map(b => `
    <div class="batch">
      <div class="batch-head">
        <span class="when">${new Date(b.created * 1000).toLocaleString()}</span>
        <span>${plural(b.files.length, "file")}, ${fmtBytes(b.bytes)}</span>
        <button class="btn" data-restore="${b.batch}">Restore</button>
        <button class="btn danger" data-purge="${b.batch}" data-n="${b.files.length}" data-bytes="${b.bytes}">Delete permanently</button>
      </div>
      <details><summary>Show files</summary>
        <ul>${b.files.map(f => `<li>${esc(f.quality)}, ${esc(f.rel)}</li>`).join("")}</ul>
      </details>
    </div>`).join("") : `<p class="empty">Quarantine is empty.</p>`;
}

$("#batches").addEventListener("click", async e => {
  const restore = e.target.dataset.restore, purgeId = e.target.dataset.purge;
  try {
    if (restore) {
      const r = await api("/api/restore", { batch: restore });
      toast(`Restored ${plural(r.restored, "file")}.` +
        (r.errors.length ? ` ${plural(r.errors.length, "file")} stayed in quarantine: ${r.errors[0]}` : ""));
      refresh();
    } else if (purgeId) {
      const { n, bytes } = e.target.dataset;
      if (!confirm(`Permanently delete ${plural(+n, "file")} (${fmtBytes(+bytes)})? Restore won't be possible after this.`)) return;
      const r = await api("/api/purge", { batch: purgeId });
      toast(`Deleted ${fmtBytes(r.bytes)} permanently.`);
    } else return;
    loadBatches();
  } catch (err) { toast(err.message); }
});

document.querySelectorAll("[data-tab]").forEach(t => t.addEventListener("click", () => {
  document.querySelectorAll("[data-tab]").forEach(x => x.setAttribute("aria-selected", x === t));
  const q = t.dataset.tab === "quarantine";
  $("#dupes-view").hidden = q;
  $("#quarantine-view").hidden = !q;
  renderBar();
  if (q) loadBatches();
}));

// ---------- boot ----------

$("#mode-help").textContent = HELP.loose;
api("/api/info").then(i => {
  $("#lib").textContent = `Library at ${i.music}`;
  if (i.navidrome) {
    $("#nd-mode input").disabled = false;
    $("#nd-mode").title = i.navidrome === "api" ? "Uses Navidrome's API" : "Uses a snapshot of Navidrome's database";
  }
}).catch(() => {});
refresh().catch(err => toast(err.message));
</script>
</body>
</html>
````

## Appendix D: tests/mkfix.py

````python
import subprocess, os
B="/tmp/t2/music"
FL=["-c:a","flac"]; HR=["-c:a","flac","-sample_fmt","s32","-ar","48000"]; AAC=["-c:a","aac","-b:a","256k"]
def mk(freq, codec, dur, path, title, artist, aa, album, date, track, isrc=None):
    p=os.path.join(B,path); os.makedirs(os.path.dirname(p),exist_ok=True)
    md=dict(title=title,artist=artist,album_artist=aa,album=album,date=date,track=str(track))
    if isrc: md["ISRC"]=isrc
    args=["ffmpeg","-loglevel","error","-y","-f","lavfi","-i",f"sine=f={freq}:d={dur}"]+codec
    for k,v in md.items(): args+=["-metadata",f"{k}={v}"]
    subprocess.run(args+[p],check=True)
for y in (2000,2007):
    for t in (1,2): mk(400+t,FL,5,f"Blindside/A Thought Crushed My Mind ({y})/0{t} Song{t}.flac",f"Song{t}","Blindside","Blindside","A Thought Crushed My Mind",str(y),t,f"USAAA0000{t}")
for t in (1,2): mk(500+t,FL,5,f"Kutless/Hearts of the Innocent (2006)/0{t} T{t}.flac",f"T{t}","Kutless","Kutless","Hearts of the Innocent","2006",t)
for t in (1,2,3): mk(500+t,FL,5,f"Kutless/Hearts Of The Innocent (Special Edition) (2006)/0{t} T{t}.flac",f"T{t}","Kutless","Kutless","Hearts Of The Innocent (Special Edition)","2006",t)
mk(600,HR,5,"Kings Kaleidoscope Hymns/Asaph's Arrows II (2025)/01 Grace.flac","Grace","Kings Kaleidoscope","Kings Kaleidoscope Hymns","Asaph's Arrows II","2025",1,"USKK1")
mk(600,FL,5,"Kings Kaleidoscope/Asaph's Arrows II (2025)/01 Grace.flac","Grace","Kings Kaleidoscope","Kings Kaleidoscope","Asaph's Arrows II","2025",1,"USKK1")
mk(700,HR,5,"Chris Renzema/Manna (2023)/01 Narrow Road.flac","Narrow Road","Chris Renzema","Chris Renzema","Manna","2023",1,"USRZ1")
mk(700,FL,5,"Chris Renzema/Manna (2024)/01 Narrow Road.flac","Narrow Road","Chris Renzema","Chris Renzema","Manna","2024",1,"USRZ1")
mk(800,FL,9,"Lynyrd Skynyrd/Pronounced (1973)/08 Free Bird.flac","Free Bird","Lynyrd Skynyrd","Lynyrd Skynyrd","Pronounced","1973",8)
mk(800,AAC,16,"Lynyrd Skynyrd/pronounced (1973)/08 Free Bird.m4a","Free Bird","Lynyrd Skynyrd","Lynyrd Skynyrd","pronounced","1973",8)
mk(900,FL,5,"Josh Garrels/Love & War (2011)/03 Farther Along.flac","Farther Along","Josh Garrels","Josh Garrels","Love & War","2011",3,"USGA1")
mk(901,FL,5,"Josh Garrels/Love & War (2024)/03 Farther Along.flac","Farther Along","Josh Garrels","Josh Garrels","Love & War","2024",3,"USGA2")
mk(1000,FL,5,"Gable Price/Fractured Heart (2020)/01 Heretic.flac","Heretic","Gable Price","Gable Price","Fractured Heart","2020",1)
mk(1001,FL,5,"Gable Price/Fractioned Heart (2020)/01 Heretic.flac","Heretic","Gable Price","Gable Price","Fractioned Heart","2020",1)
mk(1100,FL,5,"Flyleaf/Flyleaf (2005)/01 I'm So Sick.flac","I'm So Sick","Flyleaf","Flyleaf","Flyleaf","2005",1)
mk(1100,FL,5,"Flyleaf/Flyleaf (2005)/01 I\u2019m So Sick.flac","I\u2019m So Sick","Flyleaf","Flyleaf","Flyleaf","2005",1)
mk(1200,FL,5,"Band/Album (2010)/02 Two.flac","Two","Band","Band","Album","2010",2)
mk(1200,AAC,5,"Band/Album (2010)/02 Two.m4a","Two","Band","Band","Album","2010",2)
mk(1300,HR,5,"Hres/Rec (2015)/01 X.flac","X","Hres","Hres","Rec","2015",1,"USHR1")
mk(1300,FL,5,"Hres/Rec (Deluxe) (2015)/01 X.flac","X","Hres","Hres","Rec (Deluxe)","2015",1,"USHR1")
mk(1301,FL,5,"Hres/Rec (Deluxe) (2015)/02 Y.flac","Y","Hres","Hres","Rec (Deluxe)","2015",2)
````

## Appendix E: tests/check_classify.py

Usage: `python3 tests/check_classify.py loose same-folder`

````python
import time, sys, os
os.environ.update(MUSIC_DIR="/tmp/t2/music", CONFIG_DIR="/tmp/t2/config")
sys.path.insert(0, "/mnt/user-data/outputs/music-dupes")
import app
from fastapi.testclient import TestClient
c=TestClient(app.app)
def scan(mode):
    c.post('/api/scan',json={'mode':mode})
    while c.get('/api/scan').json()['status']=='scanning': time.sleep(.05)
    return c.get('/api/scan').json()
for mode in sys.argv[1:]:
    s=scan(mode); print(f"== {mode}", s['status'], s['error'] or "")
    for cl in s['clusters']:
        keep=[os.path.basename(e['folder']) for e in cl['editions'] if e['keep']]
        sel=[f['name']+" @ "+os.path.basename(os.path.dirname(f['rel'])) for r in cl['rows'] for f in r['files'] if f['suggested']]
        print(f"  {cl['artist'][:16]:16} {cl['kind']:9} {cl['reason']:42} keep={keep} sel={sel}")
````

## Appendix F: tests/serve.py

````python
import os, sys
os.environ.update(MUSIC_DIR="/tmp/t2/music", CONFIG_DIR="/tmp/t2/config")
sys.path.insert(0, "/mnt/user-data/outputs/music-dupes")
import uvicorn, app
uvicorn.run(app.app, host="127.0.0.1", port=8095, log_level="warning")
````

## Appendix G: tests/e2e.py

Requires the server from serve.py running on :8095. Usage: `python3 tests/e2e.py light` or `dark`.

````python
import asyncio, sys
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width":1280,"height":1000}, color_scheme=sys.argv[1] if len(sys.argv)>1 else "light")
        errs=[]; pg.on("pageerror", lambda e: errs.append(str(e))); pg.on("console", lambda m: m.type=="error" and errs.append(m.text))
        await pg.goto("http://127.0.0.1:8095/")
        await pg.click("#scan"); await pg.wait_for_selector(".album", timeout=20000)
        await pg.click("#expand-all")
        await pg.screenshot(path="/tmp/s1.png", full_page=True)
        await pg.click("[data-sec=manual]"); await pg.wait_for_timeout(200)
        # open tags on first review row, play a file
        await pg.click(".album [data-act=tags]"); await pg.click(".album [data-play]"); await pg.wait_for_timeout(500)
        await pg.screenshot(path="/tmp/s2.png", full_page=True)
        # edition select-all on Kings Kaleidoscope card, then guard
        kk = pg.locator(".album", has_text="Kings Kaleidoscope")
        await kk.locator("input[data-ed='0']").check(); await pg.wait_for_timeout(100)
        n1 = await kk.locator("input[data-rel]:checked").count()
        await kk.locator("input[data-ed='1']").click(); await pg.wait_for_timeout(100)
        n2 = await kk.locator("input[data-rel]:checked").count()
        toast = await pg.text_content("#toast-msg")
        print("kk selected", n1, "after 2nd edition", n2, "| toast:", toast)
        # keep both on Josh Garrels -> moves to kept
        await pg.locator(".album", has_text="Josh Garrels").locator("[data-act=keep]").click(); await pg.wait_for_timeout(300)
        print("kept count", await pg.text_content("#n-kept"), "review count", await pg.text_content("#n-manual"))
        await pg.click("[data-sec=suggested]"); await pg.wait_for_timeout(100)
        print("bar:", await pg.text_content("#count"))
        await pg.click("#go"); await pg.wait_for_timeout(800)
        print("toast:", await pg.text_content("#toast-msg"), "| suggested left:", await pg.text_content("#n-suggested"))
        await pg.click("#toast-act"); await pg.wait_for_timeout(800)
        print("after undo toast:", await pg.text_content("#toast-msg"), "| suggested:", await pg.text_content("#n-suggested"))
        await pg.set_viewport_size({"width":390,"height":900}); await pg.click("[data-sec=manual]"); await pg.wait_for_timeout(200)
        await pg.screenshot(path="/tmp/s3.png", full_page=False)
        print("errors:", errs)
        await b.close()
asyncio.run(main())
````

## Appendix H: scripts/music_dupes.py (original standalone CLI, superseded by the web app)

````python
#!/usr/bin/env python3
"""
music_dupes.py - find duplicate tracks in a music library. Read-only.

  same-folder  : files in one directory that are the same track
                 (e.g. FLAC 24/96 next to an AAC 320k, or "Song (1).flac")
  cross-folder : files anywhere whose tags match, spread across 2+ folders
  --loose      : cross-folder on artist + title only (singles vs album,
                 compilations, deluxe editions). Noisier, eyeball durations.

Within each group the highest quality file is marked KEEP. Nothing is
modified or deleted.
"""
import argparse, csv, os, re, sys, unicodedata
from collections import defaultdict
from mutagen import File as MFile

AUDIO_EXT = {".flac", ".m4a", ".mp3", ".ogg", ".opus", ".aac",
             ".wav", ".aiff", ".aif", ".wma"}
LOSSLESS_EXT = {"flac", "wav", "aiff", "aif"}


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return re.sub(r"[^\w]+", " ", s).strip()


def num(s):
    m = re.match(r"\s*(\d+)", s or "")  # "3/12" -> 3
    return int(m.group(1)) if m else None


def read(path):
    try:
        a = MFile(path, easy=True)
    except Exception as e:
        print(f"! unreadable: {path} ({e})", file=sys.stderr)
        return None
    if a is None:
        return None
    t = a.tags or {}
    g = lambda k: (t.get(k) or [""])[0]
    info = a.info
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    codec = (getattr(info, "codec", "") or ext).lower()
    lossless = ext in LOSSLESS_EXT or codec == "alac"
    return dict(
        path=path, folder=os.path.dirname(path), ext=ext, codec=codec,
        lossless=lossless,
        bits=getattr(info, "bits_per_sample", 0) or 0,
        rate=getattr(info, "sample_rate", 0) or 0,
        kbps=(getattr(info, "bitrate", 0) or 0) // 1000,
        secs=round(getattr(info, "length", 0) or 0),
        title=g("title"), artist=g("artist"), albumartist=g("albumartist"),
        album=g("album"), track=num(g("tracknumber")),
        disc=num(g("discnumber")) or 1, size=os.path.getsize(path),
    )


def label(f):
    if f["lossless"]:
        name = "ALAC" if f["codec"] == "alac" else f["ext"].upper()
        return f"{name} {f['bits']}/{f['rate'] / 1000:g}"
    name = "AAC" if f["ext"] in ("m4a", "aac") else f["ext"].upper()
    return f"{name} {f['kbps']}k"


def score(f):
    # lossless beats lossy; then bit depth, sample rate; bitrate breaks ties
    ll = f["lossless"]
    return (ll, f["bits"] if ll else 0, f["rate"] if ll else 0, f["kbps"])


def same_folder_key(f):
    title = norm(f["title"])
    if not title:  # untagged: fall back to filename minus " (1)" style suffixes
        stem = os.path.splitext(os.path.basename(f["path"]))[0]
        title = norm(re.sub(r"\s*\(\d+\)$", "", stem))
    return (f["folder"], f["disc"], f["track"], title)


def cross_key(f, loose):
    if loose:
        return (norm(f["artist"]), norm(f["title"]))
    artist = norm(f["albumartist"] or f["artist"])
    return (artist, norm(f["album"]), f["disc"], f["track"], norm(f["title"]))


def scan(root):
    for d, _, files in os.walk(root):
        for n in files:
            if n.startswith("._"):
                continue
            if os.path.splitext(n)[1].lower() in AUDIO_EXT:
                f = read(os.path.join(d, n))
                if f:
                    yield f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--mode", choices=["same-folder", "cross-folder"],
                    default="same-folder")
    ap.add_argument("--loose", action="store_true",
                    help="cross-folder: match on artist+title only")
    ap.add_argument("--csv", help="also write results to this CSV path")
    args = ap.parse_args()

    groups, scanned = defaultdict(list), 0
    for f in scan(args.root):
        scanned += 1
        k = (same_folder_key(f) if args.mode == "same-folder"
             else cross_key(f, args.loose))
        groups[k].append(f)

    dupes = [g for g in groups.values() if len(g) > 1 and (
        args.mode == "same-folder" or len({f["folder"] for f in g}) > 1)]

    rows, reclaim = [], 0
    for gid, g in enumerate(sorted(dupes, key=lambda g: g[0]["path"]), 1):
        g.sort(key=score, reverse=True)
        best = g[0]
        print(f"\n[{gid}] {best['artist']} - {best['title']}  ({best['album']})")
        for i, f in enumerate(g):
            verdict = ("KEEP" if i == 0
                       else "same" if score(f) == score(best) else "lower")
            if i:
                reclaim += f["size"]
            print(f"  {verdict:<5} {label(f):<14} {f['secs']:>5}s  {f['path']}")
            rows.append(dict(group=gid, verdict=verdict, quality=label(f),
                             secs=f["secs"], size_mb=round(f["size"] / 1e6, 1),
                             artist=f["artist"], album=f["album"],
                             title=f["title"], path=f["path"]))

    if args.csv and rows:
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)

    print(f"\nScanned {scanned} files, {len(dupes)} duplicate groups, "
          f"~{reclaim / 1e9:.2f} GB in non-KEEP copies.", file=sys.stderr)


if __name__ == "__main__":
    main()
````
