"""IPC roundtrip + command dispatch (daemon behavior, offline)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from muse.daemon import commands as C
from muse.daemon import ipc


@pytest.fixture()
def registry(tmp_dirs, monkeypatch):
    """Shared registry (exactly what the daemon uses) so handle() sees one
    engine + DB across requests; engine runs without an audio device."""
    monkeypatch.setattr(C.audio.Engine, "start", lambda self: None)
    reg = C.enable_shared_registry()
    import muse.db as dbmod
    reg.database = dbmod.default_db()
    reg.engine = C.audio.Engine()
    return reg


def test_handle_version(tmp_dirs):
    from muse import __version__
    from muse.daemon.commands import handle
    assert handle("version", {})["version"] == __version__


def test_ipc_roundtrip(tmp_dirs, registry, monkeypatch):
    """Full socket roundtrip: status/play/queue through real IPC server."""
    import tempfile
    sock = tempfile.gettempdir() + "/muse-test-roundtrip.sock"  # short AF_UNIX path
    monkeypatch.setattr(ipc, "socket_path", lambda: sock)
    srv = ipc.start_ipc(sock)
    try:
        # play a real synthetic file (engine start is stubbed; Voice decode happens)
        from muse.analysis.synth import click_track
        path = click_track(tmp_dirs / "ir.wav", 120.0, bars=4)
        tid = registry.database.upsert_track(title="IR", file_path=path)
        r1 = ipc.request("play", {"track_id": tid})
        assert r1.get("ok"), r1
        assert r1["playing"] == "IR"
        r2 = ipc.request("status")
        assert r2["state"] == "playing"
        r3 = ipc.request("pause")
        assert r3["state"] == "paused"
        r4 = ipc.request("resume")
        assert r4["state"] == "playing"
        r5 = ipc.request("queue", {"action": "add", "ref": str(tid)})
        assert r5["added"] == 1
        r6 = ipc.request("queue", {"action": "clear"})
        assert r6.get("cleared") or r6.get("queue") == []
        # unknown command
        r7 = ipc.request("nosuch")
        assert not r7["ok"] and "unknown" in r7["error"]
    finally:
        srv.shutdown()


def test_direct_cli_fallback(tmp_dirs, monkeypatch):
    """With no daemon, `_call` runs commands in-process."""
    resp = C.handle("status", {})
    assert "state" in resp


def test_volume_commands(registry):
    r = registry.cmd_volume("60")
    assert r["volume"] == 60
    r = registry.cmd_volume("up")
    assert r["volume"] == 65
    r = registry.cmd_volume("down")
    assert r["volume"] == 60
    r = registry.cmd_volume("badinput")
    assert "error" in r


def test_automix_config(registry):
    r = registry.cmd_automix("off")
    assert r["enabled"] is False
    r = registry.cmd_automix("on")
    assert r["enabled"] is True
    r = registry.cmd_automix("length", "6")
    assert r["transition_length"] == 6.0
    r = registry.cmd_automix("length", "500")  # clamped
    assert r["transition_length"] == 16.0


def test_playback_flow_with_drm_track(registry, tmp_dirs):
    """Apple provider tracks fail with the DRM notice, never decode attempts."""
    tid = registry.database.upsert_track(title="Apple Song", provider="apple",
                                         file_path=None)
    r = registry.cmd_play(track_id=tid)
    assert "DRM" in r["error"] or "drm" in r["error"].lower()


def test_playlist_flow(registry):
    r = registry.cmd_playlist("create", "Rock")
    assert r["name"] == "Rock"
    tid = registry.database.upsert_track(title="Q", file_path="/tmp/q.wav")
    r = registry.cmd_playlist("add", "Rock", str(tid))
    assert r["added"] == 1
    r = registry.cmd_playlist("show", "Rock")
    assert r["tracks"][0]["title"] == "Q"