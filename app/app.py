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
import ipaddress
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
import zlib
from array import array
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from statistics import mean

import mutagen
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
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

# Navidrome used to be a scan mode. It reads the same files, so it found
# nothing a direct scan doesn't; its settings are now ignored (with a warning).
ND_LEFTOVERS = [k for k in ("NAVIDROME_URL", "NAVIDROME_USER", "NAVIDROME_PASSWORD",
                            "NAVIDROME_DB", "NAVIDROME_MUSIC_ROOT") if os.environ.get(k)]
ND_WARNING = (f"{', '.join(ND_LEFTOVERS)} {'is' if len(ND_LEFTOVERS) == 1 else 'are'} no "
              "longer used and can be removed: every scan now reads the library "
              "directly and finds everything Navidrome mode did.") if ND_LEFTOVERS else ""

# Lidarr (optional): its import history says which download client each file
# came from. LIDARR_ROOT is the music path as Lidarr's container sees it.
LIDARR_URL = os.environ.get("LIDARR_URL", "").rstrip("/")
LIDARR_KEY = os.environ.get("LIDARR_API_KEY", "")
LIDARR_ROOT = os.path.normpath(os.environ.get("LIDARR_MUSIC_ROOT", "/data/media/music"))

# Hostnames the UI may be reached by, besides IPs, single-label names and
# private suffixes (see host_allowed). Comma-separated; "*" turns the check off.
ALLOWED_HOSTS = {h.strip().lower().rstrip(".") for h in
                 os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}
LAN_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal",
                ".localdomain", ".localhost")

# Sources to prefer when copies are otherwise equal, best first, matched
# case-insensitively against the source label ("Tidarr", "Tidarr (SABnzbd) via
# Lidarr", a sources.json label). Empty turns it off.
PREFER_SOURCES = [x.strip().lower() for x in
                  os.environ.get("PREFER_SOURCES", "Tidarr").split(",") if x.strip()]

# Prefer a remastered edition when editions are otherwise equal. A tiebreak
# only: remasters aren't always better (many are louder and more compressed).
PREFER_REMASTERS = os.environ.get("PREFER_REMASTERS", "true").strip().lower() not in (
    "0", "false", "no", "off")
REMASTER_RE = re.compile(r"\bremaster(?:ed)?\b", re.I)

# Acoustic fingerprints (Chromaprint's fpcalc) of every file in a duplicate
# group, over the whole song. "on" uses them to confirm or veto duplicates,
# "report" (the default) only shows what they found and what they would
# change, "off" skips them. Without fpcalc they're skipped either way.
FINGERPRINT = os.environ.get("FINGERPRINT", "report").strip().lower()
if FINGERPRINT not in ("on", "report", "off"):
    FINGERPRINT = "on" if FINGERPRINT in ("1", "true", "yes") else (
        "off" if FINGERPRINT in ("0", "false", "no") else "report")
FPCALC = shutil.which("fpcalc")
FP_TIMEOUT = 600  # seconds per file
FP_MIN = 80       # fingerprint items (~10s) needed before a comparison means anything
FP_MATCH = 0.85   # bit agreement for "same audio" (unrelated audio sits near 0.6)
FP_DIFF = 0.70    # below this the audio is different
FP_COVER = 0.95   # share of the longer copy the match has to span
FP_WIN = 16       # items per window (~2s) when looking for short differing passages
FP_DIP = 0.06     # a window this far below the song's typical agreement differs
FP_SHIFT = 120    # alignment search, in items (~15s of padding either way)
CLEAN_RE = re.compile(r"[(\[]\s*(?:clean|edited)(?:\s+version)?\s*[)\]]", re.I)


def container_cpus():
    """CPUs this container may use: the cgroup quota (docker's `cpus:`) and the
    CPU set, not the host's core count, which os.cpu_count() reports."""
    try:
        n = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        n = os.cpu_count() or 1
    for path, period in (("/sys/fs/cgroup/cpu.max", None),
                         ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "/sys/fs/cgroup/cpu/cpu.cfs_period_us")):
        try:
            with open(path) as fh:
                parts = fh.read().split()
            if period:
                with open(period) as fh:
                    parts.append(fh.read().strip())
            quota, per = parts[0], int(parts[1])
            if quota not in ("max", "-1") and per > 0:
                n = min(n, max(1, math.ceil(int(quota) / per)))
            break
        except (OSError, ValueError, IndexError):
            continue
    return max(1, n)


_workers = os.environ.get("FINGERPRINT_WORKERS", "").strip()
FP_WORKERS = int(_workers) if _workers.isdigit() and int(_workers) > 0 else container_cpus()

AUDIO_EXT = {".flac", ".m4a", ".mp3", ".ogg", ".opus", ".aac",
             ".wav", ".aiff", ".aif", ".wma"}
LOSSLESS_EXT = {"flac", "wav", "aiff", "aif"}
MIME = {"flac": "audio/flac", "m4a": "audio/mp4", "mp3": "audio/mpeg",
        "ogg": "audio/ogg", "opus": "audio/ogg", "aac": "audio/aac",
        "wav": "audio/wav", "aiff": "audio/aiff", "aif": "audio/aiff"}
BATCH_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
DELUXE_RE = re.compile(
    r"\b(deluxe|expanded|special edition|super deluxe|bonus tracks?|"
    r"collector'?s|anniversary|extended|complete edition)\b", re.I)
YEAR_RE = re.compile(r"\((\d{4})\)")
# Edition wording that doesn't make a different album: "(Deluxe Edition)",
# "[2011 Remaster]", "(Special Edition)", "(2019)", "Album - Remastered 2009"
EDITION_WORDS = (r"deluxe|expanded|special|collector'?s|anniversary|complete|edition|"
                 r"remaster(?:ed)?|bonus|explicit|clean|version|extended|reissue")
EDITION_RE = re.compile(
    rf"\s*[(\[][^)\]]*\b(?:{EDITION_WORDS})\b[^)\]]*[)\]]"
    r"|\s*[(\[]\s*\d{4}\s*[)\]]"
    rf"|\s+-\s+[^-]*\b(?:{EDITION_WORDS})\b[^-]*$", re.I)
SAME_ALBUM = 0.75  # name similarity above which two albums are one (typos)
COPY_RE = re.compile(r"\s*\(\d+\)$")
# Track-number prefix of a filename: "01-03 ", "03 ", "1. ", "03 - "
PREFIX_RE = re.compile(r"^\d{1,3}(?:[-.]\d{1,3})?(?:\s*[-._]\s*|\s+)?")
# The same, but only when a title follows, so "929.flac" isn't track 929
TRACKNUM_RE = re.compile(r"^(\d{1,3})(?:[-.](\d{1,3}))?(?!\d)(?=\s*[-._]?\s*\S)\s*[-._]?\s")
BATCH_SECS = 3600  # files written this close together came from one download
LEN_TOL = 2.5  # seconds; beyond this two files aren't treated as the same take


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"[^\w]+", " ", s).strip()


