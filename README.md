# music-dupes

A self-hosted web app that finds duplicate tracks in your music library, explains *why* it thinks they're duplicates using the raw tags and audio stream data, and lets you quarantine the extra copies in one click (with undo).

It's built for libraries that get fed by several pipelines at once (Lidarr, Tidarr, slskd, Usenet, torrents, old iTunes purchases) and end up with the same album three times under slightly different names.

- **Explains every match.** Same ISRC, identical FLAC audio checksum, track lengths, release years, and a side-by-side tag diff for each copy.
- **Suggests only what it can prove.** Deluxe over standard, hi-res over CD quality, lossless over lossy. Anything it can't confirm goes to a Review list with nothing pre-selected.
- **Inline player** with a "Switch copy" key so you can A/B two versions at the same timestamp.
- **Quarantine, not delete.** Files are renamed into a hidden folder inside your library. Restore any batch later; permanent deletion is a separate, explicit step.
- **One scan finds everything:** the same song anywhere in the library, the same album track even when its credits differ, and stray copies in one folder, tagged or not.
- **Knows where files came from:** Lidarr's import and retag history, Tidarr downloads, iTunes purchases, plus your own rules.
- Small: one Python file, one HTML file, no database server, no frontend build.

> **No authentication.** Keep it on your LAN. If you put it behind a reverse proxy, add an access list or basic auth there. See [Reverse proxy](#reverse-proxy).

---

## Contents

- [Quick start](#quick-start)
- [Install options](#install-options)
  - [Docker Compose](#docker-compose)
  - [docker run](#docker-run)
  - [TrueNAS SCALE](#truenas-scale)
  - [Unraid, Synology, Portainer and friends](#unraid-synology-portainer-and-friends)
  - [Build the image yourself](#build-the-image-yourself)
  - [No image at all](#no-image-at-all)
- [Configuration](#configuration)
- [Acoustic fingerprints](#acoustic-fingerprints)
- [Optional integrations](#optional-integrations)
- [Using it](#using-it)
- [How it decides](#how-it-decides)
- [Reverse proxy](#reverse-proxy)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Publishing your own image](#publishing-your-own-image)

---

## Quick start

```bash
git clone https://github.com/mikestecker/docker-music-dupes.git
cd docker-music-dupes
cp .env.example .env
# edit .env: set MUSIC_PATH, and PUID/PGID to the owner of your music files
docker compose up -d
```

Open `http://<your-server>:8095` and hit **Scan library**.

The first scan reads tags from every audio file, so on a big library over spinning disks expect it to take a few minutes. Results are cached in `/config/tags.db` keyed on path, size and modification time, so later scans only read files that changed.

---

## Install options

The image is published to GitHub Container Registry for `linux/amd64` and `linux/arm64`:

```
ghcr.io/mikestecker/docker-music-dupes:latest
```

Release tags (`:1.2.3`, `:1.2`, `:1`) and per-commit tags (`:sha-abc1234`) are published too, if you'd rather pin.

Whichever way you install, there are two mounts that matter:

| Container path | What to mount | Mode |
|---|---|---|
| `/music` | Your music library, as **one** mount | read-write |
| `/config` | Somewhere for app state (a few MB) | read-write |

The library must be a single mount. Quarantine works by renaming files into `/music/.dupe-quarantine/`, which is instant and atomic inside one filesystem. If you split the library across several mounts, moves turn into slow cross-device copies.

### Docker Compose

This is the recommended way for most setups. [`compose.yaml`](compose.yaml) reads everything from a `.env` file:

```bash
cp .env.example .env
```

```dotenv
MUSIC_PATH=/mnt/storage/music
CONFIG_PATH=./config
PORT=8095
PUID=1000
PGID=1000
TZ=America/Los_Angeles
```

Find the right `PUID`/`PGID` with `ls -ln /mnt/storage/music` (the third and fourth columns) or `id -u youruser`. The container runs as that user, so quarantined and restored files keep the same ownership as the rest of your library.

```bash
docker compose up -d            # pull the published image and start
docker compose logs -f          # watch it start
docker compose up -d --build    # or build from this checkout instead
```

### docker run

```bash
docker run -d \
  --name music-dupes \
  --restart unless-stopped \
  --user 1000:1000 \
  -p 8095:8095 \
  -e TZ=America/Los_Angeles \
  -v /path/to/config:/config \
  -v /path/to/music:/music \
  ghcr.io/mikestecker/docker-music-dupes:latest
```

Add any of the [optional variables](#configuration) with more `-e` flags.

### TrueNAS SCALE

TrueNAS Custom Apps don't read `.env` files (every `${VAR}` turns into an empty string), so there's a separate file with everything inlined: [`deploy/truenas.yaml`](deploy/truenas.yaml).

1. Create a dataset or folder for state and give it to the user that owns your music. For example, with a service account `svc_apps` (UID/GID 3000):

   ```bash
   sudo mkdir -p /mnt/ssd-pool/apps/music-dupes/config
   sudo chown -R svc_apps:svc_apps /mnt/ssd-pool/apps/music-dupes
   ```

2. Open `deploy/truenas.yaml`, change the host paths, the `user:` line, and the Lidarr values (or delete those lines if you don't use Lidarr).
3. In the TrueNAS UI go to **Apps → Discover Apps → ⋮ → Install via YAML**, name it `music-dupes` and paste the file.
4. Open `http://<nas-ip>:8095`.

Separate TrueNAS apps can't reach each other by container name, so use the NAS IP for `LIDARR_URL`.

If your pool has periodic snapshots, permanently deleted files keep using space until the snapshots holding them expire. That's expected.

### Unraid, Synology, Portainer and friends

Any UI that can run a container works. Use these settings:

| Setting | Value |
|---|---|
| Image | `ghcr.io/mikestecker/docker-music-dupes:latest` |
| Port | container `8095` to whatever host port you like |
| Path | your music share to `/music` (read-write) |
| Path | an appdata folder to `/config` (read-write) |
| User | the UID:GID that owns your music. Unraid shares are usually `99:100` (nobody:users). Synology is often `1026:100`. |
| Variables | `TZ`, plus any [optional ones](#configuration) |

In Unraid, set the user under **Advanced View → Extra Parameters** as `--user 99:100`. In Portainer, paste `compose.yaml` into a stack and fill in the environment variables in the stack editor (Portainer stacks do support them).

### Build the image yourself

```bash
git clone https://github.com/mikestecker/docker-music-dupes.git
cd docker-music-dupes
docker build -t music-dupes .
```

Then use `music-dupes` as the image name in any of the options above, or set `IMAGE=music-dupes` in `.env`. For another architecture, use buildx:

```bash
docker buildx build --platform linux/arm64 -t music-dupes --load .
```

### No image at all

If you can't pull from a registry, or you want to edit `app.py` on the server and just restart, [`deploy/compose.no-build.yaml`](deploy/compose.no-build.yaml) runs the stock `python:3.12-slim` image against the two source files and installs the three dependencies at startup (about 10 seconds):

```bash
mkdir -p /srv/music-dupes/{app,config}
cp app/app.py app/index.html /srv/music-dupes/app/
docker compose -f deploy/compose.no-build.yaml up -d
```

Updating is copying the two files over again and restarting the container.

### Without Docker

It's a plain FastAPI app, so this works too (Python 3.12 recommended):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cd app
MUSIC_DIR=/path/to/music CONFIG_DIR=/path/to/config uvicorn app:app --host 0.0.0.0 --port 8095
```

---

## Configuration

Everything is set with environment variables. Only the mounts are required.

| Variable | Default | What it does |
|---|---|---|
| `TZ` | `UTC` | Timezone for timestamps and quarantine batch names |
| `PREFER_SOURCES` | `Tidarr` | When copies are otherwise equal, keep the one whose Source label contains the first of these (comma-separated, e.g. `Tidarr,Qobuz`). Matches "Tidarr" and "Tidarr (SABnzbd) via Lidarr" alike. Empty turns it off. |
| `PREFER_REMASTERS` | `true` | When editions of one album are otherwise equal, keep the one named "Remaster(ed)". Set `false` to prefer originals (many remasters are louder and more compressed). |
| `FINGERPRINT` | `report` | Acoustic fingerprints of every file in a duplicate group, over the full song. `report` shows what they found and what they'd change without acting on it, `on` lets them confirm and veto duplicates, `off` skips them. See [Acoustic fingerprints](#acoustic-fingerprints). |
| `FINGERPRINT_WORKERS` | CPUs the container may use | How many fpcalc processes run at once. The default reads the container's CPU limit (`cpus:`), not the host's core count. |
| `ALLOWED_HOSTS` | | Extra hostnames the UI answers to, comma-separated, e.g. `dupes.example.com`. IPs, single-word names (`truenas`) and `.local`/`.lan`/`.home.arpa`/`.internal` names always work. `*` turns the check off. See [Reverse proxy](#reverse-proxy). |
| `PORT` | `8095` | Port the app listens on **inside** the container. You usually change the host side of the port mapping instead. |
| `MUSIC_DIR` | `/music` | Library path inside the container |
| `QUARANTINE_DIR` | `$MUSIC_DIR/.dupe-quarantine` | Where quarantined files go. Keep it inside the music mount. The app refuses to start if it's the library itself or a parent of it. |
| `CONFIG_DIR` | `/config` | Where state is stored |
| `LIDARR_URL` | | Lidarr base URL, e.g. `http://192.168.1.10:8686` |
| `LIDARR_API_KEY` | | Lidarr API key (Settings → General → Security) |
| `LIDARR_MUSIC_ROOT` | `/data/media/music` | Library path **as Lidarr's container sees it** |

The `*_MUSIC_ROOT` variables matter when your apps mount the same library at different paths. If Lidarr sees a file at `/data/media/music/Artist/Album/01.flac` and this app sees it at `/music/Artist/Album/01.flac`, set `LIDARR_MUSIC_ROOT=/data/media/music` and it lines up.

### Files in `/config`

| File | What it is |
|---|---|
| `tags.db` | SQLite tag cache. Safe to delete; the next scan rebuilds it. |
| `quarantine.json` | What's in each quarantine batch, so restores know where files go back to |
| `kept-both.json` | Albums you marked "Keep both" |
| `sources.json` | Your rules for labelling where files came from (see below) |

---

## Acoustic fingerprints

After grouping duplicates by tags, the scan runs [Chromaprint](https://acoustid.org/chromaprint)'s `fpcalc` on every file in a group, over the whole song, and compares each pair. It's the same fingerprint AcoustID and MusicBrainz Picard use. It hears the audio, not the tags, so a hi-res FLAC and an MP3 of the same master match, and a Lidarr retag that stripped the ISRC doesn't matter.

Each track shows a quiet line with the result, like "Fingerprint match 99%, full length." What the comparisons can say:

- **Match:** the audio agrees over the full length. With `FINGERPRINT=on` that confirms the same recording, so the cluster can be suggested.
- **Edit:** the audio agrees, but only over part of the longer copy (a radio edit, a cut version).
- **Short passages differ:** the rest agrees, but a few seconds here and there don't, which is what muted or swapped words look like. Often a clean version.
- **Differs:** different audio, whatever the tags say.

Fingerprints only confirm or veto copies that were already grouped. They never pick the keeper (quality, edition and source rules still do), never override "different album artists" or "different albums", and can't tell remasters apart (that's what the FLAC checksums are for). Files shorter than about 10 seconds aren't compared.

**Report mode first.** The default, `FINGERPRINT=report`, shows the fingerprint lines and, on each card it would change, a note like "With fingerprints on, this would move to Suggested". Look through those on your library, then set `FINGERPRINT=on`.

**Cost.** Only files in duplicate groups are fingerprinted, and results are cached in `tags.db`, so later scans only fingerprint new or changed files. A full-length fingerprint takes well under a second per file on a modern CPU, longer for hi-res. The scan shows progress and a **Skip fingerprinting** button; skipped tracks are judged as if fingerprints were off. fpcalc runs at low priority, one process per CPU the container may use. To cap it, set `cpus:` (and `mem_limit:` if you like) on the container, or `FINGERPRINT_WORKERS`. Each fpcalc uses a few tens of MB of memory.

The published image includes fpcalc. The [no-image setup](#no-image-at-all) doesn't, so fingerprints are skipped there.

## Optional integrations

### Lidarr

Set `LIDARR_URL`, `LIDARR_API_KEY` and `LIDARR_MUSIC_ROOT`, and each file gets labelled with the download client Lidarr imported it from (say "slskd via Lidarr" or "SABnzbd via Lidarr"). It reads Lidarr's import history, so files renamed after import won't match.

### Your own source rules

For files Lidarr didn't import and the built-in labels don't catch (manual rips, store purchases, a folder you keep for one source), add rules to `/config/sources.json`. Rules run top to bottom, first match wins:

```json
{
  "rules": [
    {"label": "Qobuz", "tag": "comment", "pattern": "qobuz"},
    {"label": "Bandcamp", "tag": "path", "pattern": "^Bandcamp/"},
    {"label": "Old CD rips", "tag": "encoder", "pattern": "^EAC"}
  ]
}
```

- `tag` is a tag name as shown in the app's tag table (lowercase), or `path` to match the file path relative to the library. Leave it out to search every tag.
- `pattern` is a case-insensitive regular expression.

Easiest way to write one: open **Compare tags** on a file from that source, find a tag that's always set the same way, and match on it. Rules are read at the start of each scan, so no restart needed.

Built-in labels cover:

- **Tidarr**: Tidarr repackages Tidal's stream with ffmpeg, which leaves container tags behind (`compatible_brands=mp41dashcmfc`, `major_brand=iso8`). Nothing else in a typical pipeline writes those. If Lidarr later rewrites a file's tags, they're gone, so turn off Lidarr's tag writing if you want to keep that trail (and the ISRCs Tidarr writes).
- iTunes Store purchases, anything else with "tidal" in a tag, and MusicBrainz-tagged files.

With Lidarr connected, the app also reads Lidarr's **retag** history. A retag resets a file's modified date, so for those files the date isn't used to judge which download a copy came from, and the tag table shows what Lidarr changed.

---

## Using it

**Scanning**

There's one scan. Two files are treated as possible copies if any of these match:

| Match | Catches |
|---|---|
| Artist and title, anywhere | Reissues, deluxe editions, misspelled folders, copies under a different artist folder spelling |
| Album artist, album, disc, track and title | The same album track when the track credits differ (`Artist, Guest` vs `Artist`) |
| Folder and title (or filename when untagged) | Stray copies like `Song (1).flac`, tagged or not |

Matching casts a wide net on purpose. What reaches Suggested is decided by the evidence (checksums, ISRCs, lengths, editions), so loose matches like two different songs called "Intro" land in Review with nothing selected.

Earlier versions had separate match modes and a Navidrome mode. Navidrome reads the same files, so it never found anything a direct scan misses. If you still have `NAVIDROME_*` variables set, the app ignores them and says so in the scan warnings and the container log. You can delete them.

**Sections**

- **Suggested:** the app is confident. Redundant copies are already selected. Look it over and hit **Quarantine**.
- **Review:** the app can't decide (different artists, different recordings, can't confirm). Nothing is selected. Play copies side by side, compare tags, pick what to remove, or choose **Keep both**.
- **Other albums:** the same recording on different releases. They stay, nothing is selected, and you can still pick a copy by hand.
- **Kept both:** albums you've told it to leave alone. If a new copy shows up later, the album comes back for review.

**Keyboard:** `/` focuses search, `B` switches the player to the next copy at the same timestamp, `Esc` closes the player.

**Quarantine tab:** every batch you've quarantined, with **Restore** and **Delete permanently**.

---

## How it decides

The short version, in order of strength:

1. **Identical audio.** FLAC files store an MD5 of the decoded audio. Same MD5 means bit-identical audio, no matter how the tags or file names differ.
2. **Same ISRC** and lengths within 1.5 seconds means the same recording, even across formats.
3. **Same acoustic fingerprint** over the full length (with `FINGERPRINT=on`). For copies with no shared ISRC, like a Lidarr retag next to a Tidarr download.
4. **Same disc/track slot in one folder** with lengths within a second.

Then, per album pair:

- Different album artists → Review.
- Different albums (an album and a single, a compilation or a best-of that share a recording) → **Other albums**, nothing selected. Both releases stay whole. Album names are compared without edition wording (deluxe, remaster, special edition, a year), and close spellings like `Fractured Heart` / `Fractioned Heart` still count as one album.
- Any track with different ISRCs, or lengths more than 2.5s apart → Review.
- One copy tagged clean or edited (advisory tag, or "(Clean)" / "(Edited)" in the title) and another not → Review. They're different releases.
- With `FINGERPRINT=on`, any track whose fingerprints disagree → Review: different audio, a cut version, or short passages that differ (often a clean edit).
- One folder only holds tracks that are all in a more complete copy of the same album (say Tidarr grabbed three songs Lidarr already has) → suggest keeping the complete album. Every track must be within a second, and audio that's provably different is never covered.
- One edition is deluxe/expanded/special and the other isn't, and the deluxe one is at least as complete → suggest keeping the deluxe one.
- Every track proven identical or the same recording → suggest keeping the best quality, then the most tracks, then your preferred source, then tags Lidarr didn't rewrite, then a remastered edition (`PREFER_REMASTERS`), then the richest tags, then the earliest year. The card names the step that decided it. When the checksums show two different masters of the same recording, tracks get a "Different master" chip and the card asks you to listen first.
- Anything else → Review.

Last check: if the edition it would keep has a lower-quality copy of any track than the one it would remove (say a CD-quality deluxe vs a hi-res standard), the whole album gets bumped to Review. When that's a partial folder holding hi-res copies of a CD-quality album, the reason says so, and the better file gets a **Replace the album's copy** button: it moves into the album under the album copy's name and the lower-quality copy goes to quarantine. Restore (or Undo) puts both back.

Titles that differ only by a featured artist (`Rest` vs `Rest (with Samm Henshaw)`, `feat.`, `ft.`) count as the same track, with a "Credits differ" note.

**Which copy stays.** Within a folder the keeper is ranked by, in order:

1. Quality class (hi-res, then CD-quality lossless, then lossy).
2. Fit with the folder (below).
3. A filename that matches its title tag. `Queen Songs + human.flac` matches the title `Queen Songs / human.`; `Queen SongsHuman.flac` doesn't.
4. An ISRC, when another copy has none (Lidarr retags often strip them).
5. Your `PREFER_SOURCES` (Tidarr by default).
6. Tags Lidarr didn't rewrite.
7. Then exact sample rate, no ` (1)` in the name, more tags, older file.

The kept copy says why, from the first step that separated it from the other: "Kept: has an ISRC", "Kept: filename matches its title" and so on. Newer isn't better on its own: a re-download can carry a mangled name.

Within a folder, ties on quality go to the copy that fits in: named like the other tracks (`01-03 Title` vs `03 Title`), written in the same download batch, and with a filename that agrees with its own track tags. Each copy shows a line saying how it fits, and a later stray re-download gets marked "Doesn't match the folder". 44.1 kHz and 48 kHz at the same bit depth count as the same quality, so a stray at 48 kHz doesn't outrank the album's 44.1 kHz copy.

Quality ranking is lossless over lossy, then bit depth, then sample rate. FLAC bitrate is ignored on purpose, since it only reflects how compressible the audio is.

The rules live in `app/app.py` (`classify`, `row_evidence`, `keep_rank`), with a test for each in `tests/test_classify.py`.

### Safety guarantees

- It never removes every copy of a track. That's checked in the browser **and** on the server.
- Nothing is deleted without **Delete permanently** on a quarantine batch, with a confirmation.
- Only files under the library mount (and outside the quarantine folder) can be read, played or moved.
- Restore never overwrites a file that's come back in the meantime.
- The quarantine folder starts with a dot and contains an `.ndignore`, so Navidrome and most scanners skip it.
- Quarantine re-checks the disk first: if the copy being kept is gone or changed, or the file being moved isn't the one that was scanned, that track is skipped until you scan again.
- Symlinks are never treated as copies, and nothing is ever moved through a symlink.
- FLAC copies with the same audio checksum but much less audio data (a cut-short download) go to Review instead of counting as identical.
- Other websites can't drive the API through your browser: requests must be JSON from the app's own page, and unknown hostnames are refused (see `ALLOWED_HOSTS`).

---

## Reverse proxy

If you reach the app by a real domain name, add it to `ALLOWED_HOSTS` (for example `ALLOWED_HOSTS=dupes.example.com`), or you'll get a "doesn't answer to the hostname" page. That check is what stops a malicious website from using DNS rebinding to reach the app through your browser. IPs, single-word names and `.local`/`.lan`/`.home.arpa` names work without it.

There's no login, so if you expose this past your LAN, put authentication in front of it. Some examples:

- **Nginx Proxy Manager:** add an Access List with a username/password or an IP allow list, and attach it to the proxy host.
- **Caddy:** `basic_auth` or `forward_auth` to Authelia/Authentik.
- **Traefik:** a `basicauth` or `forwardauth` middleware.

Serving it from a sub-path works too (e.g. `https://example.com/dupes/`), as long as the proxy strips the prefix and the URL ends with a trailing slash. The frontend uses relative API paths.

Audio playback relies on HTTP Range requests, which all the proxies above pass through by default.

---

## Updating

```bash
docker compose pull && docker compose up -d
```

On TrueNAS, edit the app and save it, or use the update button once a new image is out. Your `/config` folder carries over between versions. If an update changes the tag cache format, the next scan just re-reads tags once.

---

## Troubleshooting

**Permission denied when quarantining, or the container won't start.** The container user can't write to your library or config folder. Check ownership with `ls -ln` and set `PUID`/`PGID` (or `user:`) to match. The container runs as `1000:1000` if you don't set anything.

**Quarantine is slow, or you see cross-device errors.** The music folder is split across several mounts, or `QUARANTINE_DIR` points outside it. Mount the whole library once at `/music` and leave `QUARANTINE_DIR` alone.

**Lidarr sources show "Not from Lidarr" for everything.** `LIDARR_MUSIC_ROOT` doesn't match the path Lidarr uses. Check a file's path in Lidarr's history.

**"music-dupes doesn't answer to the hostname ..."** You're using a domain name the app doesn't know. Add it to `ALLOWED_HOSTS`.

**The container exits saying QUARANTINE_DIR can't be the music folder.** Unset `QUARANTINE_DIR` (the default is fine), or point it somewhere that isn't the library or a parent of it.

**Scan results disappeared.** They live in memory, so a container restart clears them. Just scan again; the tag cache makes repeat scans fast.

**Check it's alive:** `curl http://localhost:8095/healthz` should return `{"ok":true}`. The image also has a Docker healthcheck, so `docker ps` shows `(healthy)`.

---

## Development

```
app/app.py          FastAPI backend: scanning, classification, quarantine
app/index.html      the whole frontend (vanilla JS, no build step)
tests/              pytest suite + fixture library generator + Playwright flow
deploy/             TrueNAS and no-build compose files
```

Run the tests (needs `ffmpeg` on your PATH to generate the fixture library):

```bash
pip install -r requirements-dev.txt
pytest
```

The suite builds a small library of sine-wave files that covers every duplicate pattern the app handles, then checks the classification for each one and exercises the API (range requests, path containment, quarantine/restore/purge, the never-remove-every-copy guard).

To click around against that same fixture library:

```bash
python tests/mkfix.py test-output/music
MUSIC_DIR=$PWD/test-output/music CONFIG_DIR=$PWD/test-output/config \
  uvicorn --app-dir app app:app --port 8095 --reload
```

There's also a Playwright walkthrough (`pip install playwright && playwright install chromium`, then `python tests/e2e.py http://127.0.0.1:8095/ dark`). Run it against a freshly generated library, since it quarantines and restores files.

Contributor notes and hard rules are in [CLAUDE.md](CLAUDE.md).

---

## Publishing your own image

Fork the repo and the included GitHub Actions workflow publishes to `ghcr.io/<your-user>/<your-repo>` on every push to `main`, and as versioned tags when you push a `v1.2.3` tag:

```bash
git tag v1.0.0 && git push origin v1.0.0
```

The first time it publishes, the package is private. Make it public under **your profile → Packages → docker-music-dupes → Package settings → Change visibility** so other machines can pull it without logging in.

Then set `IMAGE=ghcr.io/<your-user>/<your-repo>:latest` in `.env` (and the `image:` line in `deploy/truenas.yaml`).

---

## License

[MIT](LICENSE)
