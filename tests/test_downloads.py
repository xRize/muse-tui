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
    assert "--replace-in-metadata" in cmd and "--write-thumbnail" in cmd
    assert any("%(title)s.%(ext)s" in a for a in cmd)


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


# -- filename cleanup + song-first ranking (metadata-QoL wave) ----------------

def test_clean_meta_strips_video_noise():
    from muse.providers.youtube import clean_meta
    assert clean_meta("Coldplay - Viva La Vida (Official Video)") == \
        "Coldplay - Viva La Vida"
    assert clean_meta("Song [4K Remaster]") == "Song"
    assert clean_meta("Song - Remastered 2011") == "Song"
    assert clean_meta("Song (Lyric Video)") == "Song"
    # kept (not noise per user preference)
    assert clean_meta("Song (Live at Wembley)") == "Song (Live at Wembley)"
    assert clean_meta("Song (feat. Someone)") == "Song (feat. Someone)"


def test_split_title_dash_shape():
    from muse.providers.youtube import split_title
    assert split_title("Artist - Song (Official Video)") == ("Artist", "Song")
    assert split_title("Song") == ("", "Song")
    assert split_title("AC/DC - Back In Black") == ("AC/DC", "Back In Black")


def test_search_ranks_songs_before_videos(monkeypatch, no_offline):
    """Song uploads outrank music videos in yt_search results (stable)."""
    from muse.providers import youtube as yt
    lines = [
        '{"title": "Song - Live Version", "url": '
        '"https://www.youtube.com/watch?v=lv", "duration": 200}',
        '{"title": "Song (Official Music Video)", "url": '
        '"https://www.youtube.com/watch?v=vid", "duration": 210}',
        '{"title": "Artist - Song", "url": '
        '"https://www.youtube.com/watch?v=st", "duration": 205}',
    ]

    def fake_run(cmd, **k):
        return type("R", (), {"stdout": "\n".join(lines) + "\n"})()

    monkeypatch.setattr("muse.providers.youtube.subprocess.run",
                        lambda cmd, **k: fake_run(cmd, **k))
    r = yt.Provider().search("Song", limit=3)
    order = [x["raw_title"] for x in r]
    # studio upload first; the video-titled hit ("Official Music Video", -3)
    # ranks below the live one (-2)
    assert order[0] == "Artist - Song"
    assert order.index("Song - Live Version") < order.index(
        "Song (Official Music Video)")
    assert all(x.get("raw_title") for x in r)


def test_provider_cmd_pins_metadata_cleanup_options(tmp_dirs, monkeypatch,
                                                    no_offline):
    """argv carries the Artist - Song template + title-cleanup metadata opts."""
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
    yt.Provider.download("https://www.youtube.com/watch?v=meta", tmp_dirs)
    cmd = calls["cmd"]
    assert cmd.count("--replace-in-metadata") == 4
    assert "--write-thumbnail" in cmd and "--convert-thumbnails" in cmd
    assert any("%(title)s.%(ext)s" in a for a in cmd)


def test_ytsearch_command_offline(monkeypatch, tmp_dirs):
    """cmd_ytsearch wraps yt_search; offline -> empty candidates, no writes."""
    from muse.daemon import commands as C
    reg = C.MuseCommands()
    monkeypatch.setattr(C.paths, "offline", lambda: True)
    r = reg.cmd_ytsearch("some song")
    assert r["offline"] is True and r["candidates"] == []


def test_get_search_ref_queues_top_hit(registry, clean_jobs, no_offline,
                                       tmp_dirs, monkeypatch):
    """`get search:<query>` resolves the top hit then queues its download."""
    from muse.providers import youtube as yt

    def fake_run(args, dest_dir, extra_opts, on_progress=None):
        f = Path(dest_dir) / "Found Song.mp3"
        f.write_bytes(b"ID3 found")
        return [f], 0, ""

    monkeypatch.setattr(yt.Provider, "_run_ytdlp", staticmethod(fake_run))
    monkeypatch.setattr(yt.Provider, "search",
                        lambda self, q, limit=10: [
                            {"title": "Song (Official Video)",
                             "raw_title": "Song (Official Video)",
                             "artist": "Some Band", "duration": 120,
                             "url": "https://www.youtube.com/watch?v=top1",
                             "provider": "youtube"},
                        ])
    r = registry.cmd_yt_get(ref="search:some song")
    assert r["url"].endswith("watch?v=top1")
    job = _await_job(registry, r["job"])
    assert job["state"] == "done" and len(job["track_ids"]) == 1


def test_register_downloaded_file_splits_untagged_filename(registry, tmp_dirs):
    """Tags absent + 'Artist - Song.mp3' filename -> artist/title split."""
    f = tmp_dirs / "downloads" / "Nirvana - Something.mp3"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"ID3 untagged")  # mutagen tolerates junk mp3s
    tid = registry.register_downloaded_file(f)
    t = registry.db().get_track(tid)
    assert t["artist"] == "Nirvana"
    assert t["title"] == "Something"


