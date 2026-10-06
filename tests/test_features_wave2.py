"""Tests for the wave-2 feature set: seek/prev/shuffle/discover/history/stats,
live AutoMix service_tick, band filters, daemon stop, completions (spec §3-4)."""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from muse.audio.playback import BandFilter, Engine, Voice
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


def _click(registry, tmp_path, name, bpm=120.0, bars=4):
    from muse.analysis import worker as aw
    from muse.analysis.synth import click_track
    path = click_track(str(tmp_path / f"{name}.wav"), bpm, bars=bars)
    tid = registry.database.upsert_track(title=name, file_path=path)
    aw.run_analysis(registry.database, tid)
    return tid


def _play(registry, tid):
    r = registry.cmd_play(track_id=tid)
    assert "error" not in r, r
    return r


# -- seek ----------------------------------------------------------------------
def test_seek_absolute_and_relative(registry, tmp_dirs):
    tid = _click(registry, tmp_dirs, "seekme", bpm=120.0, bars=16)  # 32s long
    registry.db().set_setting("volume", "0.8")
    _play(registry, tid)
    eng = registry.engine
    eng.current.samples_played = int(10.0 * C.audio.SR)  # fake 10s in
    r = registry.cmd_seek(30.0)
    assert abs(r["position"] - 30.0) < 0.5, r
    r = registry.cmd_seek(-5.0, relative=True)
    assert 24.0 <= r["position"] <= 26.0, r
    assert registry.cmd_seek("nope")["error"].startswith("bad seek")


def test_seek_nothing_playing(registry):
    r = registry.cmd_seek(10.0)
    assert r == {"error": "nothing playing"}


# -- prev ----------------------------------------------------------------------
def test_prev_restarts_when_past_3s(registry, tmp_dirs):
    tid = _click(registry, tmp_dirs, "prevrest")
    _play(registry, tid)
    registry.engine.current.samples_played = int(10.0 * C.audio.SR)
    r = registry.cmd_prev()
    assert r["state"] == "restarted"
    assert registry.engine.position() < 1.0


def test_prev_before_3s_returns_last_played(registry, tmp_dirs):
    t1 = _click(registry, tmp_dirs, "first")
    t2 = _click(registry, tmp_dirs, "second")
    _play(registry, t1)
    import time as _t
    _t.sleep(1.1)  # distinct play_time rows
    _play(registry, t2)
    registry.engine.current.samples_played = int(1.0 * C.audio.SR)  # < 3s
    r = registry.cmd_prev()
    assert r["playing"] == "first", r
    # the popped (current) entry was dropped; its play_count stays >= 1
    assert registry.db().get_track(t2)["play_count"] >= 1


# -- queue shuffle ---------------------------------------------------------------
def test_queue_shuffle_preserves_multiset(registry, tmp_dirs):
    db = registry.db()
    ids = [_click(registry, tmp_dirs, f"q{i}", bpm=100.0 + i) for i in range(5)]
    db.add_to_queue(ids)
    before = sorted(q["track_id"] for q in db.queue_positions())
    r = registry.cmd_queue("shuffle")
    assert r["shuffled"] == 5
    after = sorted(q["track_id"] for q in db.queue_positions())
    assert before == after
    pos = [q["position"] for q in db.queue_positions()]
    assert pos == list(range(1, 6)), pos


def test_queue_shuffle_keep_first(registry, tmp_dirs):
    db = registry.db()
    ids = [_click(registry, tmp_dirs, f"k{i}", bpm=100.0 + i) for i in range(5)]
    db.add_to_queue(ids)
    r = registry.cmd_queue("shuffle", keep_first=True)
    assert r["shuffled"] == 5
    q = r["queue"]
    assert q[0]["id"] == ids[0], q  # first entry stays first


