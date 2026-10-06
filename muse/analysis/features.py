"""Tier-1 and Tier-2 audio feature extraction — pure numpy DSP.

BPM: onset-strength envelope + autocorrelation periodicity (60–190 BPM range,
with half/double tempo resolution). Key: chroma + Krumhansl-Schmuckler profile
correlation. Loudness: EBU R128-style gated integrated measurement. Energy:
frame RMS curve. Tier-2: beat grid (phase-locked clicks), intro/outro
boundaries, coarse structure segmentation.
"""
from __future__ import annotations

import numpy as np

SR = 22050
HOP = 512
FRAME = 1024

# Krumhansl-Schmuckler key profiles (major, minor)
_KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.69, 3.98, 2.19, 3.48, 4.69, 3.98, 2.19])
_KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
_PITCHES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

CAMELOT_MAJOR = {0: 8, 1: 1, 2: 6, 3: 11, 4: 4, 5: 9, 6: 2, 7: 7, 8: 12, 9: 5, 10: 10, 11: 3}
CAMELOT_MINOR = {0: 11, 1: 4, 2: 9, 3: 2, 4: 7, 5: 12, 6: 5, 7: 10, 8: 3, 9: 8, 10: 1, 11: 6}


def _frame_signal(audio: np.ndarray, sr: int = SR, hop: int = HOP, n: int = FRAME):
    n_frames = 1 + max(0, (len(audio) - n)) // hop
    idx = np.arange(n)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = audio[idx] * np.hanning(n)
    return frames


def onset_envelope(audio: np.ndarray, sr: int = SR) -> np.ndarray:
    """Spectral-flux onset strength envelope (per hop)."""
    frames = _frame_signal(audio, sr)
    mag = np.abs(np.fft.rfft(frames))
    flux = np.maximum(mag[1:] - mag[:-1], 0).sum(axis=1)
    flux = np.concatenate([[flux[0] if len(flux) else 0.0], flux])
    # subtract local mean (accentuates peaks)
    if len(flux) > 8:
        kernel = np.ones(8) / 8
        local = np.convolve(flux, kernel, mode="same")
        flux = np.maximum(flux - local, 0)
    return flux


def detect_bpm(audio: np.ndarray, sr: int = SR) -> float:
    """Tempo via autocorrelation of the onset envelope."""
    env = onset_envelope(audio, sr)
    if len(env) < int(0.8 * HOP):  # < ~2.3s of audio: no meaningful tempo
        return 0.0
    fps = sr / HOP
    # 60–190 BPM -> lag range
    min_lag = max(1, int(fps * 60.0 / 190))
    max_lag = min(len(env) - 1, int(fps * 60.0 / 60))
    if max_lag <= min_lag:
        return 0.0
    e = env - env.mean()
    denom = np.sqrt((e * e).sum()) or 1.0
    ac = np.empty(max_lag + 1)
    for lag in range(1, max_lag + 1):
        num = (e[:-lag] * e[lag:]).sum()
        ac[lag] = num / denom
    # log-normal tempo prior (center 120 BPM) prevents low-onset mush from
    # winning at implausible tempos (spec-style robustness tweak)
    fps_ = sr / HOP
    lags = np.arange(1, max_lag + 1)                 # len == max_lag; ac[1:]
    bpms = 60.0 * fps_ / lags
    prior = np.exp(-0.5 * (np.log2(bpms / 120.0) / 0.6) ** 2)
    valid = max(1, min_lag)
    masked = ac[1:][valid - 1 : max_lag] * prior[valid - 1 : max_lag]
    k = int(np.argmax(masked))
    best_lag = valid + k
    # parabolic interpolation around the autocorrelation peak refines BPM
    # beyond the integer-lag resolution (removes ~2-3% quantization error)
    bpm = 60.0 * fps / best_lag
    if 1 <= best_lag < max_lag:
        y0, y1, y2 = masked[k - 1], masked[k], masked[k + 1] if k + 1 < len(masked) else masked[k]
        denom = y0 - 2 * y1 + y2
        if abs(denom) > 1e-12:
            shift = 0.5 * (y0 - y2) / denom
            if abs(shift) <= 1.0:
                bpm = 60.0 * fps / (best_lag + shift)
    # resolve half/double tempo: prefer 80–160 window
    while bpm < 80:
        bpm *= 2
    while bpm > 160:
        bpm /= 2
    return round(bpm, 1)


