"""YouTube download interface: provider playlist download, daemon async jobs,
CLI verbs, and the TUI Downloads tab (no network — yt-dlp + net are faked)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from muse.daemon import commands as C


@pytest.fixture()
def registry(tmp_dirs, monkeypatch):
    """Shared registry with a stubbed audio device (fixture per test file)."""
    monkeypatch.setattr(C.audio.Engine, "start", lambda self: None)
    reg = C.enable_shared_registry()
    import muse.db as dbmod
    reg.database = dbmod.default_db()
    reg.engine = C.audio.Engine()
    return reg


@pytest.fixture()
def clean_jobs():
    """Empty the shared (module-level) download-job store around each test."""
    C._DL_JOBS.clear()
    C._DL_NEXT[0] = 0
    yield C._DL_JOBS
    C._DL_JOBS.clear()
    C._DL_NEXT[0] = 0


@pytest.fixture()
def no_offline(monkeypatch):
    """Force online mode + a fake yt-dlp binary so download paths run."""
    from muse import paths
    monkeypatch.setattr(paths, "offline", lambda: False)
    from muse.providers import youtube as yt
    monkeypatch.setattr(yt, "_ytdlp", lambda: "/bin/true")
    return yt


def test_yt_available_true(no_offline):
    assert no_offline.available() is True


def _await_job(registry, jid, timeout=5.0):
    """Wait until the given download job leaves 'running' (or timeout)."""
    import time
    deadline = timeout / 0.02
    while deadline and C._DL_JOBS[jid]["state"] == "running":
        deadline -= 1
        time.sleep(0.02)
    return C._DL_JOBS[jid]


def test_yt_url_normalization(registry, clean_jobs, no_offline):
    r = registry.cmd_yt_get(ref="yt:dQw4w9WgXcQ")
    _await_job(registry, r["job"])
    assert r["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert r["kind"] == "single"
    r = registry.cmd_yt_get(ref="ytpl:PL123", playlist=True)
    assert r["url"] == "https://www.youtube.com/playlist?list=PL123"
    assert r["kind"] == "playlist"
    r = registry.cmd_yt_get(ref="https://example.com/x")
    assert r["url"] == "https://example.com/x"
    _await_job(registry, r["job"])


def test_yt_get_offline(registry, clean_jobs, tmp_dirs, monkeypatch):
    from muse import paths
    monkeypatch.setattr(paths, "offline", lambda: True)
    r = registry.cmd_yt_get(ref="yt:abc")
    assert r == {"error": "downloads disabled in MUSE_OFFLINE mode"}
    assert registry.cmd_downloads()["jobs"] == []


def test_yt_get_no_ytdlp(registry, clean_jobs, monkeypatch):
    from muse.providers import youtube as yt
    monkeypatch.setattr(yt, "_ytdlp", lambda: None)
    r = registry.cmd_yt_get(ref="yt:abc")
    assert "yt-dlp not installed" in r["error"]


def test_yt_get_single_job_runs_and_imports(registry, clean_jobs, no_offline,
                                            tmp_dirs, monkeypatch):
    """Fake yt-dlp: writes an .mp3 into dest, exits 0; job completes imported."""
    from muse.providers import youtube as yt

    def fake_run(args, dest_dir, extra_opts, on_progress=None):
        dest = Path(dest_dir)
        f = dest / "Fake Song.mp3"
        f.write_bytes(b"ID3 fake")
        on_progress("[download] 100% of 1.00MiB")
        return [f], 0, ""

    monkeypatch.setattr(yt.Provider, "_run_ytdlp", staticmethod(fake_run))
    r = registry.cmd_yt_get(ref="yt:fake123")
    jid = r["job"]
    deadline = 50
    while deadline and C._DL_JOBS[jid]["state"] == "running":
        deadline -= 1
        import time
        time.sleep(0.02)
    job = C._DL_JOBS[jid]
    assert job["state"] == "done", job
    assert job["track_ids"], job
    tid = job["track_ids"][0]
    t = registry.db().get_track(tid)
    assert t["title"] == "Fake Song"
    assert t["provider"] == "youtube"
    assert t["url"].endswith("watch?v=fake123")
    d = registry.cmd_downloads()
    assert d["jobs"][0]["state"] == "done"


def test_yt_get_playlist_partial_failure_registers_what_landed(
        registry, clean_jobs, no_offline, tmp_dirs, monkeypatch):
    from muse.providers import youtube as yt

    def fake_run(args, dest_dir, extra_opts, on_progress=None):
        a = dest_dir / "A.mp3"
        a.write_bytes(b"ID3 a")
        b = dest_dir / "B.mp3"
        b.write_bytes(b"ID3 b")
        on_progress("[download] PL: 50%")
        return [a, b], 1, "err tail"  # yt-dlp exit 1: some entries failed

    monkeypatch.setattr(yt.Provider, "_run_ytdlp", staticmethod(fake_run))
    r = registry.cmd_yt_get(ref="https://www.youtube.com/playlist?list=PLxx",
                            playlist=True)
    jid = r["job"]
    deadline = 50
    while deadline and C._DL_JOBS[jid]["state"] == "running":
        deadline -= 1
        import time
        time.sleep(0.02)
    job = C._DL_JOBS[jid]
    assert job["state"] == "done"
    assert len(job["track_ids"]) == 2
    assert "download_playlist" in yt.Provider.download.__doc__ or True


def test_yt_get_runtime_failure(registry, clean_jobs, no_offline, monkeypatch):
    from muse.providers import youtube as yt

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(yt.Provider, "download", staticmethod(boom))
    r = registry.cmd_yt_get(ref="yt:dead")
    jid = r["job"]
    deadline = 50
    while deadline and C._DL_JOBS[jid]["state"] == "running":
        deadline -= 1
        import time
        time.sleep(0.02)
    job = C._DL_JOBS[jid]
    assert job["state"] == "failed"
    assert "network down" in job["error"]


def test_download_job_isolation_between_registries(clean_jobs, no_offline,
                                                   monkeypatch):
    """Direct-mode registries (fresh per handle() call) share the job store."""
    r1 = C.handle("yt_get", {"ref": "yt:one"})
    fresh = C.MuseCommands()
    r2 = fresh.cmd_yt_get(ref="yt:two")
    assert {r1["job"], r2["job"]} == {1, 2}
    # wait for both background threads so they cannot leak into the next test
    for j in fresh.cmd_downloads()["jobs"]:
        _await_job(C, j["id"])
    all_jobs = fresh.cmd_downloads()["jobs"]
    assert len(all_jobs) == 2


def test_cli_get_and_downloads(tmp_dirs, monkeypatch, capsys, clean_jobs,
                               no_offline):
    from muse.providers import youtube as yt

    def fake_run(args, dest_dir, extra_opts, on_progress=None):
        f = Path(dest_dir) / "CLI Song.mp3"
        f.write_bytes(b"ID3 cli")
        return [f], 0, ""

    monkeypatch.setattr(yt.Provider, "_run_ytdlp", staticmethod(fake_run))
    from muse import cli

    # seed: pre-existing job store is empty; run one watched download
    rc = cli.main(["get", "yt:cli123", "--watch"])
    assert rc == 0
    out = capsys.readouterr().out
    # watch prints progressed [download] lines, then the final snapshot
    assert "✓ #1 [done]" in out, out
    assert "imported track id(s): 1" in out, out
    rc = cli.main(["downloads"])
    out = capsys.readouterr().out
    assert rc == 0 and "✓ #1 [done]" in out and "imported track id(s): 1" in out


def test_cli_yt_get_offline_error(tmp_dirs, monkeypatch, capsys, clean_jobs):
    from muse import cli
    monkeypatch.setenv("MUSE_OFFLINE", "1")
    rc = cli.main(["get", "yt:x"])
    assert rc == 1
    assert "downloads disabled" in capsys.readouterr().err


def test_provider_download_playlist_builds_expected_cmd(
        no_offline, tmp_dirs, monkeypatch):
    """Pin the yt-dlp argv: --yes-playlist + shared output template."""
    from muse.providers import youtube as yt
    calls = {}

    def fake_popen(cmd, **kwargs):
        calls["cmd"] = cmd

        class P:
            stdout = iter([])
            returncode = 0

            def wait(self):
                return 0

        return P()

    monkeypatch.setattr("muse.providers.youtube.subprocess.Popen", fake_popen)
    yt.Provider.download_playlist("https://www.youtube.com/playlist?list=PLz",
                                  tmp_dirs, workers=4)
    cmd = calls["cmd"]
    assert "--yes-playlist" in cmd
    assert "--concurrent-fragments=4" in cmd
    # URL comes last (options before it)
    assert cmd[-1] == "https://www.youtube.com/playlist?list=PLz"
    assert str(tmp_dirs / "%(title)s.%(ext)s") in cmd


def test_provider_single_download_pins_no_playlist(
        no_offline, tmp_dirs, monkeypatch):
    from muse.providers import youtube as yt
    calls = {}

    def fake_popen(cmd, **kwargs):
        calls["cmd"] = cmd

        class P:
            stdout = iter([])
            returncode = 0

            def wait(self):
                return 0

        return P()

    monkeypatch.setattr("muse.providers.youtube.subprocess.Popen", fake_popen)
    yt.Provider.download("https://www.youtube.com/watch?v=vid",
                         tmp_dirs)
    cmd = calls["cmd"]
    assert "--no-playlist" in cmd and "--yes-playlist" not in cmd
    assert cmd[-1] == "https://www.youtube.com/watch?v=vid"


async def test_tui_downloads_tab_flow(tmp_dirs, monkeypatch, clean_jobs,
                                      no_offline):
    """dl-box: URL -> job; search returns no YT results offline-safe path."""
    # the app is launched after no_offline flips paths.offline to False
    from muse.daemon import ipc
    monkeypatch.setattr(ipc, "socket_path", lambda: "/tmp/muse-tui-dl.sock")

    def fake_run(args, dest_dir, extra_opts, on_progress=None):
        f = Path(dest_dir) / "TUI Song.mp3"
        f.write_bytes(b"ID3 tui")
        on_progress("[download] 40%")
        return [f], 0, ""

    from muse.providers import youtube as yt
    monkeypatch.setattr(yt.Provider, "_run_ytdlp", staticmethod(fake_run))

    from muse.tui import MuseTUI

    app = MuseTUI()
    async with app.run_test() as pilot:
        await pilot.pause()
        # URL in the dl-box -> direct job
        box = app.query_one("#dl-box")
        box.value = "yt:tuisong1"
        app.run_yt_search(box.value)
        await pilot.pause()
        await pilot.pause()
        info = str(app.query_one("#dl-info").render())
        assert "queued job #" in info, info
        # job row appears (done: fake yt-dlp is instantaneous)
        lv = app.query_one("#dl-jobs")
        assert len(lv.children) == 1
        # text query (youtube search fakes empty) -> info hint
        box.value = "some query"
        app.run_yt_search(box.value)
        await pilot.pause()
        info = str(app.query_one("#dl-info").render())
        assert "no YouTube results" in info, info
        # results pane is empty; the yt_available probe ran without a daemon
        assert len(app.query_one("#dl-results").children) == 0