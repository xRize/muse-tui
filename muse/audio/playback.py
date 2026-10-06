"""Audio playback engine — sounddevice output fed by ffmpeg decoder threads.

Each Voice decodes its file to engine-rate stereo PCM in a worker thread and
exposes sample-counted position and fade state. The Engine mixes active voices
(prebuffered), handles end-of-stream / AutoMix handoff, volume and ReplayGain.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading

import numpy as np

log = logging.getLogger("muse.audio")

SR = 44100
CHANNELS = 2
_BLOCK = 2048  # frames per callback tick


class DecodeProcess:
    """Long-running `ffmpeg` process decoding one file to f32le stereo PCM."""

    def __init__(self, path: str, sr: int = SR, gain: float = 0.0,
                 tempo: float = 1.0, seek: float = 0.0):
        self.path = path
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [ffmpeg, "-v", "error", "-nostdin"]
        if seek > 0:
            cmd += ["-ss", f"{seek:g}"]
        cmd += ["-i", path]
        if abs(tempo - 1.0) > 1e-6:
            cmd += ["-filter:a", f"atempo={tempo:.4f}"]
        cmd += ["-f", "f32le", "-ac", str(CHANNELS), "-ar", str(sr), "pipe:1"]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        self.gain = gain  # linear scalar

    def read(self, frames: int) -> np.ndarray:
        """Read exactly `frames` frames; shorter read = end of stream."""
        want = frames * CHANNELS * 4
        buf = b""
        while len(buf) < want:
            chunk = self.proc.stdout.read(want - len(buf))
            if not chunk:
                break
            buf += chunk
        data = np.frombuffer(buf, dtype=np.float32).copy()
        n = len(data) // CHANNELS
        return data[: n * CHANNELS].reshape(n, CHANNELS)

    def close(self) -> None:
        try:
            self.proc.stdout.close()
        except Exception:
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    @property
    def failed(self) -> bool:
        return self.proc.poll() not in (None, 0) or self.proc.stdout is None


class Voice:
    """One playing track: decoder + fade state + gain."""

    def __init__(self, track_id: int, path: str, duration: float, gain: float = 0.0,
                 tempo: float = 1.0, seek: float = 0.0):
        self.track_id = track_id
        self.path = path
        self.duration = duration
        self.seek = seek
        self.decoder = DecodeProcess(path, SR, gain, tempo, seek)
        self.samples_played = 0
        self.samples_decoded = 0
        self.fade_start = None  # sample index where fade begins
        self.fade_samples = 0
        self.fade_to = 0.0      # target scalar at fade end
        self.level = 1.0        # current scalar multiplier
        self.done = False
        self.failed = False
        self.lock = threading.Lock()
        self._eof = False

    # -- timeline ------------------------------------------------------------
    @property
    def position_seconds(self) -> float:
        return self.seek + self.samples_played / SR

    def remaining_samples(self) -> int:
        return max(0, int((self.duration - self.position_seconds) * SR))

    def start_fade(self, fade_seconds: float, to_level: float) -> None:
        self.fade_start = self.samples_decoded
        self.fade_samples = int(fade_seconds * SR)
        self.fade_to = to_level

    def _current_level(self) -> float:
        if self.fade_start is None:
            return self.level
        t = (self.samples_decoded - self.fade_start) / max(1, self.fade_samples)
        return self.level + (self.fade_to - self.level) * min(1.0, max(0.0, t))

    # -- fill ----------------------------------------------------------------
    def fill(self, out: np.ndarray, n: int) -> int:
        """Mix up to `n` frames of this voice into `out`. Returns frames written."""
        if self.failed or self.done:
            return 0
        buf = np.empty((n, CHANNELS), dtype=np.float32)
        total = 0
        while total < n:
            got = self.decoder.read(n - total)
            if got.shape[0] == 0:
                self._eof = True
                break
            with self.lock:
                self.samples_decoded += got.shape[0]
            buf[total : total + got.shape[0]] = got
            total += got.shape[0]
        if total == 0:
            self.done = True
            return 0
        level = self._current_level()
        with self.lock:
            self.level = level
            self.samples_played += total
        out[:total] += buf[:total] * level
        return total


class Engine:
    """Mixer owning the current + next voices and the output stream."""

    def __init__(self, start_paused: bool = False):
        self.current: Voice | None = None
        self.upcoming: Voice | None = None   # prebuffered / crossfading voice
        self.master = 0.8
        self.stream = None
        self._stop = False
        self._lock = threading.RLock()
        self._started = threading.Event()
        self.start_paused = start_paused
        self.paused = start_paused
        self.handoff_done = threading.Event()   # set when automix handoff completes

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        self._stop = False
        self._started.clear()
        import sounddevice as sd

        try:
            self.stream = sd.OutputStream(
                samplerate=SR, channels=CHANNELS, dtype=np.float32,
                blocksize=_BLOCK, callback=self._cb,
            )
            self.stream.start()
        except Exception as e:
            log.error("audio device unavailable: %s", e)
            self.stream = None

    def stop(self) -> None:
        self._stop = True
        for v in (self.current, self.upcoming):
            if v:
                v.decoder.close()
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    # -- voice management ------------------------------------------------------
    def set_voice(self, voice: Voice | None) -> None:
        with self._lock:
            if self.current and self.current is not voice:
                self.current.decoder.close()
            self.current = voice
            self.upcoming = None

    def queue_voice(self, voice: Voice | None) -> None:
        """Set the prebuffered next voice (does not start mixing yet)."""
        with self._lock:
            if self.upcoming and self.upcoming is not voice:
                self.upcoming.decoder.close()
            self.upcoming = voice

    def position(self) -> float:
        v = self.current
        return v.position_seconds if v else 0.0

    def is_playing(self) -> bool:
        return self.current is not None and not self.paused

    # -- crossfade -------------------------------------------------------------
    def begin_crossfade(self, incoming: Voice, fade_seconds: float) -> bool:
        """Start AutoMix: outgoing begins fading to silence, incoming mixing in.
        Synchronous decoder prebuffers make the first block instant."""
        with self._lock:
            if self.current is None or self.current.failed:
                self.current = incoming
                return True
            self.current.start_fade(fade_seconds, 0.0)
            self.upcoming = incoming
            incoming.start_fade(fade_seconds, 1.0)
            return True

    def handoff_if_ready(self) -> None:
        """When outgoing finished, promote incoming. Clears handoff_done."""
        with self._lock:
            cur, nxt = self.current, self.upcoming
            if cur and cur.done and nxt:
                self.current = nxt
                self.upcoming = None
                if nxt.fade_start is not None:
                    nxt.fade_start = None  # end fade at promoted level -> keep ~1.0
                    nxt.level = 1.0
                cur.decoder.close()
                self.handoff_done.set()

    # -- callback ----------------------------------------------------------------
    def _cb(self, outdata, frames, time_info, status):
        outdata[:] = 0
        if self.paused or self._stop:
            self._started.set()
            return
        self._started.set()
        out = outdata.reshape(frames, CHANNELS)
        with self._lock:
            cur, nxt = self.current, self.upcoming
        if cur:
            cur.fill(out, frames)
        if nxt and not cur.done:
            nxt.fill(out, frames)
        out *= self.master
        self.handoff_if_ready()

    # -- controls -----------------------------------------------------------------
    def set_volume(self, v: float) -> None:
        self.master = max(0.0, min(1.0, v))

    def get_volume(self) -> float:
        return self.master

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def wait_started(self, timeout: float = 2.0) -> bool:
        return self._started.wait(timeout)