def chroma(audio: np.ndarray, sr: int = SR) -> np.ndarray:
    """12-bin chroma from a long-window STFT (8192 -> ~2.7 Hz bins)."""
    n_fft = 8192 if len(audio) >= 8192 else 1024
    hop_c = 1024
    n_frames = 1 + max(0, (len(audio) - n_fft)) // hop_c
    if n_frames < 2:
        n_frames = 1
        idx = np.arange(n_fft)[None, :]
    else:
        idx = np.arange(n_fft)[None, :] + hop_c * np.arange(n_frames)[:, None]
    win = np.hanning(n_fft)
    frames = audio[idx] * win
    mag = np.abs(np.fft.rfft(frames))
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    band = (freqs >= 60.0) & (freqs <= 4500.0)
    mag = mag[:, band]
    freqs = freqs[band]
    # midi values per bin; distribute energy to nearest pitch classes
    midi = 69 + 12 * np.log2(np.maximum(freqs, 1e-6) / 440.0)
    pcs = np.round(midi).astype(int) % 12
    chroma = np.zeros(12)
    for pc in range(12):
        mask = pcs == pc
        if mask.any():
            chroma[pc] = mag[:, mask].sum()
    total = chroma.sum()
    return chroma / total if total > 0 else chroma


def detect_key(audio: np.ndarray, sr: int = SR) -> tuple[str, float]:
    """Krumhansl-Schmuckler: correlate chroma against major/minor profiles."""
    c = chroma(audio, sr)
    best_name, best_score = "", -np.inf
    for rotate in range(12):
        rotated = np.roll(c, -rotate)
        for mode, profile in (("maj", _KS_MAJOR), ("min", _KS_MINOR)):
            p = profile - profile.mean()
            q = rotated - rotated.mean()
            denom = (np.sqrt((p * p).sum()) * np.sqrt((q * q).sum()))
            score = float((p * q).sum() / denom) if denom > 1e-12 else 0.0
            if score > best_score:
                best_name = f"{_PITCHES[rotate]}{mode}"
                best_score = score
    return best_name, round(best_score, 3)


def parse_key(key: str) -> tuple[int, bool]:
    """'Emin' -> (4, False). Returns (pitch_class, is_major)."""
    if not key:
        return (0, False)
    body = key[:-3] if key[-3:] in ("maj", "min") else key
    major = key[-3:] == "maj"
    aliases = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
    body = aliases.get(body, body)
    try:
        return (_PITCHES.index(body), major)
    except ValueError:
        return (0, False)


