"""Synthetic audio generation for deterministic analysis tests and offline demo.

A click track with clicks at exact beat intervals yields a known BPM; a chord
pad built from a known scale yields a known musical key. Pure numpy — no
network, no librosa, fully deterministic.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 22050


def write_wav(path: str | Path, samples: np.ndarray, sr: int = SR) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(samples, -1.0, 1.0)
    data = (pcm * 32767.0).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data)
    return str(path)


def _bandlimited_click(sr: int, length: int, freq: float = 1000.0) -> np.ndarray:
    """A short percussive tick: gated sinusoid with fast decay."""
    t = np.arange(length) / sr
    env = np.exp(-t * 90.0)
    return np.sin(2 * np.pi * freq * t) * env


def click_track(path: str | Path, bpm: float, bars: int = 16, beats_per_bar: int = 4,
                sr: int = SR) -> str:
    """Stereo click track with attacks exactly on the beat grid."""
    spb = 60.0 / bpm
    n_beats = bars * beats_per_bar
    total = int((n_beats * spb + 0.5) * sr)
    audio = np.zeros(total)
    click = _bandlimited_click(sr, int(0.08 * sr))
    for b in range(n_beats):
        start = int(round(b * spb * sr))
        end = min(start + len(click), total)
        audio[start:end] += click[: end - start]
        # stronger downbeat accent
        if b % beats_per_bar == 0 and start < total:
            audio[start : min(start + sr // 50, total)] *= 1.5
    peak = np.max(np.abs(audio)) or 1.0
    return write_wav(path, audio / peak * 0.9, sr)


def _freq_of(pitch_class: int, octave: int) -> float:
    # A4 (pc 9, octave 4) = 440
    semis = (octave - 4) * 12 + (pitch_class - 9)
    return 440.0 * (2 ** (semis / 12.0))


# natural minor scale degrees (semitones from tonic): used with chord I, iv, v, VII
_MIN_STEPS = [0, 2, 3, 5, 7, 8, 10]


def _minor_triad(key_pc: int, degree: int) -> list[float]:
    """Diatonic triad on a natural-minor degree. Degree is in semitones from the
    tonic: 0 (i, minor), 5 (iv, major), 7 (v, minor), 10 (VII, major)."""
    third = 3 if degree in (0, 7) else 4   # i/v minor, iv/VII major
    offsets = [degree, degree + third, degree + 7]
    return [_freq_of((key_pc + off) % 12, 3 + (key_pc + off) // 12) for off in offsets]


def chord_pad_track(path: str | Path, key_pitch_class: int, bpm: float = 100.0,
                    seconds: float = 12.0, sr: int = SR) -> str:
    """Harmonic pad: i - iv - v - VII progression in the target minor key.

    Deterministic key content for Krumhansl-Schmuckler tests (uses the full
    natural-minor scale, including the distinctive lowered 6th/7th degrees).
    """
    total = int(seconds * sr)
    audio = np.zeros(total)
    t = 0.0
    bar = 0
    while True:
        degree = [0, 5, 7, 10][bar % 4]  # i, iv, v, VII
        freqs = _minor_triad(key_pitch_class, degree)
        dur = 60.0 / bpm * 4  # one bar
        n = int(dur * sr)
        start = int(t * sr)
        if start >= total:
            break
        seg_t = np.arange(min(n, total - start)) / sr
        seg = sum(np.sin(2 * np.pi * f * seg_t) for f in freqs) / 3.0
        # gentle attack/release per bar to avoid clicks
        fade = min(0.02, len(seg_t) / sr / 2)
        nf = int(fade * sr)
        if nf:
            seg[:nf] *= np.linspace(0, 1, nf)
            seg[-nf:] *= np.linspace(1, 0, nf)
        audio[start : start + len(seg)] += seg
        t += dur
        bar += 1
    peak = np.max(np.abs(audio)) or 1.0
    return write_wav(path, audio / peak * 0.9, sr)


def hybrid_track(path: str | Path, bpm: float, key_pitch_class: int, seconds: float = 20.0,
                 sr: int = SR) -> str:
    """Clicks (beat) over a chord pad (key) — gives both BPM and key signals."""
    spb = 60.0 / bpm
    n_beats = int(spb and seconds / spb)
    click_audio = np.zeros(int(seconds * sr))
    click = _bandlimited_click(sr, int(0.08 * sr), freq=2000.0)
    for b in range(n_beats):
        start = int(round(b * spb * sr))
        end = min(start + len(click), len(click_audio))
        click_audio[start:end] += 0.6 * click[: end - start]
    kpath = str(path) + ".pad.wav"
    pad_str = chord_pad_track(kpath, key_pitch_class, bpm=bpm, seconds=seconds, sr=sr)
    import wave as _w

    with _w.open(pad_str, "rb") as w:
        pad = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float) / 32767.0
    n = min(len(click_audio), len(pad))
    mix = click_audio[:n] * 0.7 + pad[:n] * 0.6
    peak = np.max(np.abs(mix)) or 1.0
    out = write_wav(path, mix / peak * 0.9, sr)
    Path(kpath).unlink(missing_ok=True)
    return out


def silence_track(path: str | Path, seconds: float = 4.0, sr: int = SR) -> str:
    return write_wav(path, np.zeros(int(seconds * sr)), sr)