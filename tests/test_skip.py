"""N-skip regressions: an explicit `next` promotes the new voice to
`current` immediately (status flips at once — the old track must never
 linger in `status`), the outgoing voice keeps fading as an engine tail,
and parked decoders are closed off the audio callback."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from muse.audio.playback import Voice  # noqa: E402
from muse.daemon import commands as C  # noqa: E402


@pytest.fixture()
def registry(tmp_dirs, monkeypatch):
    """Shared registry (daemon-style) with a stubbed audio device."""
    monkeypatch.setattr(C.audio.Engine, "start", lambda self: None)
    reg = C.enable_shared_registry()
    import muse.db as dbmod
    reg.database = dbmod.default_db()
    reg.engine = C.audio.Engine()
    return reg


def _click(registry, tmp_path, name, bpm=120.0, bars=4):
    from muse.analysis import worker as aw
    from muse.analysis.synth import click_track
    path = click_track(str(tmp_path / f"{name}.wav"), bpm, bars=bars)
    tid = registry.database.upsert_track(title=name, file_path=path)
    aw.run_analysis(registry.database, tid)
    return tid


def test_explicit_next_flips_status_at_once(registry, tmp_dirs):
    """The reported bug: after `next` the OLD track kept showing in status
    until its natural EOF. The new voice must BE `current` right away."""
    a = _click(registry, tmp_dirs, "first")
    b = _click(registry, tmp_dirs, "second", bpm=126.0)
    registry.cmd_play(track_id=a)
    registry.db().add_to_queue([b])
    r = registry.cmd_next()
    assert r.get("playing") == "second", r
    eng = registry.engine
    assert eng.current.track_id == b
    assert eng.upcoming is None                      # no automix parking
    assert [t.track_id for t in eng.tails] == [a]    # old voice fades as tail
    st = registry.cmd_status()
    assert st["title"] == "second" and st["id"] == b
    assert not eng.current.done


def test_next_with_empty_queue_uses_smart_shuffle_or_errors(registry, tmp_dirs):
    a = _click(registry, tmp_dirs, "solo")
    _click(registry, tmp_dirs, "other", bpm=126.0)
    registry.cmd_play(track_id=a)
    r = registry.cmd_next(smart=False)               # no queue, no smart
    assert r == {"state": "queue empty"}, r


def test_double_skip_never_leaks_decoders(registry, tmp_dirs):
    """Two rapid skips: both outgoing voices end as parked tails and their
    ffmpeg decoders are closed (no orphaned processes, no abrupt audible
    takeover from an earlier automix voice)."""
    a = _click(registry, tmp_dirs, "s1")
    b = _click(registry, tmp_dirs, "s2", bpm=126.0)
    c = _click(registry, tmp_dirs, "s3", bpm=110.0)
    registry.cmd_play(track_id=a)
    registry.db().add_to_queue([b, c])
    registry.cmd_next()                              # a -> b
    registry.cmd_next()                              # b -> c mid fade-in
    eng = registry.engine
    assert eng.current.track_id == c
    assert eng.upcoming is None
    tails = list(eng.tails)
    assert [t.track_id for t in tails] == [a, b]
    for t in tails:                                  # emulate fade completion
        t.samples_played = t.fade_start + t.fade_samples
    eng._park_finished_tails()                       # callback-side parking
    assert eng.tails == []
    eng.finish_pending_handoff()                     # service-tick drain
    assert eng.pending_closes == []
    for v in tails:
        assert v.decoder.proc.poll() is not None, "decoder still running"


def test_skip_during_automix_overlap_parks_upcoming(registry, tmp_dirs):
    """Natural automix overlap running (a fading out, b upcoming): a skip
    must take over cleanly — c becomes current immediately, the parked b is
    closed (no leak, no accidental restart), a keeps fading as a tail."""
    a = _click(registry, tmp_dirs, "ama")
    b = _click(registry, tmp_dirs, "amb", bpm=126.0)
    c = _click(registry, tmp_dirs, "amc", bpm=110.0)
    registry.cmd_play(track_id=a)
    db = registry.db()
    db.add_to_queue([b, c])
    eng = registry.engine
    db.pop_queue_head()                              # automix consumed b
    vb = Voice(b, db.get_track(b)["file_path"], db.get_track(b)["duration"])
    eng.current.start_fade(8.0, 0.0)
    eng.begin_crossfade(vb, 8.0)
    assert eng.upcoming is vb
    r = registry.cmd_next()                          # skip NOW -> c
    assert r.get("playing") == "amc", r
    assert eng.current.track_id == c
    assert eng.upcoming is None                      # b parked, not orphaned
    assert [t.track_id for t in eng.tails] == [a]    # a keeps fading out
    assert eng.pending_closes and eng.pending_closes[0].track_id == b
    eng._park_finished_tails()
    eng.finish_pending_handoff()
    assert eng.pending_closes == []
    assert vb.decoder.proc.poll() is not None, "orphaned decoder leaked"


def test_skip_with_unplayable_track_keeps_playing(registry, tmp_dirs, tmp_path):
    """A broken queue entry must not kill the current playback."""
    a = _click(registry, tmp_dirs, "alive")
    registry.cmd_play(track_id=a)
    db = registry.db()
    broken = db.upsert_track(title="nope", file_path=None)
    db.add_to_queue([broken])
    eng = registry.engine
    r = registry.cmd_next()
    assert "error" in r, r
    assert eng.current is not None and eng.current.track_id == a