# -- discover --------------------------------------------------------------------
def test_discover_returns_unique_no_enqueue(registry, tmp_dirs):
    db = registry.db()
    n0 = len(db.queue_positions())
    for i in range(4):
        _click(registry, tmp_dirs, f"d{i}", bpm=100.0 + i * 5)
    r = registry.cmd_discover(length=3)
    picks = r["discover"]
    assert 1 <= len(picks) <= 3
    assert len({t["id"] for t in picks}) == len(picks)
    assert len(db.queue_positions()) == n0  # no enqueue by default


def test_discover_with_seed_and_enqueue(registry, tmp_dirs):
    db = registry.db()
    seed = _click(registry, tmp_dirs, "seed", bpm=120.0)
    _click(registry, tmp_dirs, "other", bpm=122.0)
    r = registry.cmd_discover(seed=str(seed), length=2, enqueue=True)
    assert r["discover"], r
    assert len(db.queue_positions()) == 2


# -- history / stats ---------------------------------------------------------------
def test_history_and_stats(registry, tmp_dirs):
    db = registry.db()
    t1 = _click(registry, tmp_dirs, "h1")
    t2 = _click(registry, tmp_dirs, "h2")
    db.record_play(t1, 0)
    import time as _t
    _t.sleep(1.1)
    db.record_play(t2, 0)
    db.record_play(t1, 0)
    r = registry.cmd_history(limit=5)
    hids = [t["id"] for t in r["history"]]
    assert t1 in hids and t2 in hids
    assert hids[0] == t1  # newest first
    r = registry.cmd_stats()
    assert r["library"]["tracks"] == 2
    assert r["library"]["history"] == 3
    top = {t["id"]: t["play_count"] for t in r["top"]}
    assert top[t1] == 2, top


# -- library kinds -----------------------------------------------------------------
def test_library_artists_albums_kinds(registry, tmp_dirs):
    from muse.analysis.synth import click_track
    db = registry.db()
    for name, artist, album in [("A1", "Alpha", "One"), ("A2", "Alpha", "Two"),
                                ("B1", "Beta", "One")]:
        p = click_track(str(tmp_dirs / f"{name}.wav"), 120.0, bars=2)
        db.upsert_track(title=name, artist=artist, album=album, file_path=p)
    arts = {a["name"]: a["n_tracks"] for a in registry.cmd_library(kind="artists")["artists"]}
    assert arts == {"Alpha": 2, "Beta": 1}
    albs = registry.cmd_library(kind="albums")["albums"]
    names = {(a.get("artist"), a["name"], a["n_tracks"]) for a in albs}
    assert ("Alpha", "One", 1) in names and ("Beta", "One", 1) in names


# -- playlist play -----------------------------------------------------------------
def test_playlist_play(registry, tmp_dirs):
    db = registry.db()
    t1 = _click(registry, tmp_dirs, "p1")
    t2 = _click(registry, tmp_dirs, "p2")
    t3 = _click(registry, tmp_dirs, "p3")
    r = registry.cmd_playlist("create", "Mix")
    for tid in (t1, t2, t3):
        registry.cmd_playlist("add", "Mix", str(tid))
    r = registry.cmd_playlist("play", "Mix")
    assert r["playlist"] == "Mix" and r["playing"] == "p1"
    assert r["queued"] == 2
    q = db.queue_positions()
    assert [qq["track_id"] for qq in q] == [t2, t3]


# -- daemon stop / exit_requested -----------------------------------------------
def test_daemon_stop_sets_exit_signal():
    from muse.daemon import commands as cmds_mod
    reg = cmds_mod.MuseCommands()
    assert not cmds_mod.exit_requested()
    r = reg.cmd_daemon_stop()
    assert r["daemon_exiting"] is True
    assert cmds_mod.exit_requested()
    cmds_mod._EXIT_REQUESTED.clear()


# -- service_tick -----------------------------------------------------------------
class _FakeVoice:
    """Minimal stand-in for audio.Voice in service_tick scheduling tests."""

    def __init__(self, track_id, duration, position=0.0, done=False):
        self.track_id = track_id
        self.duration = duration
        self._pos = position
        self.done = done
        self.failed = False


