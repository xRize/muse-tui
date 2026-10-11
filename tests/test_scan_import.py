"""import --analyze / muse scan: auto-analysis, batch draining, scan roots."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from muse.analysis.synth import click_track
from muse.analysis.worker import analyze_pending, run_analysis


@pytest.fixture()
def reg(tmp_dirs, monkeypatch):
    import muse.db as dbmod
    from muse.daemon import commands as C
    monkeypatch.setattr(C.audio.Engine, "start", lambda self: None)
    r = C.MuseCommands()
    r.database = dbmod.default_db()
    return r


def test_import_analyze_flag(reg, tmp_dirs, monkeypatch):
    """import --analyze returns analyzed + per-track BPM/key details; re-import
    of the same file is idempotent and does NOT re-analyze."""
    path = click_track(tmp_dirs / "song.wav", 120.0, bars=8)

    r = reg.cmd_import(str(path), analyze=True)
    assert r["imported"] == 1, r
    assert r["new"] == 1, r
    assert r["analyzed"] == 1, r
    assert r["failed"] == 0
    assert len(r["details"]) == 1
    assert r["details"][0]["bpm"], r
    # the track really has analysis
    a = reg.database.get_analysis(r["track_ids"][0])
    assert a and a.get("tiers_done", 0) >= 2

    # re-import same content: same row, no new analysis work
    r2 = reg.cmd_import(str(path), analyze=True)
    assert r2["imported"] == 1
    assert r2["new"] == 0, r2
    assert r2["track_ids"] == r["track_ids"]
    assert r2["analyzed"] == 0, r2
    assert r2["details"] == []


def test_import_plain_has_no_analysis_keys(reg, tmp_dirs):
    path = click_track(tmp_dirs / "plain.wav", 100.0, bars=4)
    r = reg.cmd_import(str(path))
    assert r["imported"] == 1
    assert "analyzed" not in r and "details" not in r


def test_import_failed_flag_when_undecodable(reg, tmp_dirs):
    """A garbage .wav imports (row saved) but analysis fails -> failed=1."""
    bad = tmp_dirs / "corrupt.wav"
    bad.write_bytes(b"definitely-not-audio")
    r = reg.cmd_import(str(bad), analyze=True)
    assert r["imported"] == 1
    assert r["analyzed"] == 0
    assert r["failed"] == 1, r


def test_analyze_pending_drains_batches(reg, tmp_dirs):
    """analyze_pending iterates until pending is exhausted (bypassing the old
    single-50-batch behavior), and honors limit."""
    ids = []
    for i, bpm in enumerate((92.0, 104.0, 116.0, 128.4)):
        p = click_track(tmp_dirs / f"batch_{i}.wav", bpm, bars=4)
        tid = reg.database.upsert_track(title=f"b{i}", file_path=str(p))
        ids.append(tid)
    done = analyze_pending(reg.database, batch=2)
    assert set(ids) <= set(done), (ids, done)

    # limit caps the work
    for i, bpm in enumerate((96.0, 108.0)):
        p = click_track(tmp_dirs / f"cap{i}.wav", bpm, bars=4)
        reg.database.upsert_track(title=f"c{i}", file_path=str(p))
    capped = analyze_pending(reg.database, limit=1)
    assert len(capped) == 1


def test_analyze_pending_skips_permanent_failures(reg, tmp_dirs):
    """Corrupt audio must not doom the walk: good tracks complete, the walk
    terminates, and a second pass finds nothing new (no infinite loop)."""
    from muse.analysis.worker import analyze_pending

    good = click_track(tmp_dirs / "ok.wav", 120.0, bars=8)
    bad = tmp_dirs / "nope.wav"
    bad.write_bytes(b"junk")
    g = reg.database.upsert_track(title="ok", file_path=str(good))
    b = reg.database.upsert_track(title="nope", file_path=str(bad))

    done = analyze_pending(reg.database)
    assert g in done and b not in done
    again = analyze_pending(reg.database)
    assert g not in again  # already complete; must terminate, not spin


def test_tilde_in_filename_survives_prune_and_analyze(reg, tmp_dirs):
    """Filenames containing literal '~' must not be mangled or auto-pruned.

    Regression: db.prune_missing_files and run_analysis did a global
    "~" -> home replace, which mangled paths like 'Pearl Jam ~~ Black ♥.mp3'
    so prune DELETED such tracks and analysis could never find the file
    (scan would re-import the same file forever)."""
    d = tmp_dirs / "tildy"
    d.mkdir()
    p = click_track(d / "song ~~ spaced ♥.wav", 120.0, bars=4)
    assert Path(p).exists()
    tid = reg.database.upsert_track(title="tildy", file_path=str(p))

    assert reg.database.prune_missing_files() == []  # kept, not deleted
    assert run_analysis(reg.database, tid)            # file reachable
    a = reg.database.get_analysis(tid)
    assert a and a.get("tiers_done", 0) >= 2
    # and it stays in the library after a second prune (analyze_pending's)
    assert reg.database.prune_missing_files() == []


def test_scan_path_imports_and_analyzes(reg, tmp_dirs):
    d = tmp_dirs / "incoming"
    d.mkdir()
    click_track(d / "a.wav", 120.0, bars=4)
    click_track(d / "b.wav", 96.0, bars=4)
    r = reg.cmd_scan(str(d))
    assert r["pruned"] == 0, r
    assert r["imported"] == 2
    assert r["new"] == 2
    assert r["analyzed"] == 2
    assert len(r["details"]) == 2
    # idempotent second scan: everything already known, nothing re-analyzed
    r2 = reg.cmd_scan(str(d))
    assert r2["imported"] == 2
    assert r2["new"] == 0
    assert r2["analyzed"] == 0


def test_scan_default_roots_from_config(reg, tmp_dirs):
    """Without a path: roots = download dir + [library] scan_paths (managed
    library dir itself is skipped)."""
    (tmp_dirs / "downloads").mkdir(exist_ok=True)
    cfg = tmp_dirs / "config"
    cfg.mkdir(exist_ok=True)
    (cfg / "config.toml").write_text(
        '[library]\nscan_paths = ["%s"]\n' % (tmp_dirs / "stash").as_posix())
    stash = tmp_dirs / "stash"
    stash.mkdir(exist_ok=True)
    click_track(stash / "stashed.wav", 120.0, bars=4)

    r = reg.cmd_scan(None)
    assert r["imported"] >= 1, r
    assert r["new"] == 1, r
    assert str(stash.resolve()) in " ".join(r["roots"]), r
    assert r["analyzed"] == 1
    lib_str = str((tmp_dirs / "data" / "library").resolve())
    assert lib_str not in " ".join(r["roots"])

    r2 = reg.cmd_scan(None)
    assert r2["new"] == 0 and r2["analyzed"] == 0


def test_scan_prunes_missing_files(reg, tmp_dirs):
    """A track whose file vanished is deleted by the scan's integrity pass."""
    bogus = tmp_dirs / "ghost.mp3"  # never created
    tid = reg.database.upsert_track(title="ghost", file_path=str(bogus))
    assert reg.database.get_track(tid) is not None
    r = reg.cmd_scan(None)
    assert r["pruned"] == 1, r
    assert reg.database.get_track(tid) is None


