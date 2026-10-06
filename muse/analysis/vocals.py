"""Tier-2 vocal/instrumental detection — band-energy heuristics, pure numpy.

Spec §3 Tier 2: "use a simple ML model or band-energy ratio to mark segments
dominated by vocals". This is the band-ratio variant. Audio is collapsed to
~1 s *low-quantile* spectra (25th percentile per bin over each segment's STFT
frames): sustained material — singing — appears at full level, while
percussive transients (clicks, drum hits, which occupy a minority of frames)
fall below the quantile and vanish. Each segment then scores three factors,
all in [0, 1]:

  ratio  — midband (300–3400 Hz) share of mid+treble energy, re-referenced so
           broadband noise (uniform bin energy) scores 0. Energy below 300 Hz
           (bass, kick) is excluded from the comparison so bass-heavy mixes
           don't mask singing.
  spread — fraction of midband bins actually active (singing spreads across
           the formant band; one tone is a single ridge)
  sing   — count of prominent midband partials: a harmonic comb has ≥5;
           a triad pad or a clicked tick has ≤3

score = ratio x spread x sing per segment; regions are thresholded runs.
Drives vocal-aware AutoMix overlap planning. Heuristic, not ML — documented.
"""
from __future__ import annotations

import numpy as np

from muse.analysis.features import SR

_FRAME = 4096
_HOP = 1024
MID_LO = 300.0
MID_HI = 3400.0
# median-spectrum segment length in frames (~1 s): short enough for
# AutoMix-planning resolution, long enough that sub-second transients
# (clicks, drum hits) fall below the per-bin quantile
_SEG_FRAMES = 21
# partial-count band for the "singing" bonus: fewer than _PEAKS_MIN midband
# partials (a triad pad, a percussive tick) can't be vocals; _PEAKS_FULL or
# more (harmonic comb) is fully vocal-like.
_PEAKS_MIN = 4.0
_PEAKS_FULL = 6.0


def _peaks_per_row(mag: np.ndarray, med: np.ndarray, floor: np.ndarray) -> np.ndarray:
    """Count of prominent local maxima per row (row = one segment spectrum)."""
    prev = np.roll(mag, 1, axis=1)
    nxt = np.roll(mag, -1, axis=1)
    lm = (mag >= prev) & (mag >= nxt)
    lm[:, 0] = mag[:, 0] >= mag[:, 1]
    lm[:, -1] = mag[:, -1] >= mag[:, -2]
    return (lm & (mag > 2.0 * med) & (mag > floor)).sum(axis=1)


def _frame_mags(audio: np.ndarray, sr: int = SR) -> np.ndarray:
    if len(audio) < _FRAME:
        audio = np.pad(audio, (0, _FRAME - len(audio)))
    n_frames = 1 + max(0, (len(audio) - _FRAME)) // _HOP
    idx = np.arange(_FRAME)[None, :] + _HOP * np.arange(n_frames)[:, None]
    return np.abs(np.fft.rfft(audio[idx] * np.hanning(_FRAME)))


def vocal_regions(audio: np.ndarray, sr: int = SR, threshold: float = 0.25,
                  min_sec: float = 0.5, merge_sec: float = 0.25) -> list[dict]:
    """Vocal segments as [{'start': s, 'end': e}, ...] in seconds."""
    mag = _frame_mags(audio, sr)
    if mag.shape[0] < _SEG_FRAMES:
        return []
    # collapse to ~1 s low-quantile spectra: sustained content survives in
    # full, transients (which light a minority of frames) drop out
    n_seg = mag.shape[0] // _SEG_FRAMES
    seg = np.quantile(
        mag[: n_seg * _SEG_FRAMES].reshape(n_seg, _SEG_FRAMES, -1), 0.25, axis=1)
    if not np.any(seg > 0):
        return []
    freqs = np.fft.rfftfreq(_FRAME, 1.0 / sr)
    mid = (freqs >= MID_LO) & (freqs <= MID_HI)
    hi = (freqs > MID_HI) & (freqs <= min(MID_HI * 2.5, sr / 2.0))
    if not mid.any() or not hi.any():
        return []
    mid_mag = seg[:, mid]
    hi_sum = seg[:, hi].sum(axis=1)
    mid_sum = mid_mag.sum(axis=1)
    # vocal formant share of mid+treble energy; broadband noise has a
    # baseline equal to the band-width share — remove it before thresholding
    ratio = mid_sum / (mid_sum + hi_sum + 1e-12)
    lo_bins = MID_HI - MID_LO
    hi_bins = min(MID_HI * 2.5, sr / 2.0) - MID_HI
    base = lo_bins / (lo_bins + hi_bins)
    norm = (ratio - base) / (1.0 - base)
    seg_max = seg.max(axis=1)[:, None] + 1e-12
    n_active = (mid_mag > 0.05 * seg_max).sum(axis=1)
    spread = np.minimum(1.0, n_active / 12.0)
    med = np.median(mid_mag[:, mid_mag.sum(axis=0) > 0], axis=1)[:, None] + 1e-12
    n_peaks = _peaks_per_row(
        mid_mag, med, floor=0.05 * seg_max.repeat(mid_mag.shape[1], axis=1))
    sing = np.clip((n_peaks - _PEAKS_MIN) / (_PEAKS_FULL - _PEAKS_MIN), 0.0, 1.0)
    score = norm * spread * sing
    active = score > threshold
    sec_per_seg = _SEG_FRAMES * _HOP / sr
    runs: list[list[int]] = []
    start = None
    for i, a in enumerate(active):
        if a and start is None:
            start = i
        elif not a and start is not None:
            runs.append([start, i])
            start = None
    if start is not None:
        runs.append([start, len(active)])
    merged: list[list[int]] = []
    for r in runs:
        if merged and (r[0] - merged[-1][1]) * sec_per_seg < merge_sec:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [
        {"start": round(a * sec_per_seg, 2), "end": round(b * sec_per_seg, 2)}
        for a, b in merged
        if (b - a) * sec_per_seg >= min_sec
    ]


def vocal_intro_sec(regions: list[dict]) -> float:
    return float(regions[0]["start"]) if regions else 0.0


def vocal_end_sec(regions: list[dict]) -> float:
    return float(regions[-1]["end"]) if regions else 0.0