# music-dupes

A self-hosted web app that finds duplicate tracks in your music library, explains *why* it thinks they're duplicates using the raw tags and audio stream data, and lets you quarantine the extra copies in one click (with undo).

It's built for libraries that get fed by several pipelines at once (Lidarr, Tidarr, slskd, Usenet, torrents, old iTunes purchases) and end up with the same album three times under slightly different names.

- **Explains every match.** Same ISRC, identical FLAC audio checksum, track lengths, release years, and a side-by-side tag diff for each copy.
- **Suggests only what it can prove.** Deluxe over standard, hi-res over CD quality, lossless over lossy. Anything it can't confirm goes to a Review list with nothing pre-selected.
- **Inline player** with a "Switch copy" key so you can A/B two versions at the same timestamp.
- **Quarantine, not delete.** Files are renamed into a hidden folder inside your library. Restore any batch later; permanent deletion is a separate, explicit step.
- **Optional integrations:** Navidrome (group by its album IDs) and Lidarr (label which download client each file came from).
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

Open `http://<your-server>:8095`, pick a match mode and hit **Scan library**.

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

2. Open `deploy/truenas.yaml`, change the host paths, the `user:` line, and the Navidrome/Lidarr values (or delete those lines if you don't use them).
3. In the TrueNAS UI go to **Apps → Discover Apps → ⋮ → Install via YAML**, name it `music-dupes` and paste the file.
4. Open `http://<nas-ip>:8095`.

Separate TrueNAS apps can't reach each other by container name, so use the NAS IP for `NAVIDROME_URL` and `LIDARR_URL`.

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
| `ALLOWED_HOSTS` | | Extra hostnames the UI answers to, comma-separated, e.g. `dupes.example.com`. IPs, single-word names (`truenas`) and `.local`/`.lan`/`.home.arpa`/`.internal` names always work. `*` turns the check off. See [Reverse proxy](#reverse-proxy). |
| `PORT` | `8095` | Port the app listens on **inside** the container. You usually change the host side of the port mapping instead. |
| `MUSIC_DIR` | `/music` | Library path inside the container |
| `QUARANTINE_DIR` | `$MUSIC_DIR/.dupe-quarantine` | Where quarantined files go. Keep it inside the music mount. The app refuses to start if it's the library itself or a parent of it. |
| `CONFIG_DIR` | `/config` | Where state is stored |
| `NAVIDROME_URL` | | Navidrome base URL, e.g. `http://192.168.1.10:4533`. Enables Navidrome mode. |
| `NAVIDROME_USER` | | Navidrome username (a non-admin user is fine) |
| `NAVIDROME_PASSWORD` | | Navidrome password |
| `NAVIDROME_MUSIC_ROOT` | `/music` | Library path **as Navidrome's container sees it** |
| `NAVIDROME_DB` | `/navidrome/navidrome.db` | DB fallback, used only when the three vars above aren't set |
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

## Optional integrations

### Navidrome

Navidrome mode groups tracks by Navidrome's own album IDs instead of by tags. The app still reads tags from the files, Navidrome is only used for grouping.

1. Create a regular (non-admin) user in Navidrome for this app, e.g. `dupes`.
2. Set `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD`, and `NAVIDROME_MUSIC_ROOT`.
3. Run one scan in **Navidrome albums** mode. It'll fail and tell you to enable real paths, which is expected the first time.
4. In Navidrome, log in as that user and go to **Settings → Players → music-dupes**, turn on **Report Real Path**, and save.
5. Scan again.

Without step 4, Navidrome reports made-up paths built from tags, and the app can't find the files.

If you'd rather not use the API, leave those three variables empty and mount Navidrome's data folder read-only at `/navidrome` instead. The app copies the database to a temp folder before reading it and never touches the live file.

### Lidarr

Set `LIDARR_URL`, `LIDARR_API_KEY` and `LIDARR_MUSIC_ROOT`, and each file gets labelled with the download client Lidarr imported it from (say "slskd via Lidarr" or "SABnzbd via Lidarr"). It reads Lidarr's import history, so files renamed after import won't match.

### Your own source rules

For files Lidarr didn't import (Tidarr downloads, manual rips, store purchases), add rules to `/config/sources.json`. Rules run top to bottom, first match wins:

```json
{
  "rules": [
    {"label": "Tidarr", "tag": "comment", "pattern": "tidal"},
    {"label": "Bandcamp", "tag": "path", "pattern": "^Bandcamp/"},
    {"label": "Old CD rips", "tag": "encoder", "pattern": "^EAC"}
  ]
}
```

- `tag` is a tag name as shown in the app's tag table (lowercase), or `path` to match the file path relative to the library. Leave it out to search every tag.
- `pattern` is a case-insensitive regular expression.

Easiest way to write one: open **Compare tags** on a file from that source, find a tag that's always set the same way, and match on it. Rules are read at the start of each scan, so no restart needed.

Built-in labels cover iTunes Store purchases, anything with "tidal" in a tag, and MusicBrainz-tagged files.

---

## Using it

**Match modes**

| Mode | Groups by | Good for |
|---|---|---|
| Artist and title (default) | artist + title anywhere | Catching everything: reissues, deluxe editions, misspelled folders, stray copies |
| Same folder only | folder + disc + track + title | Quick cleanup of `Song (1).flac` style copies |
| Same album tags | album artist + album + disc + track + title, across folders | Exact re-downloads |
| Navidrome albums | Navidrome album IDs | If you trust Navidrome's grouping (needs setup above) |

**Sections**

- **Suggested:** the app is confident. Redundant copies are already selected. Look it over and hit **Quarantine**.
- **Review:** the app can't decide (different artists, different recordings, can't confirm). Nothing is selected. Play copies side by side, compare tags, pick what to remove, or choose **Keep both**.
- **Kept both:** albums you've told it to leave alone. If a new copy shows up later, the album comes back for review.

**Keyboard:** `/` focuses search, `B` switches the player to the next copy at the same timestamp, `Esc` closes the player.

**Quarantine tab:** every batch you've quarantined, with **Restore** and **Delete permanently**.

---

## How it decides

The short version, in order of strength:

1. **Identical audio.** FLAC files store an MD5 of the decoded audio. Same MD5 means bit-identical audio, no matter how the tags or file names differ.
2. **Same ISRC** and lengths within 1.5 seconds means the same recording, even across formats.
3. **Same disc/track slot in one folder** with lengths within a second.

Then, per album pair:

- Different album artists → Review.
- Any track with different ISRCs, or lengths more than 2.5s apart → Review.
- One edition is deluxe/expanded/special and the other isn't → suggest keeping the deluxe one.
- Every track proven identical or the same recording → suggest keeping the best quality, then the most complete, best-tagged edition.
- Anything else → Review.

Last check: if the edition it would keep has a lower-quality copy of any track than the one it would remove (say a CD-quality deluxe vs a hi-res standard), the whole album gets bumped to Review.

Within a folder, ties on quality go to the copy that fits in: named like the other tracks (`01-03 Title` vs `03 Title`) and written in the same download batch. A later stray re-download gets marked "Doesn't match the folder" and is the one selected.

Quality ranking is lossless over lossy, then bit depth, then sample rate. FLAC bitrate is ignored on purpose, since it only reflects how compressible the audio is.

The full rule set is in [docs/HANDOFF.md](docs/HANDOFF.md#4-scan-pipeline-in-detail).

### Safety guarantees

- It never removes every copy of a track. That's checked in the browser **and** on the server.
- Nothing is deleted without **Delete permanently** on a quarantine batch, with a confirmation.
- Only files under the library mount (and outside the quarantine folder) can be read, played or moved.
- Restore never overwrites a file that's come back in the meantime.
- Navidrome's live database is never opened directly.
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

**Navidrome mode says paths don't exist.** Turn on **Report Real Path** for the `music-dupes` player in Navidrome (see [Navidrome](#navidrome)), and check `NAVIDROME_MUSIC_ROOT` matches where Navidrome mounts the library.

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
docs/HANDOFF.md     full design notes: every rule, API shape, known gaps
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