# Featuring credits: "Rest (with Samm Henshaw)", "Song [feat. X]", "Song ft. X"
FEAT_RE = re.compile(r"\s*[(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s[^)\]]*[)\]]"
                     r"|\s+(?:feat\.?|ft\.?|featuring)\s.*$", re.I)


def title_key(title):
    """Title for grouping: normalized, featuring credits removed."""
    return norm(FEAT_RE.sub("", title or ""))


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


def qclass(f):
    """Quality for decisions: like score(), but 44.1 kHz and 48 kHz at the same
    bit depth count as equal. Above 48 kHz (hi-res) the rate still counts."""
    if f["lossless"]:
        return (1, f["bits"], f["rate"] if f["rate"] > 48000 else 0, 0)
    return score(f)


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


def artist_key(artist):
    return norm(FEAT_RE.sub("", artist or ""))


def stem_key(rel):
    """A filename as a title: "03 - Song (1).flac" -> "song"."""
    stem = os.path.splitext(os.path.basename(rel))[0]
    stem = PREFIX_RE.sub("", COPY_RE.sub("", stem), count=1)
    return norm(stem)


def group_keys(f):
    """Every way two files can be the same track; sharing any one key puts
    them in the same group. Grouping casts a wide net on purpose: the evidence
    step decides what's confirmed, and anything loose lands in Review.

      artist + title             the same song anywhere in the library
      album artist + album slot  catches differing track credits
                                 ("A, B" vs "A") on the same album
      folder + title/filename    stray copies in one folder, untagged too"""
    title = title_key(f["title"])
    keys = [("folder", f["folder"], title or stem_key(f["rel"]))]
    if title:
        keys.append(("artist", artist_key(f["artist"] or f["albumartist"]), title))
        if f["album"] and f["track"]:
            keys.append(("album", artist_key(f["albumartist"] or f["artist"]), norm(f["album"]),
                         f["disc"], f["track"], title))
    return keys