def test_cli_scan_human_output(tmp_dirs, monkeypatch, capsys):
    """Direct-mode CLI: `muse scan <dir>` prints the summary + BPM/key rows."""
    from muse.daemon import ipc
    monkeypatch.setattr(ipc, "socket_path", lambda: "/tmp/muse-not-running.sock")
    d = tmp_dirs / "cli-in"
    d.mkdir()
    p = click_track(d / "cli-song.wav", 120.0, bars=4)
    assert p is not None

    from muse import cli
    rc = cli.main(["scan", str(d)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "pruned 0 dead row(s)" in out
    assert "processed 1 file(s), 1 new" in out
    assert "analyzed 1" in out
    assert "BPM" in out

    # --json emits the payload directly
    import json as _json

    rc = cli.main(["--json", "scan", str(d)])
    assert rc == 0
    data = _json.loads(capsys.readouterr().out)
    assert data["ok"] and data["new"] == 0 and "roots" not in data


def test_cli_import_analyze_flag(tmp_dirs, monkeypatch, capsys):
    from muse.daemon import ipc
    monkeypatch.setattr(ipc, "socket_path", lambda: "/tmp/muse-not-running.sock")
    p = click_track(tmp_dirs / "cli-imp.wav", 120.0, bars=4)

    from muse import cli
    rc = cli.main(["import", str(p), "--analyze"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "imported 1 track(s)" in out
    assert "BPM" in out or "key" in out