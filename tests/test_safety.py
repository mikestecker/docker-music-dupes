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