def group_files(files):
    """Union-find over group_keys: a file matched by one key to B and by
    another to C ends up in one group with both."""
    parent = list(range(len(files)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    first = {}
    for i, f in enumerate(files):
        for k in group_keys(f):
            if k in first:
                parent[find(i)] = find(first[k])
            else:
                first[k] = i
    out = defaultdict(list)
    for i, f in enumerate(files):
        out[find(i)].append(f)
    return [g for g in out.values() if len(g) > 1]


# ---------- caches and small JSON stores ----------

class TagCache:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        # v2: full raw tags + stream data. Old v1 rows are simply ignored.
        self.db.execute("CREATE TABLE IF NOT EXISTS files_v2 ("
                        "path TEXT PRIMARY KEY, mtime REAL, size INTEGER, data TEXT)")
        # Fingerprints live apart from the tags so turning them on doesn't
        # re-read every file's tags. An empty blob means fpcalc couldn't read it.
        self.db.execute("CREATE TABLE IF NOT EXISTS prints_v1 ("
                        "path TEXT PRIMARY KEY, mtime REAL, size INTEGER, data BLOB)")
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

    def get_print(self, rel, mtime, size):
        with self.lock:
            row = self.db.execute(
                "SELECT data FROM prints_v1 WHERE path=? AND mtime=? AND size=?",
                (rel, mtime, size)).fetchone()
        if row is None:
            return None
        a = array("I")
        if row[0]:
            a.frombytes(zlib.decompress(row[0]))
        return list(a)

    def put_print(self, rel, mtime, size, fp):
        blob = zlib.compress(array("I", fp).tobytes()) if fp else b""
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO prints_v1 VALUES (?,?,?,?)",
                            (rel, mtime, size, blob))

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

def lidarr_history(event_type):
    """Every Lidarr history record of one event type, oldest first."""
    out, page = [], 1
    while page <= 200:
        q = urllib.parse.urlencode({
            "page": page, "pageSize": 1000, "eventType": event_type,
            "sortKey": "date", "sortDirection": "ascending"})
        req = urllib.request.Request(f"{LIDARR_URL}/api/v1/history?{q}",
                                     headers={"X-Api-Key": LIDARR_KEY})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        recs = data.get("records", [])
        out.extend(recs)
        if not recs or page * 1000 >= data.get("totalRecords", 0):
            break
        page += 1
    return out


def lidarr_rel(path):
    if not path:
        return None
    rel = os.path.relpath(os.path.normpath(path), LIDARR_ROOT)
    return None if rel.startswith("..") else rel


def ci(d, key):
    """Dict lookup ignoring key case: Lidarr's data keys are ImportedPath in
    its database and importedPath in most API versions."""
    key = key.lower()
    return next((v for k, v in (d or {}).items() if k.lower() == key), None)


def fetch_lidarr_sources():
    """({rel: download client}, {rel: retag info}, warning) from Lidarr's history:
    trackFileImported (3) says where a file came from, trackFileRetagged (9)
    says Lidarr rewrote its tags, which also resets its modified date."""
    if not (LIDARR_URL and LIDARR_KEY):
        return {}, {}, None
    imports, retags = {}, {}
    try:
        for rec in lidarr_history(3):
            d = rec.get("data") or {}
            rel = lidarr_rel(ci(d, "importedPath"))
            if rel:
                imports[rel] = (ci(d, "downloadClientName") or ci(d, "downloadClient")
                                or "unknown client")
        for rec in lidarr_history(9):
            rel = lidarr_rel(rec.get("sourceTitle"))
            if not rel:
                continue
            d = rec.get("data") or {}
            try:
                fields = sorted({x.get("Field") or x.get("field") or ""
                                 for x in json.loads(ci(d, "diff") or "[]")} - {""})
            except (ValueError, TypeError, AttributeError):
                fields = []
            retags[rel] = {"date": rec.get("date", ""), "fields": fields,
                           "scrubbed": str(ci(d, "tagsScrubbed")).lower() == "true"}
        return imports, retags, None
    except Exception as e:
        return imports, retags, (f"Couldn't read Lidarr's history ({e}), so download "
                                 "sources are partial.")


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
    brands = tags.get("compatible_brands", "").lower()
    if "dash" in brands or "cmfc" in brands:
        return "Tidarr", ("Tidal stream container tags (compatible_brands="
                          f"{tags['compatible_brands']}) that Tidarr leaves behind")
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


# ---------- acoustic fingerprints ----------

def fpcalc(path):
    """The whole song's raw Chromaprint fingerprint: a list of 32-bit ints, about
    8 per second. [] when fpcalc can't decode it, None when it timed out."""
    cmd = [FPCALC, "-raw", "-length", "0", path]
    if shutil.which("nice"):
        cmd = ["nice", "-n", "10"] + cmd  # leave the CPU to media servers
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=FP_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return []
    for line in r.stdout.splitlines():
        if line.startswith("FINGERPRINT="):
            try:
                return [int(x) & 0xFFFFFFFF for x in line[12:].split(",") if x]
            except ValueError:
                return []
    return []


def load_print(f):
    """A file's fingerprint from the cache, else from fpcalc (and cached)."""
    try:
        p = library_path(f["rel"])
        st = os.stat(p)
    except (ValueError, OSError):
        return None
    fp = CACHE.get_print(f["rel"], st.st_mtime, st.st_size)
    if fp is None:
        fp = fpcalc(p)
        if fp is None:
            return None
        CACHE.put_print(f["rel"], st.st_mtime, st.st_size, fp)
    return fp


def fp_compare(a, b):
    """Line two fingerprints up and measure how well they agree.
    -> {sim, cover, dips} or None when either is too short to say."""
    if len(a) < FP_MIN or len(b) < FP_MIN:
        return None
    # Equal values point at the alignment; try the likeliest few, else every
    # shift in range. Score by agreement beyond chance so a tiny overlap of a
    # repeated chorus can't beat the real alignment.
    pos = defaultdict(list)
    for j, v in enumerate(b):
        pos[v].append(j)
    # Values repeated all over (silence, a held chord) say nothing about alignment
    # and would make this quadratic, so they don't vote.
    votes = Counter(j - i for i, v in enumerate(a) if len(pos.get(v, ())) <= 8
                    for j in pos[v] if abs(j - i) <= FP_SHIFT)
    tops = [o for o, n in votes.most_common(3) if n >= 0.05 * min(len(a), len(b))]
    shifts = {o + d for o in tops for d in (-1, 0, 1)} if tops else range(-FP_SHIFT, FP_SHIFT + 1)
    best = None
    for off in shifts:
        lo, hi = max(0, -off), min(len(a), len(b) - off)
        if hi - lo < FP_MIN:
            continue
        errs = [(a[i] ^ b[i + off]).bit_count() for i in range(lo, hi)]
        sim = 1 - sum(errs) / (32 * len(errs))
        gain = len(errs) * (sim - 0.5)
        if best is None or gain > best[0]:
            best = (gain, sim, errs)
    if best is None:
        return None
    _, sim, errs = best
    # Short passages that disagree while the rest agrees: muted or swapped
    # words (a clean edit), a changed ending. The first and last windows are
    # skipped, since fades and padding differ between copies.
    wins = [1 - sum(errs[i:i + FP_WIN]) / (32 * FP_WIN)
            for i in range(0, len(errs) - FP_WIN + 1, FP_WIN)][1:-1]
    typical = sorted(wins)[len(wins) // 2] if wins else sim
    dips = sum(w < typical - FP_DIP for w in wins)
    return {"sim": sim, "cover": len(errs) / max(len(a), len(b)), "dips": dips}


def fp_verdict(cmp):
    """match / edit / dips / differs / unsure for one comparison."""
    if cmp["sim"] < FP_DIFF:
        return "differs"
    if cmp["sim"] < FP_MATCH:
        return "unsure"
    if cmp["cover"] < FP_COVER:
        return "edit"
    if cmp["dips"]:
        return "dips"
    return "match"


def fp_evidence(g):
    """The row's fingerprint result: every pair compared, the worst one wins.
    -> {verdict, text} where verdict is None when there's nothing to go on."""
    if FINGERPRINT == "off" or not FPCALC:
        return {"verdict": None, "text": ""}
    if any(f.get("fp") is None for f in g):
        return {"verdict": None, "text": "No fingerprint: a copy was skipped or timed out."}
    if any(not f["fp"] for f in g):
        return {"verdict": None, "text": "No fingerprint: fpcalc couldn't decode a copy."}
    order = ["differs", "edit", "dips", "unsure", "match"]
    worst = None
    for i, x in enumerate(g):
        for y in g[i + 1:]:
            c = fp_compare(x["fp"], y["fp"])
            if c is None:
                return {"verdict": None, "text": "Too short to compare fingerprints."}
            v = fp_verdict(c)
            if worst is None or order.index(v) < order.index(worst[0]):
                worst = (v, c)
    v, c = worst
    pct = f"{c['sim'] * 100:.0f}%"
    text = {
        "match": f"Fingerprint match {pct}, full length.",
        "edit": f"Fingerprints agree {pct}, but only over {c['cover'] * 100:.0f}% of the "
                "longer copy: an edit or a cut version.",
        "dips": f"Fingerprints agree {pct}, but {c['dips']} short "
                f"passage{'s differ' if c['dips'] > 1 else ' differs'}: possibly a clean "
                "or edited version.",
        "differs": f"Fingerprints differ ({pct} agreement): different audio.",
        "unsure": f"Fingerprints partly agree ({pct}): not enough to call it either way.",
    }[v]
    return {"verdict": v, "text": text}


def clean_marked(f):
    t = f["tags"]
    return (t.get("itunesadvisory") == "2" or t.get("advisory") == "2"
            or bool(CLEAN_RE.search(f"{f['title']} {f['album']}")))


def row_evidence(g, single, fp=None, use_fp=False):
    """use_fp: let the fingerprint result (fp, from fp_evidence) confirm or veto."""
    fp = fp or {"verdict": None, "text": ""}
    md5s = [f["md5"] for f in g]
    identical = all(md5s) and len(set(md5s)) == 1
    # Integrity, not quality: a FLAC's MD5 and length live in its header, so a
    # truncated copy still "matches". mutagen's FLAC bitrate is measured from
    # the audio bytes actually on disk, which barely moves between compression
    # levels for the same audio, so a big gap means a copy is cut short.
    rates = [f["kbps"] for f in g]
    damaged = identical and min(rates) < 0.75 * max(rates)
    identical = identical and not damaged
    isrcs = [f["isrc"] for f in g]
    same_isrc = all(isrcs) and len(set(isrcs)) == 1
    isrc_conflict = all(isrcs) and len(set(isrcs)) > 1
    secs = [f["secs"] for f in g]
    spread = max(secs) - min(secs)
    same_slot = len({(f["disc"], f["track"]) for f in g}) == 1
    same_fmt = len({score(f) for f in g if f["lossless"]}) == 1 and all(f["lossless"] for f in g)
    chips = []
    add = lambda tone, text, why="": chips.append({"tone": tone, "text": text, "why": why})
    if damaged:
        add("warn", "Possibly damaged", "The audio checksums match, but one copy holds much less audio "
            "data than the other, so it may be cut short or corrupt. Play each copy to the end.")
    elif identical:
        add("good", "Identical audio", "The FLAC audio checksums match, so the decoded audio is bit-for-bit the same.")
    elif all(md5s) and same_fmt:
        add("warn", "Different master", "Same format, but the decoded audio isn't bit-identical: "
            "a different master, remaster or edit. Listen before choosing.")
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
        add("warn", f"Length off by {spread:.1f}s" if spread < 60 else f"Length off by {fmt_len(spread)}",
            "Probably a different version, edit or recording.")
    titles = {norm(f["title"]) for f in g}
    if len(titles) > 1:
        add("neutral", "Credits differ", "The titles differ only by a featured artist: "
            + " vs ".join(sorted({f["title"] for f in g})) + ".")
    cleans = {clean_marked(f) for f in g}
    clean_mix = len(cleans) > 1
    if clean_mix:
        add("warn", "Clean and explicit", "One copy is tagged as a clean or edited version "
            "and another isn't. Those are different releases, so both stay unless you pick.")
    # Bit-identical audio needs no fingerprint; otherwise one that disagrees
    # (other audio, a cut, short passages that differ) blocks the match.
    fp_veto = use_fp and not identical and fp["verdict"] in ("differs", "edit", "dips")
    if fp_veto:
        add("warn", "Fingerprints differ", fp["text"])
    blocked = isrc_conflict or spread > LEN_TOL or damaged or clean_mix or fp_veto
    if identical:
        confirmed = "identical"
    elif blocked:
        confirmed = None
    elif same_isrc and spread <= 1.5:
        confirmed = "recording"
    elif use_fp and fp["verdict"] == "match":
        confirmed = "audio"  # same audio by fingerprint, no ISRC to go on
    elif single and same_slot and spread <= 1:
        confirmed = "slot"  # same track slot in the same album folder
    else:
        confirmed = None
    return {"chips": chips, "identical": identical, "damaged": damaged,
            "differs": bool(all(md5s) and same_fmt and not identical and not damaged),
            "isrc_conflict": isrc_conflict, "clean_mix": clean_mix, "fp_veto": fp_veto,
            "fp": fp, "spread": spread, "blocked": blocked, "confirmed": confirmed,
            "has_isrc": all(isrcs)}


def name_shape(name):
    """How a file is named, minus the title: "01-03 Graveyard.flac" -> ("99-99 ", ".flac")."""
    stem, ext = os.path.splitext(name)
    m = PREFIX_RE.match(stem)
    return re.sub(r"\d", "9", m.group(0) if m else ""), ext.lower()


def folder_files(folder, cache):
    """[(rel, name shape, mtime)] for the audio files in a folder, cached per scan."""
    if folder not in cache:
        out = []
        try:
            with os.scandir(os.path.join(MUSIC, folder)) as it:
                for e in it:
                    if (e.name.startswith(".") or not e.is_file(follow_symlinks=False)
                            or os.path.splitext(e.name)[1].lower() not in AUDIO_EXT):
                        continue
                    out.append((os.path.join(folder, e.name), name_shape(e.name),
                                e.stat(follow_symlinks=False).st_mtime))
        except OSError:
            pass
        cache[folder] = out
    return cache[folder]


def name_agrees(f):
    """Does the filename's track number match the file's own tags? None if the
    name has no track number or the tags have none to compare."""
    m = TRACKNUM_RE.match(os.path.basename(f["rel"]))
    if not m or not f["track"]:
        return None
    a, b = int(m.group(1)), m.group(2)
    if b is not None:
        return a == f["disc"] and int(b) == f["track"]
    return a in (f["track"], f["disc"] * 100 + f["track"])


def ago(secs):
    secs = abs(secs)
    if secs >= 36 * 3600:
        n, unit = round(secs / 86400), "day"
    elif secs >= 90 * 60:
        n, unit = round(secs / 3600), "hour"
    else:
        n, unit = max(1, round(secs / 60)), "minute"
    return f"{n} {unit}{'' if n == 1 else 's'}"


MIN_SIBS = 3  # fewer other tracks than this and naming/date patterns mean little


def folder_fit(f, group, cache):
    """How well a copy fits the rest of its folder, as (score, tone, text).

    Three signals, the copies being compared left out: siblings named the same
    way, siblings written in the same download batch, and whether the file's
    own track-number prefix agrees with its tags. Dates Lidarr reset by
    retagging are left out of the batch signal. Folders with fewer than
    MIN_SIBS other tracks only use the tag check. Score runs from -1 (stray)
    to 1 (fits perfectly), so it compares fairly across folders of any size."""
    retagged = cache.get("retagged", set())
    skip = {x["rel"] for x in group}
    name = os.path.basename(f["rel"])
    shape = name_shape(name)
    sibs = [s for s in folder_files(f["folder"], cache) if s[0] not in skip]
    n = len(sibs)
    same_name = sum(s[1] == shape for s in sibs)
    mine_retagged = f["rel"] in retagged
    dated = [] if mine_retagged else [s for s in sibs if s[0] not in retagged]
    nb = len(dated)
    same_batch = sum(abs(s[2] - f["mtime"]) <= BATCH_SECS for s in dated)
    agrees = name_agrees(f)
    parts = []
    if n >= MIN_SIBS:
        parts.append(same_name / n)
    if nb >= MIN_SIBS:
        parts.append(same_batch / nb)
    score_ = (sum(parts) / len(parts) if parts else 0) - (agrees is False)

    m = PREFIX_RE.match(os.path.splitext(name)[0])
    mine = f'"{(m.group(0).strip() + " ") if m else ""}Title"'
    pos = f"{f['disc']}-{f['track']}" if f["disc"] > 1 else f"track {f['track']}"
    odd = []
    if nb >= MIN_SIBS and same_batch * 2 < nb:
        median = sorted(s[2] for s in dated)[nb // 2]
        when = "after" if f["mtime"] > median else "before"
        odd.append(f"added {ago(f['mtime'] - median)} {when} the rest of the folder")
    if agrees is False:
        odd.append(f"named {mine} though its tags say {pos}")
    elif n >= MIN_SIBS and same_name * 2 < n:
        common = Counter(s[1] for s in sibs).most_common(1)[0][0]
        other = next(s[0] for s in sibs if s[1] == common)
        mo = PREFIX_RE.match(os.path.splitext(os.path.basename(other))[0])
        theirs = f'"{(mo.group(0).strip() + " ") if mo else ""}Title"'
        odd.append(f"named {mine} while the rest use {theirs}")
    note = ". Its date is ignored because Lidarr rewrote its tags" if mine_retagged and n >= MIN_SIBS else ""
    if odd:
        text = ", ".join(odd)
        return score_, "warn", text[0].upper() + text[1:] + note
    if n < MIN_SIBS:
        return score_, "", ""
    if same_name == n and (same_batch == nb or not nb):
        return score_, "good", ("Named like the rest of the folder" if not nb or mine_retagged
                                else "Named and added like the rest of the folder") + note
    return score_, "", (f"Named like {same_name} of the {n} other tracks in the folder"
                        + (f", added with {same_batch}" if nb >= MIN_SIBS else "") + note)


def folder_summary(folder, cache):
    """The folder's majority format and typical added time, so an edition is
    described by its album and not by one stray file in it."""
    key = ("summary", folder)
    if key not in cache:
        files = folder_files(folder, cache)
        loaded = [x for x in (load_file(s[0]) for s in files) if x]
        if loaded:
            counts = Counter((label(x), tier(x)) for x in loaded)
            ((fmt, det), tr), cnt = counts.most_common(1)[0]
            retagged = cache.get("retagged", set())
            dates = sorted(s[2] for s in files if s[0] not in retagged) or sorted(s[2] for s in files)
            added = dates[len(dates) // 2]
            cache[key] = {"format": fmt, "detail": det, "tier": tr,
                          "format_count": cnt, "added": added}
        else:
            cache[key] = None
    return cache[key]


def source_rank(label):
    """Higher for sources earlier in PREFER_SOURCES, 0 for the rest."""
    label = (label or "").lower()
    for i, pref in enumerate(PREFER_SOURCES):
        if pref in label:
            return len(PREFER_SOURCES) - i
    return 0


def name_matches_title(f):
    """Does the filename (minus its track number) say the same as the title
    tag? Characters filesystems can't hold may be replaced but not dropped:
    "Queen Songs + human" matches "Queen Songs / human.", "Queen SongsHuman"
    doesn't. None when there's no title to compare."""
    if not f["title"]:
        return None
    stem = os.path.splitext(os.path.basename(f["rel"]))[0]
    return norm(PREFIX_RE.sub("", stem, count=1)) == norm(f["title"])


def keep_rank(f, fit=0):
    """Which copy to keep, best first. Quality class and fit with the folder
    come first; then metadata: a filename that matches the title, an ISRC, a
    preferred source, tags Lidarr didn't rewrite; then the finer quality, no
    ' (1)' suffix, more tags, older. KEEP_REASONS names each position."""
    stem = os.path.splitext(os.path.basename(f["rel"]))[0]
    return (qclass(f), fit, name_matches_title(f) is True, bool(f["isrc"]),
            source_rank(f.get("source")), not f.get("retag"), score(f),
            not COPY_RE.search(stem), len(f["tags"]), -f["mtime"])


KEEP_REASONS = [
    lambda f, o: "better quality",
    lambda f, o: "fits the rest of the folder",
    lambda f, o: "filename matches its title",
    lambda f, o: "has an ISRC",
    lambda f, o: f"from {f['source']}, a preferred source",
    lambda f, o: "tags not rewritten by Lidarr",
    lambda f, o: f"{label(f)[1]} instead of {label(o)[1]}",
    lambda f, o: "no (1) in the name",
    lambda f, o: "more complete tags",
    lambda f, o: "older file",
]


def keep_why(keep, others, fits):
    """The first ranking step that put the kept copy ahead of the runner-up."""
    if not others:
        return ""
    rank = lambda f: keep_rank(f, fits[f["rel"]][0])
    runner = max(others, key=rank)
    for i, (a, b) in enumerate(zip(rank(keep), rank(runner))):
        if a != b:
            return KEEP_REASONS[i](keep, runner)
    return ""


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
        "Tags rewritten by Lidarr": retag_text(f.get("retag")),
    }


def retag_text(r):
    if not r:
        return ""
    what = ", ".join(r["fields"][:8]) + (" and more" if len(r["fields"]) > 8 else "")
    out = r["date"][:10]
    if what:
        out += f", changed {what}"
    if r["scrubbed"]:
        out += ", removed other tags"
    return out


def upgrades(g, ev, fits, album_folder):
    """{better copy's rel: what it can replace}. A better copy that sits outside
    the album (a partial folder, or a stray in the album's own folder) can take
    the place of the album's lower-quality copy: same name, its own extension.
    Only for the same take (lengths within 1s, nothing contradicting)."""
    if ev["spread"] > 1.0 or ev["blocked"] or ev["differs"]:
        return {}
    out = {}
    for p in g:
        for k in g:
            if k is p or qclass(p) <= qclass(k):
                continue
            if album_folder:
                belongs = k["folder"] == album_folder and p["folder"] != album_folder
            else:
                belongs = (k["folder"] == p["folder"]
                           and fits[k["rel"]][0] > fits[p["rel"]][0] + 0.5)
            if not belongs:
                continue
            stem = os.path.splitext(os.path.basename(k["rel"]))[0]
            dst = os.path.join(k["folder"], stem + os.path.splitext(p["rel"])[1].lower())
            if dst != k["rel"] and os.path.lexists(os.path.join(MUSIC, dst)):
                continue
            fmt, det = label(k)
            out[p["rel"]] = {"replace": k["rel"], "to": dst, "replaces": f"{fmt} {det}",
                             "folder": os.path.basename(k["folder"]),
                             "name": os.path.basename(dst)}
            break
    return out


def track_total(f):
    """The album's track count as a copy states it: TOTALTRACKS/TRACKTOTAL, or
    the "/9" in a "3/9" track number. None when the copy doesn't say."""
    t = f["tags"]
    for k in ("totaltracks", "tracktotal"):
        n = num(t.get(k))
        if n:
            return n
    m = re.match(r"\s*\d+\s*/\s*(\d+)", t.get("tracknumber") or t.get("track") or "")
    return int(m.group(1)) if m else None


def build_cluster(folders, rows, ignored, folder_cache):
    rows.sort(key=lambda g: (min(f["disc"] for f in g),
                             min(f["track"] or 999 for f in g), norm(g[0]["title"])))
    single = len(folders) == 1
    eds = []
    for folder in folders:
        fs = [f for g in rows for f in g if f["folder"] == folder]
        first, best = fs[0], max(fs, key=score)
        fmt, detail = label(best)
        summ = folder_summary(folder, folder_cache) or {
            "format": fmt, "detail": detail, "tier": tier(best),
            "format_count": 0, "added": max(f["mtime"] for f in fs)}
        eds.append({
            "folder": folder,
            "artist": first["albumartist"] or first["artist"] or "Unknown artist",
            "album": first["album"] or os.path.basename(folder),
            "year": year_of(first),
            "deluxe": bool(DELUXE_RE.search(f"{first['album']} {os.path.basename(folder)}")),
            "remaster": bool(REMASTER_RE.search(f"{first['album']} {os.path.basename(folder)}")),
            "tracks": len(folder_files(folder, folder_cache)),
            "format": summ["format"], "detail": summ["detail"], "tier": summ["tier"],
            "format_count": summ["format_count"],
            "source": Counter(f["source"] for f in fs).most_common(1)[0][0],
            "added": summ["added"],
            "tag_count": round(mean(len(f["tags"]) for f in fs), 1),
            # share of copies whose tags Lidarr didn't rewrite
            "untouched": round(mean(not f.get("retag") for f in fs), 2),
            "total": max((t for t in map(track_total, fs) if t), default=None),
            "keep": False,
        })
    # The album's size: an edition's own total, else (for editions of one
    # album) the largest total any edition states.
    one_album = all(same_album(a["album"], b["album"]) for i, a in enumerate(eds) for b in eds[i + 1:])
    known = max((e["total"] for e in eds if e["total"]), default=None)
    for e in eds:
        e["album_total"] = e["total"] or (known if one_album else None)
    fps = [fp_evidence(g) for g in rows]
    evs = [row_evidence(g, single, fp, FINGERPRINT == "on") for g, fp in zip(rows, fps)]
    kind, reason, detail, keeper = classify(eds, rows, evs, single)
    # Report mode: what turning fingerprints on would change about this cluster.
    would = None
    if FINGERPRINT == "report" and any(fp["verdict"] for fp in fps):
        alt = classify(eds, rows, [row_evidence(g, single, fp, True) for g, fp in zip(rows, fps)], single)
        if alt[:2] != (kind, reason):
            would = {"kind": alt[0], "reason": alt[1]}

    fits = {f["rel"]: folder_fit(f, g, folder_cache) for g in rows for f in g}

    # Pre-select per row: keep the best copy in the keeper folder; everything
    # else in the row goes only if it isn't better than what we keep.
    picks, whys = [], {}
    album_folder = keeper if reason == "Complete album covers a partial copy" else None
    if keeper is not None:
        for g in rows:
            mine = [f for f in g if f["folder"] == keeper]
            keep = max(mine, key=lambda f: keep_rank(f, fits[f["rel"]][0]))
            others = [f for f in g if f is not keep]
            whys[keep["rel"]] = keep_why(keep, [f for f in mine if f is not keep], fits)
            if any(qclass(f) > qclass(keep) for f in others):
                kind, keeper, picks = "manual", None, []
                if album_folder:
                    better = sum(any(qclass(f) > qclass(max((x for x in r if x["folder"] == album_folder),
                                                             key=qclass))
                                     for f in r if f["folder"] != album_folder) for r in rows)
                    reason = "Better copies in a partial folder"
                    detail = (f"The complete album has lower-quality copies of {better} of "
                              f"{len(rows)} tracks than the partial folder. Use Replace the "
                              "album's copy on the better file to move it into the album, "
                              "or pick per track.")
                else:
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
        best = max(qclass(f) for f in g)
        # A copy that fits its folder far worse than another copy in the same
        # folder (other naming, another day) is a stray re-download.
        top_fit = {}
        for f in g:
            top_fit[f["folder"]] = max(top_fit.get(f["folder"], -9), fits[f["rel"]][0])
        # The copy "Select all removable" keeps: the keeper's, else the app's pick.
        pool = [f for f in g if f["folder"] == keeper] or g
        pref = max(pool, key=lambda f: keep_rank(f, fits[f["rel"]][0]))["rel"]
        ups = upgrades(g, ev, fits, album_folder)
        files = []
        for f in sorted(g, key=lambda f: (folders.index(f["folder"]), -score(f)[1])):
            fmt, det = label(f)
            files.append({
                "rel": f["rel"], "name": os.path.basename(f["rel"]),
                "ed": folders.index(f["folder"]),
                "format": fmt, "detail": det, "tier": tier(f),
                "secs": f["secs"], "size": f["size"], "mtime": f["mtime"],
                "source": f["source"], "source_why": f["source_why"],
                "quality": "best" if qclass(f) == best else "lower",
                "stray": fits[f["rel"]][0] < top_fit[f["folder"]] - 0.5,
                "fit": {"tone": fits[f["rel"]][1], "text": fits[f["rel"]][2]},
                "keep_pref": f["rel"] == pref,
                "keep_why": whys.get(f["rel"], "") if keeper is not None else "",
                "upgrade": ups.get(f["rel"]),
                "suggested": f["rel"] in picks, "moved": False,
                "props": file_props(f), "tags": f["tags"],
            })
        h = g[0]
        out_rows.append({"title": h["title"] or os.path.basename(h["rel"]),
                         "track": h["track"], "disc": h["disc"],
                         "evidence": ev["chips"], "fingerprint": ev["fp"]["text"],
                         "files": files})
    key = "\n".join(sorted(f["rel"] for g in rows for f in g))
    return {"key": key, "kind": kind, "reason": reason, "detail": detail,
            "ignored": key in ignored, "editions": eds, "rows": out_rows, "fp_would": would,
            "artist": eds[0]["artist"], "album": eds[0]["album"]}


def album_key(album):
    """An album name without edition wording, for telling editions of one
    album apart from different albums."""
    return norm(EDITION_RE.sub("", album or ""))


def same_album(a, b):
    """Editions of one album (deluxe, remaster, a typo in the name) rather than
    two releases that share a song (an album and a single, a best-of, a
    compilation). Fractured/Fractioned Heart scores 0.84; Nevermind vs The
    Very Best 0.36."""
    a, b = album_key(a), album_key(b)
    return not a or not b or a == b or SequenceMatcher(None, a, b).ratio() >= SAME_ALBUM


def partial_copy(eds, rows, evs):
    """(complete edition, [partial editions]) when every other edition is a
    folder whose audio files are all duplicated in one more complete edition of
    the same album, else None. A stray partial download (Tidarr grabbing a few
    tracks Lidarr already has) shouldn't need a decision per track."""
    if len(eds) < 2 or not all(same_album(a["album"], b["album"])
                               for i, a in enumerate(eds) for b in eds[i + 1:]):
        return None
    in_rows = Counter(f["folder"] for g in rows for f in g)
    whole = max(eds, key=lambda e: e["tracks"])
    parts = [e for e in eds if e is not whole]
    if not all(e["tracks"] and in_rows[e["folder"]] >= e["tracks"] and e["tracks"] < whole["tracks"]
               for e in parts):
        return None
    for g, ev in zip(rows, evs):
        if not any(f["folder"] == whole["folder"] for f in g):
            return None
        # same take, and never audio we can prove is different
        if ev["spread"] > 1.0 or ev["blocked"] or ev["differs"]:
            return None
    return whole, parts


def edition_rank(e, best_count):
    """Which edition of one album to keep when the audio is confirmed the same.
    EDITION_REASONS names each position."""
    return (best_count[e["folder"]], e["tracks"], source_rank(e["source"]), e["untouched"],
            e.get("remaster", False) and PREFER_REMASTERS, e["tag_count"],
            -int(e["year"] or 9999))


EDITION_REASONS = [
    "the best quality on more tracks",
    "the most tracks",
    "a preferred source",
    "tags Lidarr didn't rewrite",
    "the remastered edition",
    "the richest tags",
    "the earliest year",
]


def classify(eds, rows, evs, single):
    """-> (kind, reason, detail, keeper folder or None)"""
    if not single and len({norm(e["artist"]) for e in eds}) > 1:
        return ("manual", "Different album artists",
                "The same tracks are filed under different artists. Kept separate "
                "unless you decide otherwise.", None)
    if not single and not all(same_album(a["album"], b["album"])
                              for i, a in enumerate(eds) for b in eds[i + 1:]):
        return ("other", "Same song on another album",
                "These are different releases (an album, a single, a compilation or a "
                "best-of) that share a recording. Both stay, so nothing is selected. "
                "Select a copy yourself if you don't want the song twice.", None)
    n_damaged = sum(e["damaged"] for e in evs)
    if n_damaged:
        return ("manual", "Possibly damaged copy",
                f"On {n_damaged} of {len(evs)} tracks one copy holds much less audio data "
                "than another with the same checksum, so it may be cut short. Play each "
                "copy to the end before removing anything.", None)
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
    n_clean = sum(e["clean_mix"] for e in evs)
    if n_clean:
        return ("manual", "Clean and explicit versions",
                f"On {n_clean} of {len(evs)} tracks one copy is tagged as a clean or edited "
                "version and another isn't. Both stay unless you pick.", None)
    n_fp = sum(e["fp_veto"] for e in evs)
    if n_fp:
        return ("manual", "Audio doesn't match",
                f"The acoustic fingerprints disagree on {n_fp} of {len(evs)} tracks: "
                "different audio, a cut version, or short passages that differ (often a "
                "clean edit). Listen to compare.", None)
    if single:
        if all(e["confirmed"] for e in evs):
            return ("suggested", "Duplicate files in one folder",
                    "Same track slot in the same album folder with matching length. "
                    "The best copy stays: quality first, then the one that fits the folder "
                    "and has the best metadata. Each kept copy says why.", eds[0]["folder"])
        return ("manual", "Couldn't confirm duplicates",
                "Same folder, but the lengths or recordings don't line up.", None)

    deluxe = [e for e in eds if e["deluxe"]]
    if deluxe and len(deluxe) < len(eds):
        keeper = max(deluxe, key=lambda e: (e["tracks"], e["tag_count"]))
        # a partial deluxe folder doesn't cover a fuller standard edition
        if all(keeper["tracks"] >= e["tracks"] for e in eds):
            return ("suggested", "Deluxe edition covers the standard",
                    f"Keeping {keeper['album']}, which has {keeper['tracks']} tracks. "
                    "Matching tracks in the standard edition are selected.", keeper["folder"])

    partial = partial_copy(eds, rows, evs)
    if partial:
        whole, parts = partial
        names = ", ".join(os.path.basename(e["folder"]) for e in parts)
        n = sum(e["tracks"] for e in parts)
        total = max((e["album_total"] or 0 for e in parts), default=0)
        if any(e["deluxe"] for e in parts):
            lead = (f"{names} would normally win as the bigger edition, but only "
                    f"{n} of its tracks {'is' if n == 1 else 'are'} here")
        elif total > n:
            lead = f"{names} holds {n} of the album's {total} tracks"
        else:
            lead = f"{names} only holds tracks"
        return ("suggested", "Complete album covers a partial copy",
                f"{lead}, all also in {whole['album']} ({whole['tracks']} tracks) with "
                "matching lengths. Keeping the complete album; the partial copies are "
                "selected.", whole["folder"])


    if all(e["confirmed"] in ("identical", "recording", "audio") for e in evs):
        best_count = Counter()
        for g in rows:
            top = max(qclass(f) for f in g)
            for folder in {f["folder"] for f in g if qclass(f) == top}:
                best_count[folder] += 1
        rank = lambda e: edition_rank(e, best_count)
        keeper = max(eds, key=rank)
        ident = all(e["identical"] for e in evs)
        reason = "Identical audio" if ident else "Same recordings"
        how = {e["confirmed"] for e in evs} - {"identical"}
        why = ("Every track's decoded audio is bit-for-bit identical." if ident else
               "Every track carries the same ISRC with matching length." if how == {"recording"} else
               "Every track's acoustic fingerprint matches over the full length." if how == {"audio"} else
               "Every track matches by ISRC or by acoustic fingerprint, with matching length.")
        years = {e["year"] for e in eds if e["year"]}
        if len(years) > 1:
            why += f" Release years differ ({', '.join(sorted(years))}), but it's the same audio, not a re-recording."
        runner = max((e for e in eds if e is not keeper), key=rank)
        decided = next((i for i, (a, b) in enumerate(zip(rank(keeper), rank(runner))) if a != b), None)
        kept = (f" Kept {os.path.basename(keeper['folder'])}: {EDITION_REASONS[decided]}."
                if decided is not None else "")
        if any(e["differs"] for e in evs):
            kept += (" Some tracks are different masters of the same recording, so listen "
                     "before quarantining.")
        return ("suggested", reason,
                f"{why} Keeping the edition with the best quality, then the most "
                "tracks, then a preferred source, then tags Lidarr didn't rewrite, then a "
                "remaster, then the richest tags, then the earliest year." + kept,
                keeper["folder"])

    missing = sum(not e["has_isrc"] and not e["identical"] for e in evs)
    return ("manual", "Couldn't confirm same recordings",
            f"There's no shared ISRC to compare on {missing or 'some'} of {len(evs)} "
            "tracks, and the audio checksums don't match. Listen to compare.", None)


# ---------- scan ----------

STATE = {"status": "idle", "scan_id": None, "phase": "", "skip_fp": False,
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


def fingerprint_groups(groups, warnings):
    """Fingerprint every file in a duplicate group (f["fp"]), in parallel.
    Files left when the user skips this, or that time out, have none."""
    if FINGERPRINT == "off":
        return
    if not FPCALC:
        warnings.append("FINGERPRINT is on but fpcalc isn't installed, so fingerprints were "
                        "skipped. The image includes it; a no-build install needs "
                        "libchromaprint-tools.")
        return
    todo = [f for g in groups for f in g]
    update(phase="Fingerprinting", scanned=0, total=len(todo))
    done = 0
    with ThreadPoolExecutor(max_workers=FP_WORKERS) as pool:
        futs = {pool.submit(load_print, f): f for f in todo}
        for fut in futs:
            if STATE["skip_fp"]:
                pool.shutdown(wait=True, cancel_futures=True)
                break
            futs[fut]["fp"] = fut.result()
            done += 1
            if done % 25 == 0:
                update(scanned=done)
                CACHE.commit()
    for fut, f in futs.items():
        if "fp" not in f and fut.done() and not fut.cancelled():
            f["fp"] = fut.result()
    CACHE.commit()
    if STATE["skip_fp"]:
        n = sum("fp" not in f or f["fp"] is None for f in todo)
        warnings.append(f"Fingerprinting was skipped for {n} files. Their tracks are judged "
                        "on tags and checksums only.")


def run_scan():
    try:
        warnings = [ND_WARNING] if ND_WARNING else []
        update(phase="Reading Lidarr history" if LIDARR_URL else "Listing files")
        lidarr, retags, warn = fetch_lidarr_sources()
        if warn:
            warnings.append(warn)
        rules = load_json(SOURCES, {}).get("rules", [])
        bad = 0
        update(phase="Listing files")
        rels = [os.path.relpath(p, MUSIC) for p in walk_audio()]
        update(phase="Reading tags", total=len(rels))
        files = []
        for i, rel in enumerate(rels, 1):
            f = load_file(rel)
            if f is None:
                bad += 1
            else:
                files.append(f)
            if i % 250 == 0:
                update(scanned=i, unreadable=bad)
                CACHE.commit()
        groups = group_files(files)
        CACHE.commit()
        fingerprint_groups(groups, warnings)

        update(phase="Comparing")
        for g in groups:
            for f in g:
                f["source"], f["source_why"] = detect_source(f, lidarr, rules)
                f["retag"] = retags.get(f["rel"])
        by_folders = defaultdict(list)
        for g in groups:
            by_folders[tuple(sorted({f["folder"] for f in g}))].append(g)
        ignored = set(load_json(IGNORED, []))
        counts = {"retagged": set(retags)}
        clusters = [build_cluster(list(folders), rows, ignored, counts)
                    for folders, rows in by_folders.items()]
        clusters.sort(key=lambda c: (c["artist"].casefold(), c["album"].casefold()))
        update(status="done", phase="", scanned=len(rels), total=len(rels), unreadable=bad,
               clusters=clusters, warnings=warnings, finished=time.time())
    except Exception as e:
        update(status="error", phase="", error=str(e), finished=time.time())


# ---------- file moves ----------

def inside(path, root):
    return os.path.commonpath([path, root]) == root


def check_dirs(music, qdir):
    """Refuse a quarantine folder that is the library or contains it: we'd write
    an .ndignore of "*" over the whole library and Navidrome would hide it."""
    if inside(music, qdir):
        raise SystemExit(f"QUARANTINE_DIR ({qdir}) can't be the music folder ({music}) "
                         "or contain it. Leave it unset to use MUSIC_DIR/.dupe-quarantine.")


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

check_dirs(MUSIC, QDIR)
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
        "_example": {"label": "Bandcamp", "tag": "path", "pattern": "^Bandcamp/"},
        "rules": []})
CACHE = TagCache(os.path.join(CONFIG, "tags.db"))

app = FastAPI(title="music-dupes")
if ND_WARNING:
    print(f"music-dupes: {ND_WARNING}", flush=True)


def host_allowed(host):
    """DNS rebinding needs a public domain name pointed at your LAN, so by
    default only IPs, single-label names and private suffixes get in. Anything
    else (a reverse proxy's hostname) has to be listed in ALLOWED_HOSTS."""
    if "*" in ALLOWED_HOSTS:
        return True
    host = (host or "").strip().lower()
    if host.startswith("["):  # [::1]:8095
        name = host[1:host.find("]")] if "]" in host else ""
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    name = name.rstrip(".")
    if not name:
        return False
    if name in ALLOWED_HOSTS:
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name == "localhost" or "." not in name or name.endswith(LAN_SUFFIXES)


@app.middleware("http")
async def request_guard(request, call_next):
    """No auth, so make sure only this app's own page can drive the API."""
    host = request.headers.get("host", "")
    if not host_allowed(host):
        return PlainTextResponse(
            f"music-dupes doesn't answer to the hostname {host!r}. If that's your "
            "reverse proxy, add it to the ALLOWED_HOSTS environment variable.", 403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # application/json forces a CORS preflight, which we never approve, so
        # other sites can't send these. FastAPI would happily parse a body
        # with no Content-Type at all, which browsers send without asking.
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json" or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Requests must come from the music-dupes page."}, 403)
    return await call_next(request)


class ScanReq(BaseModel):
    mode: str | None = None  # ignored; older pages still send it


class PathsReq(BaseModel):
    paths: list[str]


class UpgradeReq(BaseModel):
    rel: str


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
    return {"music": MUSIC, "quarantine": QDIR,
            "lidarr": bool(LIDARR_URL and LIDARR_KEY),
            "fingerprint": FINGERPRINT if FPCALC else "off", "fp_workers": FP_WORKERS}


@app.get("/api/scan")
def scan_state():
    with LOCK:
        return dict(STATE)


@app.post("/api/scan")
def start_scan(req: ScanReq | None = None):
    with LOCK:
        if STATE["status"] == "scanning":
            raise HTTPException(409, "A scan is already running.")
        STATE.update(status="scanning", scan_id=uuid.uuid4().hex,
                     phase="Starting", skip_fp=False, scanned=0, total=0, unreadable=0,
                     started=time.time(), finished=None, error=None,
                     warnings=[], clusters=[])
    threading.Thread(target=run_scan, daemon=True).start()
    return {"ok": True}


@app.post("/api/scan/skip-fingerprints")
def skip_fingerprints(req: ScanReq | None = None):
    with LOCK:
        if STATE["status"] != "scanning":
            raise HTTPException(409, "No scan is running.")
        STATE["skip_fp"] = True
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


@app.post("/api/upgrade")
def upgrade(req: UpgradeReq):
    """Move a better copy into the album in place of its lower-quality copy.
    The replaced copy goes to quarantine and the batch records the move, so
    Restore puts both files back where they were."""
    with MLOCK:
        with LOCK:
            if STATE["status"] != "done":
                raise HTTPException(409, "Run a scan first.")
            hit = next(((r, f) for c in STATE["clusters"] for r in c["rows"]
                        for f in r["files"] if f["rel"] == req.rel), None)
        if not hit or hit[1]["moved"] or not hit[1].get("upgrade"):
            raise HTTPException(400, "That copy can't replace anything. Scan again.")
        row, p = hit
        up = p["upgrade"]
        k = next((f for f in row["files"] if f["rel"] == up["replace"] and not f["moved"]), None)
        if k is None or not unchanged(p) or not unchanged(k):
            raise HTTPException(409, "These files changed since the scan. Scan again.")
        try:
            src, dst = library_path(p["rel"]), library_path(up["to"])
        except ValueError:
            raise HTTPException(400, "That path is outside the library.")
        if up["to"] != k["rel"] and os.path.lexists(dst):
            raise HTTPException(409, f"{up['to']} already exists.")

        m = load_manifest()
        batch = new_batch_id(m)
        m[batch] = {"created": time.time(),
                    "files": [{"rel": k["rel"], "size": k["size"],
                               "quality": f"{k['format']} {k['detail']}"}],
                    "moves": [{"from": p["rel"], "to": up["to"], "size": p["size"],
                               "quality": f"{p['format']} {p['detail']}"}]}
        save_json(MANIFEST, m)
        try:
            move(library_path(k["rel"]), os.path.join(QDIR, batch, k["rel"]))
        except Exception as e:
            del m[batch]
            save_json(MANIFEST, m)
            raise HTTPException(500, f"Couldn't quarantine {k['rel']}: {e}")
        try:
            move(src, dst)
        except Exception as e:  # put the album's copy back; nothing changed
            move(os.path.join(QDIR, batch, k["rel"]), library_path(k["rel"]))
            prune_empty(os.path.join(QDIR, batch))
            del m[batch]
            save_json(MANIFEST, m)
            raise HTTPException(500, f"Couldn't move {p['rel']}: {e}")
        k["moved"] = p["moved"] = True
    return {"batch": batch, "replaced": k["rel"], "now": up["to"]}


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
                        "moves": m[bid].get("moves", []),
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
        moves_left = []
        for mv in b.get("moves", []):  # an upgrade: move the better copy back first
            try:
                here, home = library_path(mv["to"]), library_path(mv["from"])
                if not os.path.lexists(here) or os.path.getsize(here) != mv["size"]:
                    raise FileNotFoundError(errno.ENOENT, "it isn't the file we moved anymore")
                move(here, home)
                back.add(mv["from"])
            except Exception as e:
                errors.append(f"{mv['to']}: couldn't move it back to {mv['from']} ({e}).")
                moves_left.append(mv)
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
        if moves_left:
            b["moves"] = moves_left
        else:
            b.pop("moves", None)
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
