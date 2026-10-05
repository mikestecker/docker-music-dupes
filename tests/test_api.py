"""HTTP API behaviour and the safety invariants in docs/HANDOFF.md section 5."""
import os


def test_health_and_info(client, lib):
    assert client.get("/healthz").json() == {"ok": True}
    info = client.get("/api/info").json()
    assert info["music"] == os.path.realpath(lib[0])
    assert info["navidrome"] is None and info["lidarr"] is False


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text


def test_bad_mode_and_unconfigured_navidrome(client):
    assert client.post("/api/scan", json={"mode": "nope"}).status_code == 400
    assert client.post("/api/scan", json={"mode": "navidrome"}).status_code == 400


def test_audio_range_and_containment(client, scan):
    scan()
    rel = "Band/Album (2010)/02 Two.flac"
    r = client.get("/api/audio", params={"rel": rel}, headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100
    assert r.headers["content-type"] == "audio/flac"
    assert client.get("/api/audio", params={"rel": "../../etc/passwd"}).status_code == 400
    assert client.get("/api/audio", params={"rel": ".dupe-quarantine/.ndignore"}).status_code == 400
    assert client.get("/api/audio", params={"rel": "Band/missing.flac"}).status_code == 404


def test_quarantine_never_removes_every_copy(client, scan, lib):
    s = scan()
    band = next(c for c in s["clusters"] if c["artist"] == "Band")
    every = [f["rel"] for f in band["rows"][0]["files"]]
    r = client.post("/api/quarantine", json={"paths": every}).json()
    assert r["moved"] == 0 and r["errors"]
    for rel in every:
        assert (lib[0] / rel).exists()


def test_quarantine_ignores_paths_not_in_results(client, scan, lib):
    scan()
    r = client.post("/api/quarantine", json={"paths": ["../outside.flac", "Nope/x.flac"]}).json()
    assert r["moved"] == 0


def test_quarantine_restore_roundtrip(client, scan, lib):
    music = lib[0]
    s = scan()
    picks = [f["rel"] for c in s["clusters"] if c["kind"] == "suggested"
             for r in c["rows"] for f in r["files"] if f["suggested"]]
    assert picks
    r = client.post("/api/quarantine", json={"paths": picks}).json()
    assert r["moved"] == len(picks) and not r["errors"]
    batch = r["batch"]
    for rel in picks:
        assert not (music / rel).exists()
        assert (music / ".dupe-quarantine" / batch / rel).exists()
    assert (music / ".dupe-quarantine" / ".ndignore").read_text() == "*\n"

    listed = client.get("/api/quarantine").json()
    assert listed[0]["batch"] == batch and len(listed[0]["files"]) == len(picks)

    r = client.post("/api/restore", json={"batch": batch}).json()
    assert r == {"restored": len(picks), "errors": []}
    for rel in picks:
        assert (music / rel).exists()
    assert client.get("/api/quarantine").json() == []


def test_restore_never_overwrites(client, scan, lib):
    music = lib[0]
    scan()
    rel = "Band/Album (2010)/02 Two.m4a"
    batch = client.post("/api/quarantine", json={"paths": [rel]}).json()["batch"]
    (music / rel).write_bytes(b"squatter")
    r = client.post("/api/restore", json={"batch": batch}).json()
    assert r["restored"] == 0 and r["errors"]
    assert (music / rel).read_bytes() == b"squatter"
    (music / rel).unlink()
    assert client.post("/api/restore", json={"batch": batch}).json()["restored"] == 1


def test_purge(client, scan, lib):
    # Uses its own extra copy so the fixture library is unchanged afterwards.
    music = lib[0]
    src = music / "Band/Album (2010)/02 Two.flac"
    extra = music / "Band/Album (2010)/02 Two (1).flac"
    extra.write_bytes(src.read_bytes())
    try:
        scan()
        r = client.post("/api/quarantine", json={"paths": [str(extra.relative_to(music))]}).json()
        assert r["moved"] == 1
        assert client.post("/api/purge", json={"batch": "../../etc"}).status_code == 404
        assert client.post("/api/purge", json={"batch": r["batch"]}).json()["bytes"] > 0
        assert not (music / ".dupe-quarantine" / r["batch"]).exists()
        assert not extra.exists() and src.exists()
    finally:
        extra.unlink(missing_ok=True)


def test_keep_both_persists(client, scan, lib):
    s = scan()
    key = next(c["key"] for c in s["clusters"] if c["artist"] == "Josh Garrels")
    client.post("/api/keep-both", json={"key": key, "kept": True})
    c = next(c for c in scan()["clusters"] if c["key"] == key)
    assert c["ignored"] is True
    client.post("/api/keep-both", json={"key": key, "kept": False})
    c = next(c for c in scan()["clusters"] if c["key"] == key)
    assert c["ignored"] is False
