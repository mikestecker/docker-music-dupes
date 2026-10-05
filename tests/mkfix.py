"""Build the fixture library: short sine-wave files with exact tags covering
every duplicate pattern in docs/HANDOFF.md section 1. Needs ffmpeg.

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


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join("test-output", "music")
    build(out)
    print(f"Fixture library written to {out}")
