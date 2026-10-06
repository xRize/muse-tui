"""Feature extraction: known-BPM and known-key synthetic signals."""
import numpy as np
import pytest

from muse.analysis import features as F
from muse.analysis.synth import (
    chord_pad_track,
    click_track,
    silence_track,
)


@pytest.mark.parametrize("bpm", [(80.0), (100.0), (120.0), (128.0), (140.0)])
def test_bpm_accuracy_click(tmp_path, bpm):
    path = click_track(tmp_path / f"c{bpm:g}.wav", bpm, bars=16)
    audio = F.decode(path) if hasattr(F, "decode") else None
    from muse.analysis.decoder import decode
    audio = decode(path)
    detected = F.detect_bpm(audio)
    err = abs(detected - bpm) / bpm
    # also accept half/double (octave confusion within 6%)
    err2 = abs(detected * 2 - bpm) / bpm if detected < bpm else abs(detected - bpm * 2) / bpm
    assert min(err, err2) < 0.06, f"{bpm} -> {detected}"


@pytest.mark.parametrize("pc", [0, 2, 4, 5, 7, 9, 11])
def test_key_accuracy_pad(tmp_path, pc):
    path = chord_pad_track(tmp_path / f"k{pc}.wav", pc, bpm=100.0, seconds=14.0)
    from muse.analysis.decoder import decode
    audio = decode(path)
    key, score = F.detect_key(audio)
    from muse.analysis.features import parse_key
    got_pc, got_major = parse_key(key)
    assert got_pc == pc, f"pc {pc}: got {key}"
    assert not got_major, f"expected minor-mode tonic for pc {pc}: got {key}"


def test_key_camelot_tables(tmp_path):
    # derived consistency: relative major of a minor key shares camelot number
    for pc, n_min in F.CAMELOT_MINOR.items():
        rel_maj_pc = (pc + 3) % 12
        assert F.CAMELOT_MAJOR[rel_maj_pc] == n_min


def test_loudness_reasonable(tmp_path):
    from muse.analysis.decoder import decode
    path = click_track(tmp_path / "l.wav", 120.0, bars=8)
    lufs = F.loudness_lufs(decode(path))
    assert -35 < lufs < 0, lufs


def test_silence_yields_low_loudness(tmp_path):
    from muse.analysis.decoder import decode
    path = silence_track(tmp_path / "s.wav", 3.0)
    lufs = F.loudness_lufs(decode(path))
    assert lufs <= -60


def test_beat_grid_bpm_consistency(tmp_path):
    bpm = 120.0
    path = click_track(tmp_path / "g.wav", bpm, bars=16)
    from muse.analysis.decoder import decode
    audio = decode(path)
    grid = F.beat_grid(audio, F.detect_bpm(audio))
    assert len(grid) >= 30
    dts = np.diff(grid)
    # inter-beat intervals within 2% of nominal
    assert np.allclose(dts, 60.0 / bpm, rtol=0.02)


def test_sections_shape(tmp_path):
    path = click_track(tmp_path / "sec.wav", 100.0, bars=16)
    from muse.analysis.decoder import decode
    audio = decode(path)
    e = F.energy_curve(audio)
    secs = F.sections(e, 100.0)
    total = sum(s["end"] - s["start"] for s in secs)
    # covers signal without major overlaps (allow float slop)
    assert total >= (len(audio) / 22050) * 0.98


def test_boundaries_intro_outro(tmp_path):
    sr = 22050
    from muse.analysis.synth import write_wav
    click_track(tmp_path / "b.wav", 120.0)  # ensure modules loaded
    audio = np.zeros(sr * 6)
    tone = np.sin(2 * np.pi * 440 * np.arange(sr * 2) / sr) * 0.5
    audio[sr * 2 : sr * 4] = tone  # sound from 2s..4s
    write_wav(tmp_path / "b2.wav", audio)
    from muse.analysis.decoder import decode
    intro, outro_start = F.boundaries(decode(tmp_path / "b2.wav"))
    assert 1.5 < intro < 2.5
    assert 3.5 < outro_start < 4.5