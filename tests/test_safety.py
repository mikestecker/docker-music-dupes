"""Regression tests for the file-safety issues (GitHub issues #1-#7).

Each test builds its own album under "Zz Safety/" in the shared fixture
library, scans in same-folder mode so nothing else groups with it, and cleans
up after itself (including any quarantine batches it created).
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


def two_copies(lib, folder):
    src = lib[0] / SRC
    a, b = folder / "02 Two.flac", folder / "02 Two (1).flac"
    shutil.copy2(src, a)
    shutil.copy2(src, b)
    return a, b


def rel(lib, path):
    return str(path.relative_to(lib[0]))


def cluster(scan, lib, folder):
    s = scan("same-folder")
    want = rel(lib, folder)
    hits = [c for c in s["clusters"] if c["editions"][0]["folder"] == want]
    return hits[0] if hits else None


def suggested(c):
    return [f["rel"] for r in c["rows"] for f in r["files"] if f["suggested"]]


# ---------- #1 symlinks ----------

def test_symlinked_copy_is_never_a_copy(client, scan, lib, album):
    real = album / "02 Two.flac"
    shutil.copy2(lib[0] / SRC, real)
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