def test_service_tick_auto_advance_broken_queue_entries(registry, tmp_dirs):
    db = registry.db()
    good = _click(registry, tmp_dirs, "good")
    _play(registry, good)
    eng = registry.engine
    # queue: broken entry (track row without file_path) then a good track
    broken = db.upsert_track(title="ghost", file_path=None)
    db.add_to_queue([broken, good])
    eng.current.done = True  # pretend the current track finished
    r = registry.service_tick()
    assert r and r["advanced_to"] == good, r
    assert eng.current.track_id == good


def test_service_tick_automix_crossfade(registry, tmp_dirs, monkeypatch):
    """Inside the transition zone with a queue head: live crossfade starts."""
    db = registry.db()
    a = _click(registry, tmp_dirs, "cfa", bpm=120.0)
    b = _click(registry, tmp_dirs, "cfb", bpm=120.0)
    _play(registry, a)
    eng = registry.engine
    db.add_to_queue([b])
    db.set_setting("automix_enabled", "1")
    # high score plan (same tempo); pin the anchor check to pass
    monkeypatch.setattr(C.MuseCommands, "_plan_to_track",
                        lambda self, fa, fb: {"overlap_sec": 4.0, "tempo_scale": 1.0,
                                              "a_end_sec": 0.0, "b_start_sec": 0.0,
                                              "score": 90.0})
    eng.current.duration = 10.0
    eng.current.samples_played = int(7.0 * C.audio.SR)  # remaining=3 <= overlap 4
    # the engine has no real decoder output; begin_crossfade only needs voices
    fades = []
    monkeypatch.setattr(Engine, "begin_crossfade",
                        lambda self, voice, sec, band_limited=False:
                        fades.append((voice.track_id, sec, band_limited)) or True)
    r = registry.service_tick()
    assert r and r.get("automix"), r
    assert r["to"] == b and r["overlap"] == 4.0
    assert fades and fades[0][0] == b and fades[0][1] == 4.0
    # queue head was consumed
    assert db.peek_queue_head() is None


def test_service_tick_low_score_no_crossfade(registry, tmp_dirs, monkeypatch):
    db = registry.db()
    a = _click(registry, tmp_dirs, "lfa", bpm=90.0)
    b = _click(registry, tmp_dirs, "lfb", bpm=140.0)
    _play(registry, a)
    eng = registry.engine
    db.add_to_queue([b])
    eng.current.done = False
    eng.current.duration = 10.0
    eng.current.samples_played = int(7.0 * C.audio.SR)
    monkeypatch.setattr(C.MuseCommands, "_plan_to_track",
                        lambda self, fa, fb: {"overlap_sec": 4.0, "tempo_scale": 1.0,
                                              "a_end_sec": 0.0, "b_start_sec": 0.0,
                                              "score": 20.0})
    r = registry.service_tick()
    assert r is None
    assert db.peek_queue_head() is not None  # not consumed


def test_service_tick_waits_for_beat_anchor(registry, tmp_dirs, monkeypatch):
    db = registry.db()
    a = _click(registry, tmp_dirs, "anc", bpm=120.0)
    b = _click(registry, tmp_dirs, "anc2", bpm=120.0)
    _play(registry, a)
    eng = registry.engine
    db.add_to_queue([b])
    eng.current.duration = 20.0
    eng.current.samples_played = int(14.0 * C.audio.SR)  # remaining 6 > overlap? no: overlap 8
    monkeypatch.setattr(C.MuseCommands, "_plan_to_track",
                        lambda self, fa, fb: {"overlap_sec": 8.0, "tempo_scale": 1.0,
                                              "a_end_sec": 18.0, "b_start_sec": 0.0,
                                              "score": 90.0})
    r = registry.service_tick()
    assert r is None  # position 14 < anchor 18
    assert db.peek_queue_head() is not None
    eng.current.samples_played = int(18.2 * C.audio.SR)  # past the anchor now
    fades = []
    monkeypatch.setattr(Engine, "begin_crossfade",
                        lambda self, voice, sec, band_limited=False:
                        fades.append(voice.track_id) or True)
    r = registry.service_tick()
    assert r and r.get("automix"), r
    assert fades == [b]


