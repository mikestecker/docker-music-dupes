"""Expected classification for the fixture library built by tests/mkfix.py."""
import os
import shutil

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
    "Peppy": ("suggested", "Duplicate files in one folder", None),
    "Kaleido": ("suggested", "Duplicate files in one folder", None),
    "Sourcey": ("suggested", "Duplicate files in one folder", None),
    "Comp": ("other", "Same song on another album", None),
    "Dlx": ("suggested", "Complete album covers a partial copy", "Album (2019)"),
    "Remas": ("suggested", "Same recordings", "Album (Remastered) (1984)"),
    "Hymnal": ("suggested", "Complete album covers a partial copy", "Hymns - Take the World, but Give Me Jesus (2010)"),
    # FINGERPRINT=report (the default): fingerprints are shown, not used.
    "Printz": ("manual", "Couldn't confirm same recordings", None),
    "Swapt": ("suggested", "Same recordings", None),
    "Cleanly": ("manual", "Clean and explicit versions", None),
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


@pytest.mark.parametrize("artist,kept,removed,why", [
    ("Peppy", "02 Queen Songs + human.flac", "02 Queen SongsHuman.flac", "filename matches its title"),
    ("Kaleido", "02 Alive (feat. Guest).flac", "02 Alive.flac", "has an ISRC"),
    ("Sourcey", "02 song.flac", "02 Song.flac", "from Tidarr, a preferred source"),
])
def test_keeper_by_metadata(loose, artist, kept, removed, why):
    files = {f["name"]: f for r in loose[artist]["rows"] for f in r["files"]}
    assert [n for n, f in files.items() if f["suggested"]] == [removed]
    assert files[kept]["keep_why"] == why
    assert files[removed]["keep_why"] == ""


def test_prefer_sources_can_be_turned_off(app_mod, scan, monkeypatch):
    monkeypatch.setattr(app_mod, "PREFER_SOURCES", [])
    s = scan()
    c = next(c for c in s["clusters"] if c["artist"] == "Sourcey")
    files = {f["name"]: f for r in c["rows"] for f in r["files"]}
    assert [n for n, f in files.items() if f["suggested"]] == ["02 song.flac"]
    assert files["02 Song.flac"]["keep_why"] == "more complete tags"


def test_source_rank_order(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "PREFER_SOURCES", ["tidarr", "qobuz"])
    r = app_mod.source_rank
    assert r("Tidarr") == r("Tidarr (SABnzbd) via Lidarr") == 2
    assert r("Qobuz") == 1 and r("MusicBrainz-tagged") == r(None) == 0


@pytest.mark.parametrize("name,title,want", [
    ("07 Queen Songs + human.flac", "Queen Songs / human.", True),
    ("07 Queen SongsHuman.flac", "Queen Songs / human.", False),
    ("01 The Beauty Between (feat. Andy Mineo).flac", "The Beauty Between (feat. Andy Mineo)", True),
    ("03 - What's Up.flac", "What\u2019s Up?", True),
    ("Song.flac", "", None),
])
def test_name_matches_title(app_mod, name, title, want):
    assert app_mod.name_matches_title({"rel": f"A/B/{name}", "title": title}) is want


def test_untouched_tags_beat_a_lidarr_retag(app_mod):
    base = dict(rel="A/02 X.flac", title="X", isrc="", source="Tidarr", tags={}, mtime=1,
                lossless=True, bits=16, rate=44100, kbps=900)
    retagged = dict(base, retag={"date": "2026-10-01", "fields": [], "scrubbed": True})
    assert app_mod.keep_rank(base) > app_mod.keep_rank(retagged)


def test_other_albums_select_nothing(loose):
    c = loose["Comp"]
    assert len(c["editions"]) == 3
    assert not any(f["suggested"] for r in c["rows"] for f in r["files"])
    assert not any(e["keep"] for e in c["editions"])


