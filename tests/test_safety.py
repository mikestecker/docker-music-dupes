"""Regression tests for the file-safety issues (GitHub issues #1-#7).

Each test builds its own album under "Zz Safety/" in the shared fixture
library, retags its copies with an artist and title nothing else uses so they
only group with each other, and cleans up after itself (including any
quarantine batches it created).
"""
import json
import os
import shutil
import threading
import time

import pytest

SRC = "Band/Album (2010)/02 Two.flac"


@pytest.fixture
def album(lib, client):
    music = lib[0]
    root = music / "Zz Safety"
    folder = root / "Album (2010)"
    folder.mkdir(parents=True)
    before = {b["batch"] for b in client.get("/api/quarantine").json()}
    yield folder
    for b in client.get("/api/quarantine").json():
        if b["batch"] not in before:
            client.post("/api/purge", json={"batch": b["batch"]})
    shutil.rmtree(root, ignore_errors=True)


def own(*paths):
    """Give test copies tags no fixture uses, so they only match each other."""
    import mutagen
    for p in paths:
        st = os.stat(p)
        a = mutagen.File(p)
        for k, v in {"artist": "Zz Safety", "albumartist": "Zz Safety",
                     "album": "Safety", "title": "Solo"}.items():
            a[k] = v
        a.save()
        os.utime(p, (st.st_atime, st.st_mtime))


def two_copies(lib, folder):
    src = lib[0] / SRC
    a, b = folder / "02 Two.flac", folder / "02 Two (1).flac"
    shutil.copy2(src, a)
    shutil.copy2(src, b)
    own(a, b)
    return a, b


def rel(lib, path):
    return str(path.relative_to(lib[0]))


def cluster(scan, lib, folder):
    s = scan()
    want = rel(lib, folder)
    hits = [c for c in s["clusters"] if c["editions"][0]["folder"] == want]
    return hits[0] if hits else None


def suggested(c):
    return [f["rel"] for r in c["rows"] for f in r["files"] if f["suggested"]]


# ---------- #1 symlinks ----------

def test_symlinked_copy_is_never_a_copy(client, scan, lib, album):
    real = album / "02 Two.flac"
    shutil.copy2(lib[0] / SRC, real)
    own(real)
    os.symlink("02 Two.flac", album / "02 Two (1).flac")
    assert cluster(scan, lib, album) is None
    # and nothing will act on the link, even if asked directly
    link = rel(lib, album / "02 Two (1).flac")
    assert client.get("/api/audio", params={"rel": link}).status_code == 400
    assert real.is_file()


def test_paths_through_symlinked_dirs_are_refused(app_mod, lib, album):
    os.symlink(str(album), str(album.parent / "Linked"))
    shutil.copy2(lib[0] / SRC, album / "02 Two.flac")
    with pytest.raises(ValueError):
        app_mod.library_path("Zz Safety/Linked/02 Two.flac")
    assert app_mod.load_file("Zz Safety/Linked/02 Two.flac") is None
    assert app_mod.load_file("Zz Safety/Album (2010)/02 Two.flac") is not None


def test_quarantine_folder_is_never_a_copy(app_mod):
    with pytest.raises(ValueError):
        app_mod.library_path(".dupe-quarantine/x.flac")
    with pytest.raises(ValueError):
        app_mod.library_path("")


# ---------- #2 concurrent quarantines ----------

