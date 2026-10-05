"""Build the fixture library: short sine-wave files with exact tags covering
every duplicate pattern the app handles (see EXPECTED in tests/test_classify.py).
Needs ffmpeg.

Identical frequency + duration produces identical FLAC MD5s, which is how the
"identical audio" cases are made.

Usage: python tests/mkfix.py [output dir]   (default: ./test-output/music)
"""
import os
import subprocess
import sys

FL = ["-c:a", "flac"]
HR = ["-c:a", "flac", "-sample_fmt", "s32", "-ar", "48000"]
AAC = ["-c:a", "aac", "-b:a", "256k"]


def build(root):
    def mk(freq, codec, dur, path, title, artist, aa, album, date, track, isrc=None,
           mtime=None, **extra):
        p = os.path.join(root, path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        md = dict(title=title, artist=artist, album_artist=aa, album=album,
                  date=date, track=str(track))
        if isrc:
            md["ISRC"] = isrc
        md.update(extra)
        args = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
                "-i", f"sine=f={freq}:d={dur}"] + codec
        for k, v in md.items():
            args += ["-metadata", f"{k}={v}"]
        subprocess.run(args + [p], check=True)
        if mtime:
            os.utime(p, (mtime, mtime))

    for y in (2000, 2007):
        for t in (1, 2):
            mk(400 + t, FL, 5, f"Blindside/A Thought Crushed My Mind ({y})/0{t} Song{t}.flac",
               f"Song{t}", "Blindside", "Blindside", "A Thought Crushed My Mind", str(y), t, f"USAAA0000{t}")
    for t in (1, 2):
        mk(500 + t, FL, 5, f"Kutless/Hearts of the Innocent (2006)/0{t} T{t}.flac",
           f"T{t}", "Kutless", "Kutless", "Hearts of the Innocent", "2006", t)
    for t in (1, 2, 3):
        mk(500 + t, FL, 5, f"Kutless/Hearts Of The Innocent (Special Edition) (2006)/0{t} T{t}.flac",
           f"T{t}", "Kutless", "Kutless", "Hearts Of The Innocent (Special Edition)", "2006", t)
    mk(600, HR, 5, "Kings Kaleidoscope Hymns/Asaph's Arrows II (2025)/01 Grace.flac",
       "Grace", "Kings Kaleidoscope", "Kings Kaleidoscope Hymns", "Asaph's Arrows II", "2025", 1, "USKK1")
    mk(600, FL, 5, "Kings Kaleidoscope/Asaph's Arrows II (2025)/01 Grace.flac",
       "Grace", "Kings Kaleidoscope", "Kings Kaleidoscope", "Asaph's Arrows II", "2025", 1, "USKK1")
    mk(700, HR, 5, "Chris Renzema/Manna (2023)/01 Narrow Road.flac",
       "Narrow Road", "Chris Renzema", "Chris Renzema", "Manna", "2023", 1, "USRZ1")
    mk(700, FL, 5, "Chris Renzema/Manna (2024)/01 Narrow Road.flac",
       "Narrow Road", "Chris Renzema", "Chris Renzema", "Manna", "2024", 1, "USRZ1")
    mk(800, FL, 9, "Lynyrd Skynyrd/Pronounced (1973)/08 Free Bird.flac",
       "Free Bird", "Lynyrd Skynyrd", "Lynyrd Skynyrd", "Pronounced", "1973", 8)
    mk(800, AAC, 16, "Lynyrd Skynyrd/pronounced (1973)/08 Free Bird.m4a",
       "Free Bird", "Lynyrd Skynyrd", "Lynyrd Skynyrd", "pronounced", "1973", 8)
    mk(900, FL, 5, "Josh Garrels/Love & War (2011)/03 Farther Along.flac",
       "Farther Along", "Josh Garrels", "Josh Garrels", "Love & War", "2011", 3, "USGA1")
    mk(901, FL, 5, "Josh Garrels/Love & War (2024)/03 Farther Along.flac",
       "Farther Along", "Josh Garrels", "Josh Garrels", "Love & War", "2024", 3, "USGA2")
    mk(1000, FL, 5, "Gable Price/Fractured Heart (2020)/01 Heretic.flac",
       "Heretic", "Gable Price", "Gable Price", "Fractured Heart", "2020", 1)
    mk(1001, FL, 5, "Gable Price/Fractioned Heart (2020)/01 Heretic.flac",
       "Heretic", "Gable Price", "Gable Price", "Fractioned Heart", "2020", 1)
    mk(1100, FL, 5, "Flyleaf/Flyleaf (2005)/01 I'm So Sick.flac",
       "I'm So Sick", "Flyleaf", "Flyleaf", "Flyleaf", "2005", 1)
    mk(1100, FL, 5, "Flyleaf/Flyleaf (2005)/01 I’m So Sick.flac",
       "I’m So Sick", "Flyleaf", "Flyleaf", "Flyleaf", "2005", 1)
    mk(1200, FL, 5, "Band/Album (2010)/02 Two.flac", "Two", "Band", "Band", "Album", "2010", 2)
    mk(1200, AAC, 5, "Band/Album (2010)/02 Two.m4a", "Two", "Band", "Band", "Album", "2010", 2)
    mk(1300, HR, 5, "Hres/Rec (2015)/01 X.flac", "X", "Hres", "Hres", "Rec", "2015", 1, "USHR1")
    mk(1300, FL, 5, "Hres/Rec (Deluxe) (2015)/01 X.flac", "X", "Hres", "Hres", "Rec (Deluxe)", "2015", 1, "USHR1")
    mk(1301, FL, 5, "Hres/Rec (Deluxe) (2015)/02 Y.flac", "Y", "Hres", "Hres", "Rec (Deluxe)", "2015", 2)
    # A later stray re-download in an album folder: other naming, another day,
    # and more tags than the album's copy (which used to make it the keeper).
    sep23, oct3 = 1758652260, 1759531620
    for i, (d, t, title) in enumerate([(1, 1, "Ashley"), (1, 2, "Clementine"), (1, 3, "Graveyard"),
                                       (2, 1, "Wipe Your Tears"), (2, 2, "Be Kind")]):
        mk(1400 + i, FL, 5, f"Mixtape/Manic (2020)/{d:02d}-{t:02d} {title}.flac", title,
           "Mixtape", "Mixtape", "Manic", "2020", t, mtime=sep23 + i, disc=str(d))
    mk(1402, FL, 5, "Mixtape/Manic (2020)/03 Graveyard.flac", "Graveyard", "Mixtape", "Mixtape",
       "Manic", "2020", 3, mtime=oct3, comment="tidal", label="Some Label", copyright="2020")
    # Same tags, different master: the album's 16/44.1 copy vs a later 16/48
    # stray named "20 Title" though its tags say disc 2, track 5.
    sep22, oct3b = 1758575160, 1759531500
    for i, (d, t, title, dur) in enumerate([(1, 1, "Why", 5), (2, 4, "Any Old Barstool", 5),
                                            (2, 5, "You Make It Easy", 9), (2, 6, "Drowns the Whiskey", 5)]):
        mk(1500 + i, FL, dur, f"Countryman/Hits (2025)/{d:02d}-{t:02d} {title}.flac", title,
           "Countryman", "Countryman", "Hits", "2025", t, mtime=sep22 + i, disc=str(d))
    mk(1550, ["-c:a", "flac", "-ar", "48000"], 5.5, "Countryman/Hits (2025)/20 You Make It Easy.flac",
       "You Make It Easy", "Countryman", "Countryman", "Hits", "2025", 5, mtime=oct3b, disc="2")
    # Tidarr grabbed three tracks at 24-bit into its own artist folder (Tidal's
    # middle-dot spelling, ffmpeg container tags, a featuring credit in a title)
    # while Lidarr has the complete album at 16-bit under MusicBrainz's bullet.
    HR441 = ["-c:a", "flac", "-sample_fmt", "s32", "-ar", "44100"]
    tidarr = {"compatible_brands": "mp41dashcmfc", "major_brand": "iso8"}
    for freq, t, title in [(1600, 2, "Runaway"), (1601, 10, "Rest (with Samm Henshaw)"),
                           (1602, 12, "Creature")]:
        mk(freq, HR441, 5, f"Half\u00b7Alive/Now (2019)/{t:02d} {title}.flac", title,
           "Half\u00b7Alive", "Half\u00b7Alive", "Now", "2019", t, mtime=oct3b + t, **tidarr)
    for freq, t, title in [(1610, 1, "Ok"), (1600, 2, "Runaway"), (1611, 3, "Maybe"),
                           (1601, 10, "Rest"), (1602, 12, "Creature")]:
        mk(freq, FL, 5, f"Half\u2022Alive/Now (2019)/{t:02d} {title}.flac", title,
           "Half\u2022Alive", "Half\u2022Alive", "Now", "2019", t, mtime=sep22 + t)
    # A partial lossy copy of an album that's complete in FLAC: suggested outright.
    for freq, t, title in [(1700, 1, "One"), (1701, 2, "Two"), (1702, 3, "Three"), (1703, 4, "Four")]:
        mk(freq, FL, 5, f"Partly/Album (2018)/{t:02d} {title}.flac", title,
           "Partly", "Partly", "Album", "2018", t)
    for freq, t, title in [(1701, 2, "Two"), (1702, 3, "Three")]:
        mk(freq, AAC, 5, f"Partly/Album (2018) (1)/{t:02d} {title}.m4a", title,
           "Partly", "Partly", "Album", "2018", t)
    # Untagged rips: only the folder + filename key can pair them.
    for name in ("Song.flac", "Song (1).flac"):
        p = os.path.join(root, "Untagged/Rips", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=1800:d=5",
                        "-c:a", "flac", "-map_metadata", "-1", p], check=True)
    # Same album slot, different track credits: only the album key pairs them.
    mk(1900, FL, 5, "Credits/Album (2021)/03 Song.flac", "Song", "Credits, Guest", "Credits",
       "Album", "2021", 3)
    mk(1900, FL, 5, "Credits/Album (2021) (Tidal)/03 Song.flac", "Song", "Credits", "Credits",
       "Album", "2021", 3)
    # Which copy stays when the audio is identical. In each folder the copy that
    # should lose is older and has more tags, so only the metadata checks save it.
    t0 = 1758990000
    extra = dict(label="L", copyright="C", bpm="90", comment="x", genre="G")
    for i, (artist, base) in enumerate([("Peppy", 2000), ("Kaleido", 2100), ("Sourcey", 2200)]):
        for t, title in [(1, "Opener"), (3, "Middle"), (4, "Closer")]:
            mk(base + t, FL, 5, f"{artist}/Album (2019)/{t:02d} {title}.flac", title,
               artist, artist, "Album", "2019", t, mtime=t0 + t)
    # A mangled filename ("/" dropped) loses to one that matches the title.
    mk(2002, FL, 5, "Peppy/Album (2019)/02 Queen SongsHuman.flac", "Queen Songs / human.",
       "Peppy", "Peppy", "Album", "2019", 2, mtime=t0 + 10, **extra)
    mk(2002, FL, 5, "Peppy/Album (2019)/02 Queen Songs + human.flac", "Queen Songs / human.",
       "Peppy", "Peppy", "Album", "2019", 2, mtime=t0 + 600)
    # A copy with an ISRC beats one a retag stripped.
    mk(2102, FL, 5, "Kaleido/Album (2019)/02 Alive.flac", "Alive",
       "Kaleido", "Kaleido", "Album", "2019", 2, mtime=t0 + 10, **extra)
    mk(2102, FL, 5, "Kaleido/Album (2019)/02 Alive (feat. Guest).flac", "Alive (feat. Guest)",
       "Kaleido", "Kaleido", "Album", "2019", 2, "USKK20000001", mtime=t0 + 600)
    # Otherwise equal: the preferred source (Tidarr) wins.
    mk(2202, FL, 5, "Sourcey/Album (2019)/02 Song.flac", "Song",
       "Sourcey", "Sourcey", "Album", "2019", 2, mtime=t0 + 10, **extra)
    mk(2202, FL, 5, "Sourcey/Album (2019)/02 song.flac", "Song",
       "Sourcey", "Sourcey", "Album", "2019", 2, mtime=t0 + 600,
       compatible_brands="mp41dashcmfc", major_brand="iso8")
    # One recording on three releases (album, best-of, single): all stay.
    for folder, album, t, date in [("Album (2010)", "Album", 3, "2010"),
                                   ("The Best Of (2015)", "The Best Of", 7, "2015"),
                                   ("Hit (2009)", "Hit", 1, "2009")]:
        mk(2300, FL, 5, f"Comp/{folder}/{t:02d} Hit.flac", "Hit", "Comp", "Comp", album, date, t,
           "USCO10000001")
    for t, title in [(1, "Intro"), (2, "Other")]:
        mk(2300 + t, FL, 5, f"Comp/Album (2010)/{t:02d} {title}.flac", title, "Comp", "Comp",
           "Album", "2010", t)
    # A deluxe folder holding just one track of the standard album: the
    # standard is the complete one, so the deluxe copy is the partial.
    for t, title in [(1, "A"), (2, "B"), (3, "C")]:
        mk(2400 + t, FL, 5, f"Dlx/Album (2019)/{t:02d} {title}.flac", title, "Dlx", "Dlx",
           "Album", "2019", t)
    mk(2402, FL, 5, "Dlx/Album (Deluxe) (2019)/02 B.flac", "B", "Dlx", "Dlx",
       "Album (Deluxe)", "2019", 2)
    # Lidarr's complete album (its tags say 4 tracks) vs a Tidarr folder with
    # 2 of them under Tidal's shorter album name.
    for t, title in [(1, "Love"), (2, "Great"), (3, "Rock"), (4, "World")]:
        mk(2500 + t, FL, 5, f"Hymnal/Hymns - Take the World, but Give Me Jesus (2010)/{t:02d} {title}.flac", title,
           "Hymnal", "Hymnal", "Hymns: Take the World, but Give Me Jesus", "2010", t, TOTALTRACKS="4")
    for t, title in [(1, "Love"), (2, "Great")]:
        mk(2500 + t, FL, 5, f"Hymnal/Take the World, but Give Me Jesus (2014)/{t:02d} {title}.flac", title,
           "Hymnal", "Hymnal", "Take the World, but Give Me Jesus", "2014", t, compatible_brands="mp41dashcmfc",
           major_brand="iso8")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join("test-output", "music")
    build(out)
    print(f"Fixture library written to {out}")