@pytest.mark.parametrize("a,b", [
    ("Nevermind", "The Very Best"), ("1,000 Names", "Ways"),
    ("Creature Comforts", "Everything at Once"), ("B-Sides", "Cannonball"),
    ("Bottle Rocket", "Smashes - The Best of Guardian"), ("JOY INVINCIBLE", "Christian Radio"),
    ("3 + 7", "Don't Know If I Believe It"), ("Greatest Hits - Chapter One", "Stronger"),
    ("Change Your World", "The First Decade (1983\u20131993)"),
])
def test_different_albums(app_mod, a, b):
    assert not app_mod.same_album(a, b)


@pytest.mark.parametrize("a,b", [
    ("Hearts of the Innocent", "Hearts Of The Innocent (Special Edition)"),
    ("Rec", "Rec (Deluxe)"), ("Persona", "Persona (Extended)"),
    ("Fractured Heart", "Fractioned Heart"), ("Pronounced", "pronounced"),
    ("Abbey Road", "Abbey Road (2019 Remaster)"), ("Abbey Road", "Abbey Road - Remastered 2009"),
    ("I Know a Ghost", "I Know A Ghost"), ("Now (2019)", "Now"),
])
def test_same_album(app_mod, a, b):
    assert app_mod.same_album(a, b)


def test_partial_deluxe_says_why(loose):
    assert loose["Dlx"]["detail"].startswith(
        "Album (Deluxe) (2019) would normally win as the bigger edition, but only 1 of its tracks is here")


def test_partial_copy_states_album_size(loose):
    c = loose["Hymnal"]
    assert "Take the World, but Give Me Jesus (2014) holds 2 of the album's 4 tracks" in c["detail"]
    tidarr = next(e for e in c["editions"] if e["folder"].endswith("Take the World, but Give Me Jesus (2014)"))
    assert (tidarr["tracks"], tidarr["album_total"]) == (2, 4)


@pytest.mark.parametrize("tags,want", [
    ({"totaltracks": "9"}, 9), ({"tracktotal": "12"}, 12), ({"tracknumber": "3/9"}, 9),
    ({"tracknumber": "3"}, None), ({}, None),
])
def test_track_total(app_mod, tags, want):
    assert app_mod.track_total({"tags": tags}) == want


def test_untouched_edition_beats_lidarr_retag(app_mod):
    """Two complete editions, identical audio, both labelled Tidarr: the one
    Lidarr didn't retag wins even though the retag added more tags."""
    def f(folder, retag):
        return {"folder": folder, "lossless": True, "bits": 16, "rate": 44100, "kbps": 900,
                "retag": retag}
    eds = [
        {"folder": "A/Pure (2014)", "artist": "A", "album": "Take the World, but Give Me Jesus", "tracks": 9,
         "deluxe": False, "source": "Tidarr", "untouched": 1.0, "tag_count": 15, "year": "2014",
         "album_total": 9},
        {"folder": "A/Hymns (2010)", "artist": "A", "album": "Hymns: Take the World, but Give Me Jesus", "tracks": 9,
         "deluxe": False, "source": "Tidarr (SABnzbd) via Lidarr", "untouched": 0.0,
         "tag_count": 30, "year": "2010", "album_total": 9},
    ]
    rows = [[f("A/Pure (2014)", None), f("A/Hymns (2010)", {"date": "x"})]]
    evs = [{"damaged": False, "isrc_conflict": False, "spread": 0, "confirmed": "identical",
            "identical": True, "differs": False, "has_isrc": False,
            "clean_mix": False, "fp_veto": False}]
    kind, reason, _, keeper = app_mod.classify(eds, rows, evs, single=False)
    assert (kind, reason, keeper) == ("suggested", "Identical audio", "A/Pure (2014)")


def test_remaster_wins_the_tie_and_says_so(loose):
    c = loose["Remas"]
    assert "Kept Album (Remastered) (1984): the remastered edition." in c["detail"]
    assert "different masters" in c["detail"]
    assert all("Different master" in [ch["text"] for ch in r["evidence"]] for r in c["rows"])
    assert [e["remaster"] for e in c["editions"]] == [False, True]


def test_prefer_remasters_can_be_turned_off(app_mod, scan, monkeypatch):
    monkeypatch.setattr(app_mod, "PREFER_REMASTERS", False)
    c = next(c for c in scan()["clusters"] if c["artist"] == "Remas")
    assert [e["folder"] for e in c["editions"] if e["keep"]] == ["Remas/Album (1984)"]


