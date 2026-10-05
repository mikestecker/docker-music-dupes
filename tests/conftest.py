import importlib
import os
import shutil
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def lib(tmp_path_factory):
    """Fixture library + config dir, built once per session."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required to build the fixture library")
    import mkfix
    base = tmp_path_factory.mktemp("lib")
    music, config = base / "music", base / "config"
    mkfix.build(str(music))
    return music, config


@pytest.fixture(scope="session")
def app_mod(lib):
    # app.py reads its config from the environment at import time.
    music, config = lib
    os.environ.update(MUSIC_DIR=str(music), CONFIG_DIR=str(config))
    for k in ("NAVIDROME_URL", "NAVIDROME_USER", "NAVIDROME_PASSWORD", "NAVIDROME_DB",
              "NAVIDROME_MUSIC_ROOT", "LIDARR_URL", "LIDARR_API_KEY"):
        os.environ.pop(k, None)
    return importlib.import_module("app")


@pytest.fixture(scope="session")
def client(app_mod):
    from fastapi.testclient import TestClient
    return TestClient(app_mod.app)


@pytest.fixture(scope="session")
def scan(client):
    def run():
        r = client.post("/api/scan", json={})
        assert r.status_code == 200, r.text
        deadline = time.time() + 60
        while client.get("/api/scan").json()["status"] == "scanning":
            assert time.time() < deadline, "scan timed out"
            time.sleep(0.05)
        s = client.get("/api/scan").json()
        assert s["status"] == "done", s["error"]
        return s
    return run
