"""End-to-end: import -> analyze -> search -> queue -> status in offline mode."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from muse.analysis.synth import click_track, hybrid_track


@pytest.fixture()
def offline_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MUSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MUSE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MUSE_TEST_DB", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("MUSE_OFFLINE", "1")
    return tmp_path


def test_full_pipeline(offline_env, capsys, monkeypatch):
    """muse import -> analyze -> search -> queue -> automix preview, all offline."""
    # generate 2 audio files with known tempo keys
    click_track(offline_env / "song1.wav", 120.0, bars=8)
    hybrid_track(offline_env / "song2.wav", 100.0, 9, seconds=12.0)

    from muse.daemon import commands as C

    # stub engine start (no device in CI)
    monkeypatch.setattr(C.audio.Engine, "start", lambda self: None)
    reg = C.MuseCommands()
    import muse.db as dbmod
    reg.database = dbmod.default_db()

    # 1. import
    r = reg.cmd_import(str(offline_env / "song1.wav"))
    assert r["imported"] == 1, r
    r = reg.cmd_import(str(offline_env / "song2.wav"))
    assert r["imported"] == 1
    stats = reg.database.stats()
    assert stats["tracks"] == 2

    # 2. analyze all
    r = reg.cmd_analyze(all_pending=True)
    assert len(r["analyzed"]) == 2, r
    a1 = reg.database.get_analysis(r["analyzed"][0])
    assert a1["bpm"] > 0

    # 3. search
    r = reg.cmd_search("song1")
    assert len(r["results"]) == 1
    tid1 = r["results"][0]["id"]

    # 4. queue add + status
    r = reg.cmd_queue("add", str(tid1))
    assert r["added"] == 1
    st = reg.cmd_status()
    assert st["state"] == "idle" and st["library"]["tracks"] == 2

    # 5. automix preview between the two
    rows = reg.database.search_tracks("song", limit=2)
    ids = sorted(tr["id"] for tr in rows)
    r = reg.cmd_automix_preview(str(ids[0]), str(ids[1]))
    assert "Transition Score" in r["text"], r
    assert 0 <= r["plan"]["score"] <= 100

    # 6. radio builds a queue without repeating
    r = reg.cmd_radio(str(ids[0]), length=2)
    assert len(r["radio"]) == 2
    assert r["radio"][0]["id"] != r["radio"][1]["id"]

    # 7. lyrics/cover offline paths return gracefully
    r = reg.cmd_lyrics(str(ids[0]))
    assert "lyrics" in r
    r = reg.cmd_cover(str(ids[0]))
    assert "cover" in r


def test_daemon_process_lifecycle(offline_env, monkeypatch):
    """Real daemon process: start, query socket, stop cleanly."""
    import subprocess
    import tempfile
    import time

    from muse.daemon import ipc as ipcmod

    sock = tempfile.gettempdir() + "/muse-lifecycle-test.sock"  # short AF_UNIX path
    monkeypatch.setattr(ipcmod, "socket_path", lambda: sock)

    env = dict(os.environ)
    env["MUSE_SOCKET"] = sock
    stderr_file = offline_env / "daemon-stderr.log"
    proc = subprocess.Popen(
        [sys.executable, "-m", "muse.daemon.main"], env=env,
        stdout=subprocess.DEVNULL, stderr=stderr_file.open("w"), start_new_session=True)
    try:
        # wait for readiness
        deadline = time.time() + 10
        resp = {}
        while time.time() < deadline:
            try:
                resp = ipcmod.request("version", timeout=1.0)
                if resp.get("ok"):
                    break
            except Exception:
                pass
            time.sleep(0.2)
        assert resp.get("ok"), f"daemon never ready: {resp}; stderr: {stderr_file.read_text()[-2000:]}"
        assert resp["version"]
        st = ipcmod.request("status")
        assert st.get("state") in ("idle", "playing", "paused")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_cli_human_output(offline_env, monkeypatch, capsys):
    """CLI human output for search/status (direct mode: no daemon)."""
    from muse.daemon import ipc
    monkeypatch.setattr(ipc, "socket_path", lambda: "/tmp/muse-not-running.sock")
    from muse import cli
    from muse.analysis.synth import click_track
    path = click_track(offline_env / "cli.wav", 120.0, bars=4)

    # import first (direct mode; no daemon)
    rc = cli.main(["import", str(path)])
    assert rc == 0
    rc = cli.main(["search", "cli"])
    out = capsys.readouterr().out
    assert "cli" in out.lower()
    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert "state" in out
    rc = cli.main(["--json", "status"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["ok"] and "state" in data