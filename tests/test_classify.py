"""Expected classification for the fixture library (docs/HANDOFF.md 10.1)."""
import os

import pytest

# artist -> (kind, reason, kept folder name or None)
EXPECTED = {
    "Band": ("suggested", "Duplicate files in one folder", None),
    "Blindside": ("suggested", "Identical audio", "A Thought Crushed My Mind (2000)"),
    "Chris Renzema": ("suggested", "Same recordings", "Manna (2023)"),
    "Flyleaf": ("suggested", "Duplicate files in one folder", None),
    "Gable Price": ("manual", "Couldn't confirm same recordings", None),
    "Hres": ("manual", "Better quality in the edition we'd remove", None),
    "Josh Garrels": ("manual", "Different recordings", None),
    "Kings Kaleidoscope Hymns": ("manual", "Different album artists", None),
    "Kutless": ("suggested", "Deluxe edition covers the standard",
                "Hearts Of The Innocent (Special Edition) (2006)"),
    "Lynyrd Skynyrd": ("manual", "Track lengths differ", None),
}


@pytest.fixture(scope="module")
def loose(scan):
    return {c["artist"]: c for c in scan("loose")["clusters"]}


def test_every_artist_found(loose):
    assert sorted(loose) == sorted(EXPECTED)


@pytest.mark.parametrize("artist", sorted(EXPECTED))
def test_classification(loose, artist):
    kind, reason, keep = EXPECTED[artist]
    c = loose[artist]
    assert (c["kind"], c["reason"]) == (kind, reason)
    if keep:
        kept = [os.path.basename(e["folder"]) for e in c["editions"] if e["keep"]]
        assert kept == [keep]


@pytest.mark.parametrize("artist", sorted(a for a, e in EXPECTED.items() if e[0] == "manual"))
def test_manual_never_preselects(loose, artist):
    files = [f for r in loose[artist]["rows"] for f in r["files"]]
    assert not any(f["suggested"] for f in files)


@pytest.mark.parametrize("artist", sorted(a for a, e in EXPECTED.items() if e[0] == "suggested"))
def test_suggested_keeps_one_copy_per_track(loose, artist):
    for r in loose[artist]["rows"]:
        assert any(not f["suggested"] for f in r["files"]), r["title"]
        assert any(f["suggested"] for f in r["files"]), r["title"]


def test_same_folder_keeps_lossless(loose):
    row = loose["Band"]["rows"][0]
    picked = [f["name"] for f in row["files"] if f["suggested"]]
    assert picked == ["02 Two.m4a"]


@pytest.mark.parametrize("mode", ["same-folder", "cross-folder"])
def test_other_modes_run(scan, mode):
    s = scan(mode)
    assert s["status"] == "done", s["error"]
    assert s["mode"] == mode