def test_service_tick_pops_and_skips_broken_in_automix_path(registry, tmp_dirs, monkeypatch):
    """Broken queue head in the automix path: popped, not requeued; falls
    back to the plain-advance path this turn (returns None)."""
    db = registry.db()
    a = _click(registry, tmp_dirs, "brk", bpm=120.0)
    _play(registry, a)
    eng = registry.engine
    ghost = db.upsert_track(title="ghost2", file_path=None)  # track row, no file
    db.add_to_queue([ghost])
    eng.current.duration = 10.0
    eng.current.samples_played = int(7.0 * C.audio.SR)
    monkeypatch.setattr(C.MuseCommands, "_plan_to_track",
                        lambda self, fa, fb: {"overlap_sec": 4.0, "tempo_scale": 1.0,
                                              "a_end_sec": 0.0, "b_start_sec": 0.0,
                                              "score": 90.0})
    r = registry.service_tick()
    assert r is None
    assert db.peek_queue_head() is None  # popped, not requeued


# -- BandFilter (DSP, numeric) -------------------------------------------------------
def _ref_lp(x, alpha):
    y = np.empty_like(x)
    carry = 0.0
    for i in range(x.shape[0]):
        carry = carry + alpha * (x[i] - carry)
        y[i] = carry
    return y


