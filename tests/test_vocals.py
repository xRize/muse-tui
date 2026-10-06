"""Tier-2 vocal detection tests (spec §3) — synthetic surrogates, deterministic."""
from __future__ import annotations

import numpy as np

import muse.analysis.synth as synth
import muse.analysis.vocals as vocals

SR = vocals.SR


def _harmonic_vocal(dur=12.0, start=2.0, end=8.0, f0=240.0, seed=7):
    """Surrogate vocal: harmonic stack with 5 Hz vibrato over [start, end)."""
    t = np.arange(int(dur * SR)) / SR
    x = np.zeros_like(t)
    m = (t >= start) & (t < end)
    vib = 1 + 0.02 * np.sin(2 * np.pi * 5 * t[m])
    for k, amp in [(1, 1.0), (2, 0.6), (3, 0.4), (4, 0.25), (5, 0.15), (6, 0.1)]:
        x[m] += amp * np.sin(2 * np.pi * f0 * k * vib * t[m])
    x += 0.01 * np.random.default_rng(seed).standard_normal(len(t))
    return x.astype(np.float32)


def _pad(dur=12.0, start=2.0, end=8.0):
    t = np.arange(int(dur * SR)) / SR
    x = np.zeros_like(t)
    m = (t >= start) & (t < end)
    for f, amp in [(220, 1.0), (277, 0.7), (330, 0.7)]:
        x[m] += amp * np.sin(2 * np.pi * f * t[m])
    return x.astype(np.float32)


def _wav(path, bpm, key_pc, seconds):
    synth.hybrid_track(path, bpm=bpm, key_pitch_class=key_pc, seconds=seconds)
    import wave
    with wave.open(str(path)) as w:
        return (np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
                .astype(np.float32) / 32767.0)


def _read(path):
    import wave
    with wave.open(str(path)) as w:
        return (np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
                .astype(np.float32) / 32767.0)


def test_vocal_surrogate_detected(tmp_path):
    regions = vocals.vocal_regions(_harmonic_vocal(), SR)
    assert len(regions) == 1
    r = regions[0]
    # surrogate sings 2-8s; detector resolves at ~1s segment resolution
    assert 1.0 <= r["start"] <= 3.0, regions
    assert 7.0 <= r["end"] <= 9.0, regions
    # helper accessors agree
    assert vocals.vocal_intro_sec(regions) == r["start"]
    assert vocals.vocal_end_sec(regions) == r["end"]


def test_vocal_two_regions(tmp_path):
    x = _harmonic_vocal(start=1.0, end=3.5) + _harmonic_vocal(start=6.0, end=10.0)
    regions = vocals.vocal_regions(x, SR)
    assert len(regions) == 2, regions
    assert regions[1]["start"] >= 5.0  # second region after the gap


def test_pad_not_vocal():
    assert vocals.vocal_regions(_pad(), SR) == []


def test_click_track_not_vocal(tmp_path):
    assert vocals.vocal_regions(_read(synth.click_track(
        str(tmp_path / "c.wav"), bpm=120)), SR) == []


def test_hybrid_clicks_over_pad_not_vocal(tmp_path):
    x = _wav(str(tmp_path / "h.wav"), bpm=120, key_pc=2, seconds=16.0)
    assert vocals.vocal_regions(x, SR) == []


def test_silence_and_noise_not_vocal():
    assert vocals.vocal_regions(np.zeros(int(12 * SR), np.float32), SR) == []
    rng = np.random.default_rng(3)
    noise = (rng.standard_normal(int(12 * SR)) * 0.1).astype(np.float32)
    assert vocals.vocal_regions(noise, SR) == []


def test_short_audio_returns_empty():
    assert vocals.vocal_regions(np.zeros(int(0.4 * SR), np.float32), SR) == []


def test_intro_end_helpers_empty():
    assert vocals.vocal_intro_sec([]) == 0.0
    assert vocals.vocal_end_sec([]) == 0.0


def test_threshold_sensitivity_direct():
    # a weak (quiet) surrogate should be found when threshold is lowered
    v = _harmonic_vocal()
    weak = (v * 0.3).astype(np.float32)
    regions = vocals.vocal_regions(weak, SR, threshold=0.12)
    assert len(regions) == 1, regions
    # band ratios are scale-invariant, so a *pure* loud vocal is found even
    # at a high threshold — but a threshold above the raw score cap is not
    assert vocals.vocal_regions(_harmonic_vocal(), SR, threshold=5.0) == []