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


def load_manifest():
    """The quarantine manifest. Unlike the other stores it is never silently
    reset: losing it would orphan every quarantined file."""
    try:
        with open(MANIFEST) as fh:
            m = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise HTTPException(500, f"Can't read {MANIFEST} ({e}). Nothing was changed. "
                                 "Fix or move that file, then try again.")
    if not isinstance(m, dict):
        raise HTTPException(500, f"{MANIFEST} isn't a quarantine manifest. Nothing was changed.")
    return m


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def load_file(rel):
    """Our file dict for a library-relative path, or None if unreadable/gone.
    Symlinks, quarantined files and paths outside the library are never copies."""
    try:
        p = library_path(rel)
        if not os.path.isfile(p):
            return None
        st = os.stat(p)
    except (ValueError, OSError):
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
    buckets, seen, found, rels = defaultdict(list), 0, 0, set()
    for s in songs:
        seen += 1
        p = os.path.normpath(s["path"] or "")
        rel = os.path.relpath(p, ND_ROOT) if os.path.isabs(p) else p
        if rel.startswith("..") or not os.path.exists(os.path.join(MUSIC, rel)):
            continue
        found += 1
        if rel in rels:  # overlapping libraries list one file twice
            continue
        rels.add(rel)
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
            p = os.path.join(d, n)
            if (not n.startswith(".") and os.path.splitext(n)[1].lower() in AUDIO_EXT
                    and not os.path.islink(p)):
                yield p


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
    """Absolute path for a library-relative one. Refuses paths outside the
    library, inside quarantine, or reached through a symlink: realpath() would
    turn "move the link" into "move the file it points to"."""
    p = os.path.normpath(os.path.join(MUSIC, rel))
    if (os.path.realpath(p) != p or p == MUSIC or not inside(p, MUSIC)
            or inside(p, QDIR)):
        raise ValueError("path is outside the library")
    return p


def move(src, dst):
    if os.path.lexists(dst):  # os.rename would silently replace it
        raise FileExistsError(errno.EEXIST, "a file already exists there", dst)
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

os.makedirs(CONFIG, exist_ok=True)
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


@app.get("/healthz")
def healthz():
    return {"ok": True}


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


def unchanged(f):
    """True if the file is still exactly what the scan saw."""
    try:
        st = os.stat(library_path(f["rel"]))
    except (ValueError, OSError):
        return False
    return st.st_size == f["size"] and st.st_mtime == f["mtime"]


def new_batch_id(m):
    while True:
        bid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        if bid not in m and not os.path.lexists(os.path.join(QDIR, bid)):
            return bid


@app.post("/api/quarantine")
def quarantine(req: PathsReq):
    # MLOCK for the whole operation: two overlapping requests must not both
    # pass the one-copy guard for the same track.
    with MLOCK:
        with LOCK:
            if STATE["status"] != "done":
                raise HTTPException(409, "Run a scan first.")
            rows = [r for c in STATE["clusters"] for r in c["rows"]]
        wanted = set(req.paths)
        plan, errors = [], []
        for r in rows:
            live = [f for f in r["files"] if not f["moved"]]
            picks = [f for f in live if f["rel"] in wanted]
            if not picks:
                continue
            # The library may have changed since the scan (Lidarr upgrades,
            # manual cleanup, another tab), so re-check against the disk.
            keep = [f for f in live if f not in picks and unchanged(f)]
            if not keep:
                errors.append(f"Skipped {r['title']}: no other copy would be left, "
                              "or it changed since the scan. Scan again.")
                continue
            for f in picks:
                if unchanged(f):
                    plan.append(f)
                else:
                    errors.append(f"Skipped {f['rel']}: it changed or moved since the scan. "
                                  "Scan again.")
        if not plan:
            return {"batch": None, "moved": 0, "bytes": 0, "errors": errors}

        # Record the batch before moving anything, so a full or read-only
        # /config fails here instead of leaving files nobody can restore.
        m = load_manifest()
        batch = new_batch_id(m)
        entry = lambda fs: [{"rel": f["rel"], "size": f["size"],
                             "quality": f"{f['format']} {f['detail']}"} for f in fs]
        m[batch] = {"created": time.time(), "files": entry(plan)}
        save_json(MANIFEST, m)

        moved = []
        for f in plan:
            try:
                move(library_path(f["rel"]), os.path.join(QDIR, batch, f["rel"]))
                f["moved"] = True
                moved.append(f)
            except Exception as e:
                errors.append(f"{f['rel']}: {e}")
        if moved:
            m[batch]["files"] = entry(moved)
        else:
            del m[batch]
        save_json(MANIFEST, m)
    return {"batch": batch if moved else None, "moved": len(moved),
            "bytes": sum(f["size"] for f in moved), "errors": errors}


@app.get("/api/quarantine")
def list_quarantine():
    with MLOCK:
        m = load_manifest()
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
        m = load_manifest()
        b = get_batch(m, req.batch)
        restored, remaining, errors, back = 0, [], [], set()
        for f in b["files"]:
            src = os.path.join(QDIR, req.batch, f["rel"])
            if not os.path.lexists(src):
                continue
            try:
                move(src, library_path(f["rel"]))
            except FileExistsError:
                errors.append(f"{f['rel']}: a file already exists there, left in quarantine.")
                remaining.append(f)
                continue
            except Exception as e:  # keep going; one bad file mustn't strand the rest
                errors.append(f"{f['rel']}: {e}, left in quarantine.")
                remaining.append(f)
                continue
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
        m = load_manifest()
        b = get_batch(m, req.batch)
        bdir = os.path.join(QDIR, req.batch)
        here = lambda f: os.path.lexists(os.path.join(bdir, f["rel"]))
        freed = sum(f["size"] for f in b["files"] if here(f))
        try:
            if os.path.lexists(bdir):
                shutil.rmtree(bdir)
        except OSError as e:
            left = [f for f in b["files"] if here(f)]
            if left:
                b["files"] = left
            else:
                del m[req.batch]
            save_json(MANIFEST, m)
            raise HTTPException(500, f"Couldn't delete everything in that batch ({e}). "
                                     f"{len(left)} files are still in quarantine.")
        del m[req.batch]
        save_json(MANIFEST, m)
    return {"bytes": freed}