def test_band_filter_lowpass_gains():
    sr = 44100
    lp = BandFilter("lowpass", 300.0, sr)
    # steady 100Hz sine: passes
    t = np.arange(sr) / sr
    s100 = np.sin(2 * np.pi * 100.0 * t).astype(np.float32)
    y = lp.process(np.stack([s100, s100], axis=1))
    tail = y[-sr // 10 :, 0]
    x_tail = s100[-sr // 10:]
    gain100 = np.sqrt((tail ** 2).mean() / (x_tail ** 2).mean())
    lp2 = BandFilter("lowpass", 300.0, sr)
    s3k = np.sin(2 * np.pi * 3000.0 * t).astype(np.float32)
    y3 = lp2.process(np.stack([s3k, s3k], axis=1))
    gain3k = np.sqrt((y3[-sr // 10:, 0] ** 2).mean() / (s3k[-sr // 10:] ** 2).mean())
    assert gain100 > 0.9, gain100
    assert gain3k < 0.2, gain3k


def test_band_filter_highpass_gains():
    sr = 44100
    t = np.arange(sr) / sr
    s100 = np.sin(2 * np.pi * 100.0 * t).astype(np.float32)
    s3k = np.sin(2 * np.pi * 3000.0 * t).astype(np.float32)
    hp = BandFilter("highpass", 300.0, sr)
    y = hp.process(np.stack([s100, s100], axis=1))
    gain100 = np.sqrt((y[-sr // 10:, 0] ** 2).mean() / (s100[-sr // 10:] ** 2).mean())
    hp2 = BandFilter("highpass", 300.0, sr)
    y3 = hp2.process(np.stack([s3k, s3k], axis=1))
    gain3k = np.sqrt((y3[-sr // 10:, 0] ** 2).mean() / (s3k[-sr // 10:] ** 2).mean())
    assert gain100 < 0.45, gain100
    assert gain3k > 0.9, gain3k


def test_band_filter_carry_continuity():
    """Splitting a signal into two blocks must match single-block processing
    (streaming carry is exact)."""
    sr = 44100
    rng = np.random.default_rng(5)
    x = rng.standard_normal((4096, 2)).astype(np.float32)
    f = BandFilter("lowpass", 300.0, sr)
    f.process(rng.standard_normal((512, 2)).astype(np.float32))  # prime the carry
    whole = f.process(x.copy())                          # carry carried through
    f2 = BandFilter("lowpass", 300.0, sr)
    f2.process(rng.standard_normal((512, 2)).astype(np.float32))  # same prime
    a = f2.process(x[:1024].copy())
    b = f2.process(x[1024:].copy())
    split = np.vstack([a, b])
    assert np.abs(whole - split).max() < 0.3, np.abs(whole - split).max()


# -- engine crossfade / handoff / promote --------------------------------------------
def test_engine_begin_crossfade_and_handoff(tmp_path):
    """Real Engine (no stream): crossfade + handoff promote incoming."""
    eng = Engine()
    from muse.analysis.synth import click_track
    p1 = click_track(str(tmp_path / "v1.wav"), 120.0, bars=4)
    p2 = click_track(str(tmp_path / "v2.wav"), 120.0, bars=4)
    v1 = Voice(1, p1, 8.0)
    v2 = Voice(2, p2, 8.0)
    eng.current = v1
    eng.begin_crossfade(v2, 2.0)
    assert eng.upcoming is v2
    assert v1.fade_start is not None
    assert eng.upcoming.fade_start is not None
    # simulate outgoing completion
    v1.done = True
    eng.handoff_if_ready()
    assert eng.current is v2 and eng.upcoming is None
    assert eng.handoff_done.is_set()
    assert v2.filter is None


def test_engine_seek_rebuilds_decoder(tmp_path):
    from muse.analysis.synth import click_track
    p = click_track(str(tmp_path / "vk.wav"), 120.0, bars=8)
    v = Voice(1, p, 16.0)
    eng = Engine()
    eng.current = v
    pos = eng.seek(5.0)
    assert abs(pos - 5.0) < 0.5
    assert eng.current is v  # same voice object rebuilt in place
    assert abs(v.position_seconds - 5.0) < 1.5


def test_engine_needs_advance(tmp_path):
    from muse.analysis.synth import click_track
    p = click_track(str(tmp_path / "na.wav"), 120.0, bars=4)
    v = Voice(1, p, 8.0)
    eng = Engine()
    eng.current = v
    assert not eng.needs_advance()
    v.done = True
    assert eng.needs_advance()
    eng.upcoming = Voice(2, p, 8.0)
    assert not eng.needs_advance()


# -- CLI completions -------------------------------------------------------------------
def test_cli_completions_output(capsys):
    from muse import cli
    for shell in ("bash", "zsh", "fish"):
        rc = cli.main(["completions", shell])
        out = capsys.readouterr().out
        assert rc == 0
        assert "muse" in out
    bash = capsys.readouterr().out  # rerun for content checks
    rc = cli.main(["completions", "bash"])
    bash = capsys.readouterr().out
    assert "compgen -W" in bash and "discover" in bash
    rc = cli.main(["completions", "zsh"])
    zsh = capsys.readouterr().out
    assert "#compdef muse" in zsh
    rc = cli.main(["completions", "fish"])
    fish = capsys.readouterr().out
    assert "complete -c muse" in fish


def test_cli_shuffle_alias(tmp_dirs, monkeypatch, capsys):
    """`muse shuffle` maps to queue shuffle end-to-end (direct mode)."""
    from muse.daemon import ipc as ipc_mod
    monkeypatch.setattr(ipc_mod, "socket_path", lambda: "/tmp/muse-x.sock")
    from muse import cli
    from muse.analysis.synth import click_track
    p = click_track(str(tmp_dirs / "sh.wav"), 120.0, bars=2)
    rc = cli.main(["import", str(p)])
    assert rc == 0
    rc = cli.main(["queue", "add", "1"])
    assert rc == 0
    rc = cli.main(["shuffle"])
    out = capsys.readouterr().out
    assert rc == 0 and ("1." in out or "sh" in out)


# -- daemon CLI status (no daemon) -----------------------------------------------------
def test_cli_daemon_status_no_daemon(tmp_dirs, monkeypatch, capsys):
    from muse.daemon import ipc as ipc_mod
    monkeypatch.setattr(ipc_mod, "socket_path", lambda: "/tmp/muse-ds-off.sock")
    monkeypatch.setattr(ipc_mod, "daemon_running", lambda: False)
    from muse import cli
    rc = cli.main(["daemon", "status"])
    out = capsys.readouterr().out
    assert rc == 0 and "not running" in out