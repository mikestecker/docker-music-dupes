"""Expected classification for the fixture library built by tests/mkfix.py."""
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
    "Countryman": ("manual", "Track lengths differ", None),
    "Mixtape": ("suggested", "Duplicate files in one folder", None),
    "Half\u00b7Alive": ("manual", "Better copies in a partial folder", None),
    "Partly": ("suggested", "Complete album covers a partial copy", "Album (2018)"),
    "Unknown artist": ("suggested", "Duplicate files in one folder", None),
    "Credits": ("suggested", "Identical audio", None),
}


@pytest.fixture(scope="module")
def loose(scan):
    return {c["artist"]: c for c in scan()["clusters"]}


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


def test_keeps_the_copy_that_fits_the_folder(loose):
    # "03 Graveyard.flac" is named and dated unlike the rest of the album and
    # has more tags; the album's own "01-03 Graveyard.flac" must stay.
    files = loose["Mixtape"]["rows"][0]["files"]
    assert [f["name"] for f in files if f["suggested"]] == ["03 Graveyard.flac"]
    assert [f["name"] for f in files if f["stray"]] == ["03 Graveyard.flac"]


@pytest.fixture(scope="module")
def easy(loose):
    files = loose["Countryman"]["rows"][0]["files"]
    return {f["name"]: f for f in files}


def test_44_vs_48_khz_isnt_lower_quality(easy):
    assert {f["detail"] for f in easy.values()} == {"16/44.1", "16/48"}
    assert all(f["quality"] == "best" for f in easy.values())


def test_fit_line_says_why(easy):
    album, stray = easy["02-05 You Make It Easy.flac"], easy["20 You Make It Easy.flac"]
    assert album["fit"] == {"tone": "good", "text": "Named and added like the rest of the folder"}
    assert stray["fit"]["tone"] == "warn"
    assert stray["fit"]["text"] == ('Added 11 days after the rest of the folder, '
                                    'named "20 Title" though its tags say 2-5')
    assert stray["stray"] and not album["stray"]


def test_select_all_removable_keeps_the_album_copy(easy):
    assert [n for n, f in easy.items() if f["keep_pref"]] == ["02-05 You Make It Easy.flac"]
    assert not any(f["suggested"] for f in easy.values())  # still Review, nothing picked


def test_edition_header_describes_the_album(loose):
    e = loose["Countryman"]["editions"][0]
    assert (e["format"], e["detail"], e["tracks"], e["format_count"]) == ("FLAC", "16/44.1", 5, 4)
    assert e["added"] < 1759000000  # Sep 22 batch, not the Oct 3 stray


def test_length_chip_has_one_decimal(loose):
    chips = [c["text"] for c in loose["Countryman"]["rows"][0]["evidence"]]
    assert "Length off by 3.5s" in chips


@pytest.mark.parametrize("name,disc,track,want", [
    ("02-05 You Make It Easy.flac", 2, 5, True), ("20 You Make It Easy.flac", 2, 5, False),
    ("05 Song.flac", 1, 5, True), ("205 Song.flac", 2, 5, True), ("1. Song.flac", 1, 1, True),
    ("03 - Song.flac", 1, 3, True), ("929.flac", 1, 16, None), ("Song.flac", 1, 3, None),
    ("1999 Song.flac", 1, 3, None), ("04 Song.flac", 1, None, None),
])
def test_name_agrees(app_mod, name, disc, track, want):
    f = {"rel": f"A/B/{name}", "disc": disc, "track": track}
    assert app_mod.name_agrees(f) is want


def test_quality_class(app_mod):
    q = lambda bits, rate: app_mod.qclass({"lossless": True, "bits": bits, "rate": rate, "kbps": 0})
    assert q(16, 44100) == q(16, 48000)
    assert q(24, 48000) > q(16, 48000)
    assert q(24, 96000) > q(24, 48000)
    assert q(16, 44100) > app_mod.qclass({"lossless": False, "bits": 0, "rate": 44100, "kbps": 320})


TIDARR = "Half\u00b7Alive/Now (2019)"
LIDARR = "Half\u2022Alive/Now (2019)"


@pytest.fixture(scope="module")
def halfalive(loose):
    return loose["Half\u00b7Alive"]


def test_featuring_credit_pairs_tracks(halfalive):
    titles = sorted(r["title"] for r in halfalive["rows"])
    assert len(titles) == 3 and any(t.startswith("Rest") for t in titles)
    rest = next(r for r in halfalive["rows"] if r["title"].startswith("Rest"))
    assert "Credits differ" in [c["text"] for c in rest["evidence"]]


def test_tidarr_source_is_detected(halfalive):
    files = [f for r in halfalive["rows"] for f in r["files"]]
    assert {f["source"] for f in files if f["rel"].startswith(TIDARR)} == {"Tidarr"}
    assert "Tidarr" not in {f["source"] for f in files if f["rel"].startswith(LIDARR)}


def test_hires_partial_offers_upgrade_not_removal(halfalive):
    files = [f for r in halfalive["rows"] for f in r["files"]]
    assert not any(f["suggested"] for f in files)
    for r in halfalive["rows"]:
        better = next(f for f in r["files"] if f["rel"].startswith(TIDARR))
        album = next(f for f in r["files"] if f["rel"].startswith(LIDARR))
        assert better["upgrade"]["replace"] == album["rel"]
        assert better["upgrade"]["to"] == album["rel"]  # same name, same extension
        assert album["upgrade"] is None


def test_tiny_folder_has_no_fit_line(halfalive):
    tidarr = [f for r in halfalive["rows"] for f in r["files"] if f["rel"].startswith(TIDARR)]
    assert all(f["fit"]["text"] == "" for f in tidarr)


def test_partial_lossy_copy_is_suggested(loose):
    c = loose["Partly"]
    picks = sorted(f["name"] for r in c["rows"] for f in r["files"] if f["suggested"])
    assert picks == ["02 Two.m4a", "03 Three.m4a"]


def test_title_key(app_mod):
    k = app_mod.title_key
    assert k("Rest (with Samm Henshaw)") == k("Rest") == k("Rest [feat. X]") == k("Rest ft. X")
    assert k("Rest (Live)") != k("Rest")
    assert k("Featuring Song") == "featuring song"


def test_untagged_copies_are_found(loose):
    c = loose["Unknown artist"]
    assert [f["name"] for r in c["rows"] for f in r["files"] if f["suggested"]] == ["Song (1).flac"]


def test_differing_track_credits_are_found(loose):
    c = loose["Credits"]
    assert {f["rel"] for r in c["rows"] for f in r["files"]} == {
        "Credits/Album (2021)/03 Song.flac", "Credits/Album (2021) (Tidal)/03 Song.flac"}


def test_group_files_unions_keys(app_mod):
    base = dict(disc=1, track=3, album="A", albumartist="X", folder="X/A")
    a = dict(base, rel="X/A/03 S.flac", title="S", artist="X")
    b = dict(base, rel="X/B/03 S.flac", folder="X/B", title="S", artist="X, Y")  # album key
    c = dict(base, rel="X/B/S (1).flac", folder="X/B", title="", artist="", track=None)  # folder key
    d = dict(base, rel="Z/Q.flac", folder="Z", title="Other", artist="Z")
    groups = app_mod.group_files([a, b, c, d])
    assert [sorted(f["rel"] for f in g) for g in groups] == [["X/A/03 S.flac", "X/B/03 S.flac", "X/B/S (1).flac"]]
