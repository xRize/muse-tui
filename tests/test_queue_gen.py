"""Generate Queue command + import copy-on-managed-path + startup integrity
prune (metadata-QoL wave 2, spec §3/§2)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from muse.analysis import worker as aw
from muse.daemon import commands as C


@pytest.fixture()
def registry(tmp_dirs, monkeypatch):
    """Shared registry (daemon-style) with a stubbed audio device."""
    monkeypatch.setattr(C.audio.Engine, "start", lambda self: None)
    reg = C.enable_shared_registry()
    import muse.db as dbmod
    reg.database = dbmod.default_db()
    reg.engine = C.audio.Engine()
    return reg


def _click(registry, tmp_path, name, bpm=120.0, genre=None, bars=16):
    from muse.analysis import worker as aw
    from muse.analysis.synth import click_track
    path = click_track(str(tmp_path / f"{name}.wav"), bpm, bars=bars)
    tid = registry.database.upsert_track(title=name, file_path=path, genre=genre)
    aw.run_analysis(registry.database, tid)
    return tid


# -- cmd_genq -------------------------------------------------------------------
def test_genq_enqueues_similar_at_head(registry, tmp_dirs):
    db = registry.db()
    seed = _click(registry, tmp_dirs, "seed", bpm=120.0, bars=8)
    for i in range(3):
        _click(registry, tmp_dirs, f"sim{i}", bpm=120.0 + i)
    registry.cmd_play(track_id=seed)
    r = registry.cmd_genq()
    picks = r["generated"]
    assert len(picks) == 3, picks
    q = db.queue_positions()
    assert [e["track_id"] for e in q] == [t["id"] for t in picks], \
        "generated picks must be queued at the head, in ranked order"


def test_genq_with_ref_and_no_enqueue(registry, tmp_dirs):
    db = registry.db()
    _click(registry, tmp_dirs, "seedA", bpm=120.0)
    other = _click(registry, tmp_dirs, "seedB", bpm=122.0)
    _click(registry, tmp_dirs, "sim", bpm=121.0)
    r = registry.cmd_genq(ref=str(other), enqueue=False)
    assert r["seed"] == other
    assert r["generated"]
    assert db.queue_positions() == []  # --no-enqueue respected


def test_genq_nothing_playing(registry):
    r = registry.cmd_genq()
    assert "nothing playing" in r["error"]


def test_genq_min_score_gate(registry, tmp_dirs):
    """Unanalyzed-only candidates yield an empty generated queue (no junk)."""
    from muse.analysis.synth import click_track

    db = registry.db()
    seed = _click(registry, tmp_dirs, "okseed", bpm=120.0)
    raw = db.upsert_track(title="raw", file_path=str(click_track(
        tmp_dirs / "raw.wav", 120.0)))
    # remove analysis to simulate an unanalyzed library
    db.conn.execute("DELETE FROM analysis WHERE track_id=?", (raw,))
    db.conn.commit()
    registry.cmd_play(track_id=seed)
    r = registry.cmd_genq()
    assert r["generated"] == []
    assert db.queue_positions() == []
    assert db.get_analysis(seed) is not None  # seed itself unaffected


# -- import copies into the managed library dir -----------------------------------
def test_import_copies_to_library_dir(registry, tmp_dirs):
    """cmd_import stores the hash-named copy under paths.library_dir; the
    original file is no longer referenced."""
    import muse.paths as muse_paths
    from muse.analysis.synth import click_track
    src = tmp_dirs / "outside" / "my song.wav"
    src.parent.mkdir(parents=True, exist_ok=True)
    click_track(src, 120.0, bars=16)
    r = registry.cmd_import(str(src))
    assert r["imported"] == 1
    tid = r["track_ids"][0]
    t = registry.db().get_track(tid)
    lib = muse_paths.library_dir()
    assert Path(t["file_path"]).is_relative_to(lib)
    assert Path(t["file_path"]).is_file()
    assert t["file_path"] != str(src)
    # readable copy name: 'my song [<hash>].wav'
    assert "my song [" in Path(t["file_path"]).name
    # idempotent: re-importing the same content hits the same row
    r2 = registry.cmd_import(str(src))
    assert r2["track_ids"] == [tid]
    assert registry.db().stats()["tracks"] == 1


def test_import_reads_genre_tag(registry, tmp_dirs):
    """TCON genre tag survives import into tracks.genre (real mp3 via ffmpeg)."""
    import subprocess

    import muse.analysis.worker as aw
    f = tmp_dirs / "tagged.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.3", "-b:a", "64k",
                    str(f), "-y"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   stdin=subprocess.DEVNULL)
    from mutagen import File as mutagen_file
    m = mutagen_file(str(f), easy=True)
    m["genre"] = ["Alt Rock"]
    m["title"] = ["Some Song"]
    m["artist"] = ["Some Artist"]
    m.save()
    tid = registry.database.upsert_track(
        title="x", file_path=str(f))
    # re-import via the worker: reads + stores genre
    tid = aw.import_file(registry.database, f)
    t = registry.db().get_track(tid)
    assert t["genre"] == "Alt Rock"


# -- analyze --all vs missing-file batch clog ------------------------------------
def test_analyze_pending_prunes_missing_head(registry, tmp_dirs):
    """>50 dead rows once clogged the pending batch forever (analyzed: []).
    analyze_pending must clear them so live pending tracks get reached."""
    from muse.analysis.synth import click_track

    db = registry.db()
    for i in range(60):  # distinct missing paths (upsert dedupes on file_path)
        db.upsert_track(title=f"dead{i}",
                        file_path=str(tmp_dirs / "gone" / f"dead_{i}.wav"))
    live = db.upsert_track(title="live", file_path=str(click_track(
        tmp_dirs / "live.wav", 120.0, bars=16)))
    assert len(db.pending_analysis(tiers=2, limit=50)) == 50  # head is all dead
    done = aw.analyze_pending(db)
    assert done == [live]
    assert db.get_analysis(live) is not None
    assert db.stats()["tracks"] == 1  # 60 dead rows pruned in the same pass


# -- startup integrity prune --------------------------------------------------------
def test_prune_missing_files_removes_gone_rows(registry, tmp_dirs):
    """Startup check deletes rows whose file vanished; queue rows for them
    cascade. Metadata-only rows (no file_path) are kept."""
    from muse.analysis.synth import click_track
    db = registry.db()
    stay = _click(registry, tmp_dirs, "stays", bpm=120.0)
    doomed_path = tmp_dirs / "doomed.wav"
    click_track(doomed_path, 110.0, bars=4)
    doomed = db.upsert_track(title="doomed", file_path=str(doomed_path))
    db.add_to_queue([doomed, stay])
    meta_only = db.upsert_track(title="yt-only", provider="youtube",
                                url="https://www.youtube.com/watch?v=xyz",
                                file_path=None)
    doomed_path.unlink()
    gone = db.prune_missing_files()
    assert gone == [doomed]
    assert db.get_track(doomed) is None
    assert db.get_track(stay) is not None
    assert db.get_track(meta_only) is not None
    # queue FK cascade removed only the doomed entry (the survivor remains)
    remaining = {q["track_id"] for q in db.queue_positions()}
    assert remaining == {stay}
    assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_daemon_main_runs_prune_on_start(tmp_dirs, monkeypatch):
    """main() invokes prune_missing_files during startup (integrity check)."""
    import muse.daemon.main as dm
    called = {}
    monkeypatch.setattr("muse.db.Database.prune_missing_files",
                        lambda self: called.setdefault("ran", True) or [])
    # stub the rest of startup so main() returns after wiring
    monkeypatch.setattr(dm.ipc, "daemon_running", lambda *a, **k: False)
    monkeypatch.setattr(dm.ipc, "start_ipc", lambda *a, **k: None)
    monkeypatch.setattr(dm, "MediaKeys",
                        lambda **k: type("M", (), {"stop": lambda self: None})())
    monkeypatch.setattr("muse.daemon.commands.MuseCommands.audio_engine",
                        lambda self: None)
    monkeypatch.setattr(dm.ipc, "socket_path", lambda: "/tmp/muse-prune.sock")
    # stop the main loop on the first tick: daemon_stop via exit event
    import threading

    from muse.daemon import commands as commands_mod
    monkeypatch.setattr(commands_mod, "_EXIT_REQUESTED",
                        threading.Event().set() or threading.Event())
    # make the loop bail immediately after one iteration
    monkeypatch.setattr(dm.time, "sleep",
                        lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
    dm.main()
    assert called.get("ran") is True