def test_concurrent_quarantines_leave_one_copy(client, scan, lib, album, app_mod, monkeypatch):
    a, b = two_copies(lib, album)
    cluster(scan, lib, album)
    real_move = app_mod.move

    def slow_move(src, dst):
        time.sleep(0.3)  # widen the window a slow disk would give
        real_move(src, dst)
    monkeypatch.setattr(app_mod, "move", slow_move)
    out = []
    threads = [threading.Thread(target=lambda p=p: out.append(
        client.post("/api/quarantine", json={"paths": [rel(lib, p)]}).json())) for p in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(o["moved"] for o in out) == [0, 1]
    assert a.exists() != b.exists()


# ---------- #3 stale scan results ----------

def test_keeper_deleted_after_scan(client, scan, lib, album):
    a, b = two_copies(lib, album)
    picks = suggested(cluster(scan, lib, album))
    keeper = a if rel(lib, b) in picks else b
    keeper.unlink()
    r = client.post("/api/quarantine", json={"paths": picks}).json()
    assert r["moved"] == 0 and r["errors"]
    assert all((lib[0] / p).exists() for p in picks)


def test_keeper_replaced_after_scan(client, scan, lib, album):
    a, b = two_copies(lib, album)
    picks = suggested(cluster(scan, lib, album))
    keeper = a if rel(lib, b) in picks else b
    keeper.write_bytes(b"not the file we scanned")
    r = client.post("/api/quarantine", json={"paths": picks}).json()
    assert r["moved"] == 0


def test_pick_changed_after_scan(client, scan, lib, album):
    two_copies(lib, album)
    picks = suggested(cluster(scan, lib, album))
    with open(lib[0] / picks[0], "ab") as fh:
        fh.write(b"\0")
    r = client.post("/api/quarantine", json={"paths": picks}).json()
    assert r["moved"] == 0 and "changed" in r["errors"][0]


# ---------- #6 quarantine bookkeeping ----------

def test_corrupt_manifest_is_never_reset(client, scan, lib, album, app_mod):
    two_copies(lib, album)
    picks = suggested(cluster(scan, lib, album))
    path = app_mod.MANIFEST
    original = open(path).read() if os.path.exists(path) else None
    try:
        with open(path, "w") as fh:
            fh.write('{"20260101-000000-abcd": {"created": 1, "files": [')  # truncated
        assert client.post("/api/quarantine", json={"paths": picks}).status_code == 500
        assert client.get("/api/quarantine").status_code == 500
        assert open(path).read().endswith('"files": [')  # untouched
        assert all((lib[0] / p).exists() for p in picks)
    finally:
        if original is None:
            os.remove(path)
        else:
            with open(path, "w") as fh:
                fh.write(original)


def test_unwritable_config_moves_nothing(client, scan, lib, album, app_mod, monkeypatch):
    two_copies(lib, album)
    picks = suggested(cluster(scan, lib, album))

    def full(*_):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(app_mod, "save_json", full)
    with pytest.raises(OSError):
        client.post("/api/quarantine", json={"paths": picks})
    assert all((lib[0] / p).exists() for p in picks)


def test_batch_ids_never_collide(app_mod, monkeypatch):
    class FakeUUID:
        hexes = iter(["aaaa" + "0" * 28, "aaaa" + "0" * 28, "bbbb" + "0" * 28])

        @classmethod
        def uuid4(cls):
            class U:
                hex = next(cls.hexes)
            return U
    monkeypatch.setattr(app_mod.time, "strftime", lambda *_: "20260101-000000")
    monkeypatch.setattr(app_mod, "uuid", FakeUUID)
    assert app_mod.new_batch_id({"20260101-000000-aaaa": {}}) == "20260101-000000-bbbb"


def test_restore_keeps_going_and_never_replaces_a_link(client, scan, lib, album):
    a, b = two_copies(lib, album)
    picks = suggested(cluster(scan, lib, album))
    batch = client.post("/api/quarantine", json={"paths": picks})
    batch = batch.json()["batch"]
    os.symlink("/nonexistent", lib[0] / picks[0])  # dangling link where it was
    r = client.post("/api/restore", json={"batch": batch}).json()
    assert r["restored"] == 0 and r["errors"]
    assert os.path.islink(lib[0] / picks[0])
    assert client.get("/api/quarantine").json()[0]["batch"] == batch
    os.unlink(lib[0] / picks[0])
    assert client.post("/api/restore", json={"batch": batch}).json()["restored"] == 1


def test_purge_failure_keeps_the_record(client, scan, lib, album, app_mod, monkeypatch):
    two_copies(lib, album)
    picks = suggested(cluster(scan, lib, album))
    batch = client.post("/api/quarantine", json={"paths": picks}).json()["batch"]

    def fail(*_, **__):
        raise OSError(5, "I/O error")
    monkeypatch.setattr(app_mod.shutil, "rmtree", fail)
    assert client.post("/api/purge", json={"batch": batch}).status_code == 500
    monkeypatch.undo()
    assert any(b["batch"] == batch for b in client.get("/api/quarantine").json())


def test_quarantine_never_overwrites(app_mod, tmp_path):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_text("a")
    dst.write_text("b")
    with pytest.raises(FileExistsError):
        app_mod.move(str(src), str(dst))
    assert dst.read_text() == "b" and src.exists()


# ---------- #5 truncated FLAC ----------

def test_truncated_flac_goes_to_review(scan, lib, album):
    # The truncated copy gets the cleaner name and the older mtime, so before
    # the fix it was kept and the intact copy was suggested for removal.
    data = (lib[0] / "Lynyrd Skynyrd/Pronounced (1973)/08 Free Bird.flac").read_bytes()
    bad, good = album / "08 Free Bird.flac", album / "08 Free Bird (1).flac"
    bad.write_bytes(data[: len(data) // 3])
    good.write_bytes(data)
    own(bad, good)
    os.utime(bad, (1, 1))
    c = cluster(scan, lib, album)
    assert (c["kind"], c["reason"]) == ("manual", "Possibly damaged copy")
    assert suggested(c) == []
    chips = [ch["text"] for ch in c["rows"][0]["evidence"]]
    assert "Possibly damaged" in chips and "Identical audio" not in chips


def test_identical_copies_still_suggested(scan, lib, album):
    two_copies(lib, album)
    c = cluster(scan, lib, album)
    assert (c["kind"], c["reason"]) == ("suggested", "Duplicate files in one folder")


# ---------- #7 quarantine folder misconfiguration ----------

@pytest.mark.parametrize("qdir", ["{m}", "{base}"])
def test_quarantine_dir_cant_cover_library(tmp_path, qdir):
    import subprocess
    import sys
    music = tmp_path / "music"
    music.mkdir()
    env = dict(os.environ, MUSIC_DIR=str(music), CONFIG_DIR=str(tmp_path / "config"),
               QUARANTINE_DIR=qdir.format(m=music, base=tmp_path))
    app_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
    r = subprocess.run([sys.executable, "-c", "import app"], cwd=app_dir, env=env,
                       capture_output=True, text=True)
    assert r.returncode != 0 and "QUARANTINE_DIR" in r.stderr
    assert not (music / ".ndignore").exists() and not (tmp_path / ".ndignore").exists()


def test_quarantine_dir_inside_library_is_fine(app_mod):
    app_mod.check_dirs("/music", "/music/.dupe-quarantine")
    app_mod.check_dirs("/music", "/elsewhere")


# ---------- upgrade in place (#13) ----------

TIDARR = "Half·Alive/Now (2019)"
LIDARR = "Half•Alive/Now (2019)"


def bits(path):
    import mutagen
    return mutagen.File(path).info.bits_per_sample


def test_upgrade_in_place_and_undo(client, scan, lib):
    music = lib[0]
    scan()
    better, album = f"{TIDARR}/02 Runaway.flac", f"{LIDARR}/02 Runaway.flac"
    assert bits(music / better) == 24 and bits(music / album) == 16
    r = client.post("/api/upgrade", json={"rel": better})
    assert r.status_code == 200, r.text
    r = r.json()
    assert r["replaced"] == album and r["now"] == album
    assert not (music / better).exists()
    assert bits(music / album) == 24  # the album now holds the 24-bit file
    assert bits(music / ".dupe-quarantine" / r["batch"] / album) == 16
    listed = next(b for b in client.get("/api/quarantine").json() if b["batch"] == r["batch"])
    assert listed["moves"][0]["from"] == better

    u = client.post("/api/restore", json={"batch": r["batch"]}).json()
    assert u == {"restored": 1, "errors": []}
    assert bits(music / better) == 24 and bits(music / album) == 16
    assert not any(b["batch"] == r["batch"] for b in client.get("/api/quarantine").json())


def test_upgrade_refuses_what_it_wasnt_offered(client, scan):
    scan()
    album = f"{LIDARR}/02 Runaway.flac"  # the lower-quality copy has no upgrade
    assert client.post("/api/upgrade", json={"rel": album}).status_code == 400
    assert client.post("/api/upgrade", json={"rel": "../../etc/passwd"}).status_code == 400


def test_upgrade_refuses_changed_files(client, scan, lib):
    music = lib[0]
    scan()
    better, album = music / TIDARR / "12 Creature.flac", music / LIDARR / "12 Creature.flac"
    st = os.stat(album)
    os.utime(album, (st.st_atime, st.st_mtime + 5))
    try:
        r = client.post("/api/upgrade", json={"rel": f"{TIDARR}/12 Creature.flac"})
        assert r.status_code == 409
        assert better.exists() and bits(album) == 16
    finally:
        os.utime(album, (st.st_atime, st.st_mtime))


# ---------- Lidarr retag history (#13) ----------

@pytest.fixture
def fake_lidarr(app_mod, monkeypatch):
    import http.server
    import urllib.parse

    records = {
        "3": [{"data": {"ImportedPath": "/data/media/music/A/B/01 X.flac",
                        "downloadClientName": "Tidarr (SABnzbd)"}}],
        "9": [{"sourceTitle": "/data/media/music/A/B/02 Y.flac", "date": "2026-10-03T14:02:00Z",
               "data": {"tagsScrubbed": "True",
                        "diff": json.dumps([{"Field": "ISRC", "OldValue": "US1", "NewValue": ""},
                                            {"Field": "Title", "OldValue": "y", "NewValue": "Y"}])}},
              {"sourceTitle": "/elsewhere/03 Z.flac", "data": {}}],
    }

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            assert self.headers["X-Api-Key"] == "k"
            recs = records.get(q["eventType"][0], [])
            body = json.dumps({"records": recs, "totalRecords": len(recs)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(app_mod, "LIDARR_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(app_mod, "LIDARR_KEY", "k")
    monkeypatch.setattr(app_mod, "LIDARR_ROOT", "/data/media/music")
    yield
    srv.shutdown()


def test_lidarr_imports_and_retags(app_mod, fake_lidarr):
    imports, retags, warn = app_mod.fetch_lidarr_sources()
    assert warn is None
    assert imports == {"A/B/01 X.flac": "Tidarr (SABnzbd)"}  # PascalCase key read too
    assert retags == {"A/B/02 Y.flac": {"date": "2026-10-03T14:02:00Z",
                                        "fields": ["ISRC", "Title"], "scrubbed": True}}
    assert app_mod.retag_text(retags["A/B/02 Y.flac"]) == "2026-10-03, changed ISRC, Title, removed other tags"


def test_retagged_date_is_ignored(app_mod, lib):
    # The Mixtape stray, pretending Lidarr rewrote its tags: its date no longer
    # counts, but its naming still marks it as the odd one out.
    folder = "Mixtape/Manic (2020)"
    group = [app_mod.load_file(f"{folder}/01-03 Graveyard.flac"),
             app_mod.load_file(f"{folder}/03 Graveyard.flac")]
    stray = group[1]
    score, tone, text = app_mod.folder_fit(stray, group, {"retagged": {stray["rel"]}})
    assert tone == "warn" and "added" not in text.lower()
    assert text.endswith("Its date is ignored because Lidarr rewrote its tags")
    score_plain, _, text_plain = app_mod.folder_fit(stray, group, {})
    assert "added" in text_plain.lower() and score < 0.5
