"""Decode audio files to float32 mono PCM via ffmpeg (already a dependency).

Uses `ffmpeg -f f32le -ac 1 -ar <sr>` piped to stdout. Works for wav/mp3/flac/
ogg/m4a — anything ffmpeg supports. Offline-safe (no downloads).
"""
from __future__ import annotations

import shutil
import subprocess

import numpy as np

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


class DecodeError(RuntimeError):
    pass


def decode(path: str, sr: int = 22050, max_seconds: float | None = None) -> np.ndarray:
    """Decode to float32 mono at `sr`. Raises DecodeError on failure."""
    cmd = [FFMPEG, "-v", "error", "-nostdin", "-i", path]
    if max_seconds:
        cmd += ["-t", f"{max_seconds:g}"]
    cmd += ["-f", "f32le", "-ac", "1", "-ar", str(sr), "pipe:1"]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        raise DecodeError(f"ffmpeg failed for {path}: {e.stderr.decode(errors='replace')[:400]}") from e
    raw = proc.stdout
    audio = np.frombuffer(raw, dtype=np.float32).copy()
    if audio.size == 0:
        raise DecodeError(f"no audio decoded from {path}")
    return audio


def ffprobe_duration(path: str) -> float:
    """File duration in seconds via ffprobe."""
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", path]
    try:
        out = subprocess.run(cmd, capture_output=True, check=True, text=True).stdout.strip()
        return float(out)
    except (subprocess.CalledProcessError, ValueError):
        return 0.0