needs_fpcalc = pytest.mark.skipif(not shutil.which("fpcalc"), reason="fpcalc isn't installed")


@needs_fpcalc
def test_report_mode_shows_what_fingerprints_would_change(loose):
    assert loose["Printz"]["fp_would"] == {"kind": "suggested", "reason": "Same recordings"}
    assert loose["Swapt"]["fp_would"] == {"kind": "manual", "reason": "Audio doesn't match"}
    assert loose["Cleanly"]["fp_would"] is None
    assert all(r["fingerprint"] == "Fingerprint match 100%, full length."
               for r in loose["Printz"]["rows"])
    assert "different audio" in loose["Swapt"]["rows"][0]["fingerprint"]
    assert "clean or edited" in loose["Cleanly"]["rows"][0]["fingerprint"]
    # 5s tones are too short to judge: no verdict, nothing would change.
    assert loose["Blindside"]["fp_would"] is None
    assert loose["Blindside"]["rows"][0]["fingerprint"] == "Too short to compare fingerprints."


@needs_fpcalc
def test_fingerprints_on_confirm_and_veto(app_mod, scan, monkeypatch):
    monkeypatch.setattr(app_mod, "FINGERPRINT", "on")
    s = scan()
    assert s["total"] == s["scanned"] == len(list(app_mod.walk_audio()))  # not the fingerprint count
    got = {c["artist"]: c for c in s["clusters"]}
    c = got["Printz"]
    assert (c["kind"], c["reason"]) == ("suggested", "Same recordings")
    assert "acoustic fingerprint matches over the full length" in c["detail"]
    assert [os.path.basename(e["folder"]) for e in c["editions"] if e["keep"]] == ["Echoes (2019)"]
    for r in c["rows"]:
        assert [f["suggested"] for f in r["files"]].count(True) == 1
    c = got["Swapt"]
    assert (c["kind"], c["reason"]) == ("manual", "Audio doesn't match")
    assert not any(f["suggested"] for r in c["rows"] for f in r["files"])
    assert "Fingerprints differ" in [ch["text"] for ch in c["rows"][0]["evidence"]]
    assert (got["Cleanly"]["kind"], got["Cleanly"]["reason"]) == ("manual", "Clean and explicit versions")
    # Too-short fixtures abstain, so every other decision is unchanged.
    for artist, (kind, reason, _) in EXPECTED.items():
        if artist not in ("Printz", "Swapt"):
            assert (got[artist]["kind"], got[artist]["reason"]) == (kind, reason), artist


def test_fingerprints_off(app_mod, scan, monkeypatch):
    monkeypatch.setattr(app_mod, "FINGERPRINT", "off")
    got = {c["artist"]: c for c in scan()["clusters"]}
    assert got["Printz"]["fp_would"] is None
    assert all(r["fingerprint"] == "" for c in got.values() for r in c["rows"])
    assert got["Cleanly"]["reason"] == "Clean and explicit versions"  # a tag rule, not a fingerprint one


def test_fingerprint_compare_verdicts(app_mod):
    import random
    rnd = random.Random(1)
    a = [rnd.getrandbits(32) for _ in range(400)]
    flip = lambda x, n: x ^ sum(1 << b for b in rnd.sample(range(32), n))
    near = [flip(x, 2) for x in a]                      # another encode of the same audio
    other = [rnd.getrandbits(32) for _ in range(400)]  # unrelated audio
    padded = [rnd.getrandbits(32) for _ in range(9)] + a  # same audio after a short gap
    dipped = [flip(x, 14) if 160 <= i < 176 else x for i, x in enumerate(a)]  # a muted word
    cmp = app_mod.fp_compare
    assert app_mod.fp_verdict(cmp(a, near)) == "match"
    assert app_mod.fp_verdict(cmp(a, padded)) == "match"
    assert app_mod.fp_verdict(cmp(a, other)) == "differs"
    assert app_mod.fp_verdict(cmp(a, a[:280])) == "edit"
    assert app_mod.fp_verdict(cmp(a, dipped)) == "dips"
    assert cmp(a, a[:40]) is None  # too short to say