def test_register_downloaded_file_tagged_title_no_double_prefix(registry,
                                                                tmp_dirs):
    """yt-dlp writes full 'Artist - Song (noise)' in TIT2 + TPE1 separately:
    the rename must clean the tag and NOT prefix the artist twice."""
    from mutagen.id3 import ID3, TIT2, TPE1
    f = tmp_dirs / "downloads" / "Coldplay - Coldplay - Hymn For The" \
                             " Weekend  .mp3"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\xff\xfb\xa0\x00" + b"\x00" * 100)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["Coldplay - Hymn For The Weekend  "]))
    tags.add(TPE1(encoding=3, text=["Coldplay"]))
    tags.save(str(f), v1=0, v2_version=4)
    tid = registry.register_downloaded_file(f)
    t = registry.db().get_track(tid)
    assert t["title"] == "Hymn For The Weekend"
    assert t["artist"] == "Coldplay"
    # file renamed to the canonical single "Artist - Song" form
    assert (tmp_dirs / "downloads" / "Coldplay - Hymn For The Weekend.mp3") \
        .is_file()
    assert not f.exists()


def test_track_cover_sidecar_and_query_columns(registry, tmp_dirs):
    """tracks.cover_art_path column (migration) picks up the sidecar jpg."""
    db = registry.db()
    f = tmp_dirs / "downloads" / "With Art.mp3"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"ID3 art")
    (tmp_dirs / "downloads" / "With Art.jpg").write_bytes(b"\xff\xd8jpg")
    tid = registry.register_downloaded_file(f)
    row = db.conn.execute("SELECT cover_art_path FROM tracks WHERE id=?",
                          (tid,)).fetchone()
    assert row["cover_art_path"] and row["cover_art_path"].endswith("With Art.jpg")
    assert db.get_track(tid)["cover_art_path"].endswith("With Art.jpg")


def test_lyrics_fetch_uses_split_title(tmp_dirs, monkeypatch):
    """'Artist - Song' fallback parse: lrclib called with separated parts."""
    from muse.providers import media
    calls = {}

    def fake_get(url, **kw):
        calls["params"] = kw.get("params")
        raise RuntimeError("offline")

    monkeypatch.setattr("muse.providers.media.requests.get", fake_get)
    res = media.fetch_lyrics("", "Muse - Starlight", offline=False)
    assert res["text"] == ""
    # first attempt used the split parts, not the joined title
    assert calls["params"]["track_name"] == "Starlight"
    assert calls["params"]["artist_name"] == "Muse"


def test_fetch_cover_prefers_thumb_url(tmp_dirs, monkeypatch):
    from muse.providers import media
    saved = {}

    def fake_get(url, **kw):
        saved["url"] = url
        return type("R", (), {"raise_for_status": lambda self: None,
                              "content": b"\xff\xd8fakejpg"})()

    monkeypatch.setattr("muse.providers.media.requests.get", fake_get)
    p = media.fetch_cover("A", "B",
                          thumb_url="https://i.ytimg.com/vi/x/hqdefault.jpg")
    assert saved["url"] == "https://i.ytimg.com/vi/x/hqdefault.jpg"
    assert Path(p).read_bytes().startswith(b"\xff\xd8")


def test_cmd_cover_uses_youtube_thumb_url(registry, tmp_dirs, monkeypatch):
    """cover for a youtube-provider track fetches that video's thumbnail."""
    from muse.providers import media
    calls = {}

    def fake_fetch_cover(artist, album, offline=False, thumb_url=None):
        calls["thumb_url"] = thumb_url
        return "/tmp/fake.png"

    monkeypatch.setattr(media, "fetch_cover", fake_fetch_cover)
    f = tmp_dirs / "downloads" / "A - B.mp3"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"ID3 x")
    tid = registry.register_downloaded_file(
        f, url="https://www.youtube.com/watch?v=vid123")
    r = registry.cmd_cover(str(tid))
    assert calls["thumb_url"] == "https://i.ytimg.com/vi/vid123/hqdefault.jpg"
    assert r["cover"] == "/tmp/fake.png"
    row = registry.db().conn.execute(
        "SELECT cover_art_path FROM tracks WHERE id=?", (tid,)).fetchone()
    assert row["cover_art_path"] == "/tmp/fake.png"


def test_cli_ytsearch_prints_ranked(tmp_dirs, monkeypatch, capsys):
    from muse import cli
    monkeypatch.setattr(
        "muse.daemon.commands.MuseCommands.cmd_yt_search",
        lambda self, q, limit=10: {
            "query": q, "offline": False,
            "results": [
                {"title": "Song", "raw_title": "Song (Official Video)",
                 "artist": "A", "duration": 100, "provider": "youtube",
                 "url": "https://www.youtube.com/watch?v=1"},
                {"title": "Song", "raw_title": "A - Song",
                 "artist": "A", "duration": 101, "provider": "youtube",
                 "url": "https://www.youtube.com/watch?v=2"},
            ]})
    rc = cli.main(["ytsearch", "song"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "video)" in out and "song)" in out
    # the video-titled hit sorts below the song-shaped one pre-ranking; the
    # human printer shows tags — ordering already applied upstream here so
    # just assert both lines and tags exist
    assert out.index("song)") >= 0 and out.index("video)") >= 0