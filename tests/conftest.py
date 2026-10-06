import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def reset_shared_registry():
    """The daemon's process-global registry must not leak across tests."""
    from muse.daemon import commands as C
    C._SHARED = None
    yield
    C._SHARED = None


@pytest.fixture()
def tmp_dirs(tmp_path, monkeypatch):
    """Redirect all muse XDG dirs + sqlite into the test's tmp_path."""
    monkeypatch.setenv("MUSE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MUSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MUSE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MUSE_TEST_DB", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("MUSE_DOWNLOAD_DIR", str(tmp_path / "downloads"))
    return tmp_path


@pytest.fixture()
def db(tmp_dirs):
    from muse import db as dbmod
    database = dbmod.default_db()
    yield database
    database.close()


@pytest.fixture()
def sample_tracks(db, tmp_dirs):
    """Generate + import 5 synthetic tracks; returns [(track_id, bpm, key_pc), ...]."""
    from muse.analysis import worker as aw
    from muse.analysis.synth import click_track

    specs = []
    for i, bpm in enumerate([90.0, 100.0, 120.0, 128.0, 140.0]):
        pc = (i * 2) % 12
        path = tmp_dirs / "audio" / f"sample_{i}_{bpm:g}.wav"
        click_track(path, bpm, bars=8)
        specs.append((bpm, pc, path))
    for i, (bpm, pc, path) in enumerate(specs):
        aw.run_analysis(db, db.upsert_track(title=f"Track {i}", file_path=str(path)))
    return specs