def loudness_lufs(audio: np.ndarray, sr: int = SR) -> float:
    """EBU R128-style gated integrated loudness (simplified single-stage)."""
    frame_n = int(0.4 * sr)
    hop_n = int(0.1 * sr)
    n_blocks = max(0, 1 + (len(audio) - frame_n) // hop_n)
    if n_blocks <= 0:
        return -70.0
    blocks = [
        audio[i * hop_n : i * hop_n + frame_n]
        for i in range(n_blocks)
    ]
    powers = np.array([np.mean(b ** 2) for b in blocks if len(b) == frame_n])
    if not len(powers) or powers.max() <= 0:
        return -70.0
    # absolute gate: -70 LUFS. K-weighting is approximated with unweighted
    # power (within ~1-2 LU for pink-ish content; fine for relative gains).
    gated = powers[powers > 10 ** ((-70 + 0.691) / 10)]
    if not len(gated):
        return -70.0
    mean_power = gated.mean()
    # relative gate: blocks louder than (integrated -10 LU)
    integrated = -0.691 + 10 * np.log10(mean_power)
    rel_thr = 10 ** ((integrated - 10 + 0.691) / 10)
    gated2 = powers[powers > rel_thr]
    if not len(gated2):
        return integrated
    return round(-0.691 + 10 * np.log10(gated2.mean()), 1)


def energy_curve(audio: np.ndarray, sr: int = SR, hop: int = HOP) -> np.ndarray:
    n_frames = max(1, len(audio) // hop)
    padded = np.pad(audio, (0, max(0, n_frames * hop - len(audio))))
    fr = padded[: n_frames * hop].reshape(n_frames, hop)
    rms = np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    return rms / (rms.max() or 1.0)


def mean_energy(audio: np.ndarray, sr: int = SR) -> float:
    return float(np.mean(energy_curve(audio, sr)))


def beat_grid(audio: np.ndarray, bpm: float, sr: int = SR) -> list[float]:
    """Phase-lock beats onto the onset envelope: pick phase with max click alignment."""
    if bpm <= 0:
        return []
    fps = sr / HOP
    step_frames = 60.0 / bpm * fps
    env = onset_envelope(audio, sr)
    n_beats = int(len(env) / step_frames)
    if n_beats <= 0:
        return []
    best_phase, best_score = 0.0, -np.inf
    for phase in np.linspace(0, step_frames, 12, endpoint=False):
        idx = (phase + np.arange(n_beats) * step_frames).astype(int)
        idx = idx[idx < len(env)]
        score = env[idx].sum()
        if score > best_score:
            best_score, best_phase = score, phase
    times = []
    for b in range(n_beats):
        t = (best_phase + b * step_frames) / fps
        if t > len(audio) / sr:
            break
        times.append(round(t, 3))
    return times


def _downsample_curve(curve: np.ndarray, seconds: int = 60) -> np.ndarray:
    return curve[: int(seconds * (SR / HOP))]


def sections(energy: np.ndarray, bpm: float, sr: int = SR) -> list[dict]:
    """Coarse structure: segment on smoothed-energy change points, label by position.

    Returns [{'start': s, 'end': e, 'label': 'intro'|'verse'|'chorus'|'outro'}, ...]
    """
    dur = len(energy) * HOP / sr
    if dur <= 0:
        return []
    # smooth
    kernel = np.ones(SR // HOP) / (SR // HOP)
    sm = np.convolve(energy, kernel, mode="same")
    # detect large drops/rises as boundaries (quantized to ~4 bars)
    bar = 4 * 60.0 / (bpm or 100.0)
    min_seg = max(2.0, bar)
    bnds = [0.0]
    win = max(1, int(min_seg / (HOP / sr)))
    for i in range(win, len(sm) - win):
        left, right = sm[i - win : i].mean(), sm[i : i + win].mean()
        if abs(right - left) > 0.18 and (HOP / sr) * i - bnds[-1] >= min_seg:
            bnds.append((HOP / sr) * i)
    bnds.append(dur)
    out = []
    n = len(bnds) - 1
    for i in range(n):
        s, e = bnds[i], bnds[i + 1]
        if e - s < 0.5:
            continue
        rel = s / (bnds[-1] or 1)
        if i == 0 and rel < 0.12:
            label = "intro"
        elif i == n - 1 and s / (bnds[-1] - 0.12) > 0.85:
            label = "outro"
        else:
            level = sm[int(s / (HOP / sr)) : int(e / (HOP / sr))].mean()
            label = "chorus" if level > np.median(sm) * 1.1 else "verse"
        out.append({"start": round(s, 2), "end": round(e, 2), "label": label})
    return out


def boundaries(audio: np.ndarray, sr: int = SR, threshold_db: float = -45.0) -> tuple[float, float]:
    """Intro/outro boundaries: first/last frames above a silence threshold."""
    hop = HOP
    e = energy_curve(audio, sr, hop)
    thr = 10 ** (threshold_db / 20.0)
    active = np.where(e > thr)[0]
    if not len(active):
        return (0.0, len(audio) / sr)
    intro = active[0] * hop / sr
    outro_start = (active[-1] + 1) * hop / sr
    return (round(intro, 2), round(outro_start, 2))


def analyze_file(path: str, sr: int = SR, max_seconds: float = 90.0) -> dict:
    """Full Tier-1 (+light Tier-2) analysis of one file."""
    from muse.analysis.decoder import decode

    audio = decode(path, sr=sr, max_seconds=max_seconds)
    bpm = detect_bpm(audio, sr)
    key, key_score = detect_key(audio, sr)
    lufs = loudness_lufs(audio, sr)
    energy = mean_energy(audio, sr)
    grid = beat_grid(audio, bpm, sr)
    intro, outro = boundaries(audio, sr)
    struct = sections(energy_curve(audio, sr), bpm, sr)
    return {
        "bpm": bpm,
        "key": key,
        "key_score": key_score,
        "loudness": lufs,
        "energy": round(energy, 4),
        "beat_grid": grid,
        "beat_count": len(grid),
        "intro_sec": intro,
        "outro_sec": round(len(audio) / sr - outro, 2),
        "structure": struct,
        "tiers_done": 2,
    }