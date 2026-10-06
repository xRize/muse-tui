"""TUI smoke test with Textual's pilot harness (no daemon; direct fallback)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def test_tui_mounts_and_shows_status(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MUSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MUSE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MUSE_TEST_DB", str(tmp_path / "db.sqlite"))
    # force direct (in-process) mode even if a real daemon is up
    from muse.daemon import ipc
    monkeypatch.setattr(ipc, "socket_path", lambda: "/tmp/muse-not-running.sock")
    from muse.tui import MuseTUI

    app = MuseTUI()
    async with app.run_test() as pilot:
        await pilot.pause()
        # now-playing label exists and was populated
        np_title = app.query_one("#np-title")
        assert np_title is not None
        await pilot.press("space")       # toggle (idle -> engine exists, no crash)
        await pilot.pause()
        await pilot.press("q")           # quit binding registered


async def test_tui_search_populates_results(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MUSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MUSE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MUSE_TEST_DB", str(tmp_path / "db.sqlite"))
    from muse.daemon import ipc
    monkeypatch.setattr(ipc, "socket_path", lambda: "/tmp/muse-not-running.sock")
    from muse.analysis import worker as aw
    from muse.analysis.synth import click_track
    from muse.db import default_db

    db = default_db()
    path = click_track(tmp_path / "smoke.wav", 120.0, bars=4)
    aw.import_file(db, path)
    db.close()

    from muse.tui import MuseTUI

    app = MuseTUI()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.run_search("smoke")
        await pilot.pause()
        await pilot.pause()
        lv = app.query_one("#results")
        assert len(lv.children) >= 1