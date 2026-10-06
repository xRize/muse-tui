"""YouTube provider (yt-dlp subprocess).

Search resolves to URLs; `muse get <url>` downloads with legal disclaimers and
registers the file + tags in the library. Never bundles DRM circumvention.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("muse.youtube")

TERMS_NOTICE = (
    "Downloads use yt-dlp. Only download media you own or that is licensed for it; "
    "respect YouTube's Terms of Service and applicable copyright law. "
    "muse does not process DRM-protected streams."
)


def _ytdlp() -> str | None:
    exe = shutil.which("yt-dlp")
    if exe:
        return exe
    # fall back to venv / pipx installs not on PATH
    for cand in ("~/.local/bin/yt-dlp", "/opt/homebrew/bin/yt-dlp"):
        p = Path(cand).expanduser()
        if p.exists():
            return str(p)
    try:
        import sys

        import yt_dlp  # noqa: F401
        return str(Path(sys.executable).parent / "yt-dlp")
    except ImportError:
        return None


def _run(proc_args: list[str]) -> subprocess.CompletedProcess:
    """Safe yt-dlp invocation for contexts where this process's std streams
    may be revoked/broken (detached daemons): children get /dev/null stdin."""
    return subprocess.run(proc_args, capture_output=True, text=True,
                          timeout=120, stdin=subprocess.DEVNULL)


def available() -> bool:
    """yt-dlp usable for downloads? (search requires it too)."""
    return _ytdlp() is not None


class Provider:
    name = "youtube"

    def search(self, query: str, limit: int = 10) -> list[dict]:
        exe = _ytdlp()
        if not exe:
            return []
        cmd = [exe, "--flat-playlist", "--dump-json", "--no-warnings",
               f"ytsearch{limit}:{query}"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=45,
                                 stdin=subprocess.DEVNULL, check=True).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            log.warning("yt-dlp search failed: %s", e)
            return []
        results = []
        for line in out.splitlines():
            if not line.strip():
                continue
            import json
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            results.append({
                "title": entry.get("title") or "",
                "artist": entry.get("uploader") or entry.get("channel") or "",
                "duration": entry.get("duration") or 0,
                "url": entry.get("url") or entry.get("webpage_url") or "",
                "provider": "youtube",
            })
        return results

    AUDIO_EXTS = (".mp3", ".m4a", ".opus")

    @staticmethod
    def download(url: str, dest_dir: str | Path, extra_opts: list[str] | None = None,
                 on_progress=None) -> Path | None:
        """Download one item's audio via yt-dlp; returns the file path (or None).

        `--no-playlist` keeps single-video pages from exploding into
        their parent playlist; playlist URLs are handled by download_playlist.
        """
        files, _rc, _tail = Provider._run_ytdlp(["--no-playlist", url], dest_dir,
                                                extra_opts, on_progress)
        return files[0] if files else None

    @staticmethod
    def download_playlist(url: str, dest_dir: str | Path, workers: int = 4,
                          extra_opts: list[str] | None = None,
                          on_progress=None) -> tuple[list[Path], int, str]:
        """Download a playlist's audio entries via yt-dlp. Applies TERMS_NOTICE.

        Returns (files, yt_dlp_exit_code, failure_tail); files landed even on
        partial failure are all registered by the caller. yt-dlp processes
        entries sequentially — `workers` maps to --concurrent-fragments
        (within-item parallelism).
        """
        extra = list(extra_opts or []) + [f"--concurrent-fragments={max(1, workers)}"]
        files, rc, tail = Provider._run_ytdlp(["--yes-playlist", url], dest_dir,
                                              extra, on_progress)
        log.info("playlist download finished: %d file(s), yt-dlp exit %s",
                 len(files), rc)
        return files, rc, tail

    @staticmethod
    def _run_ytdlp(args: list[str], dest_dir: str | Path,
                   extra_opts: list[str] | None,
                   on_progress=None) -> tuple[list[Path], int, str]:
        """Shared yt-dlp runner. Returns (new_audio_files, exit_code, tail).

        `tail` holds the last lines of yt-dlp output (empty when it exited
        0) so callers can surface the real failure reason to the user.
        """
        exe = _ytdlp()
        if not exe:
            raise RuntimeError("yt-dlp not installed (pip install yt-dlp)")
        dest_dir = Path(dest_dir).expanduser()
        dest_dir.mkdir(parents=True, exist_ok=True)
        known = {p.name for p in dest_dir.iterdir() if p.is_file()}
        cmd = [exe, "--newline",
               "-o", str(dest_dir / "%(title)s.%(ext)s"),
               "-x", "--audio-format", "mp3",
               "--embed-thumbnail", "--add-metadata"]
        if extra_opts:
            cmd += extra_opts
        # URL(s) last (yt-dlp treats the first non-option as the target and
        # options after it as per-input keys)
        cmd += args
        log.info("starting yt-dlp download (%s)", TERMS_NOTICE)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1,
                                # never inherit the daemon's (possibly revoked)
                                # stdin descriptor: child CPython init dies on it
                                stdin=subprocess.DEVNULL)
        raw: list[str] = []
        dests: list[Path] = []
        for line in proc.stdout or []:
            line = line.strip()
            if line:
                raw.append(line)
            if "Destination:" in line:
                path = line.split("Destination:", 1)[1].strip()
                if path:
                    dests.append(Path(path))
            elif line.startswith("[download] ") and "has already been downloaded" in line:
                path = line[len("[download] "):].split(" has already")[0].strip()
                if path:
                    dests.append(Path(path))
            if on_progress and (line.startswith("[download]") or line.startswith("[ExtractAudio]")):
                on_progress(line)
        proc.wait()
        rc = proc.returncode
        tail = ""
        if rc != 0:
            tail = " | ".join(raw[-8:])[-400:]
            log.error("yt-dlp exited %s (%d output lines): %s",
                      rc, len(raw), tail or "<no output captured>")
            dbg = os.environ.get("MUSE_YTDLP_DEBUG")
            if dbg:
                Path(dbg).write_text("\n".join(raw))
        new_files = [
            p for p in dest_dir.iterdir()
            if p.is_file() and p.name not in known
            and p.suffix.lower() in Provider.AUDIO_EXTS
        ]
        files = new_files
        if not files and rc == 0:
            # postprocessed destinations (incl. "already downloaded" re-runs):
            # a video path may name the pre-merger container — its mp3 twin
            # is what lands on disk
            for p in dests:
                if p.suffix.lower() in Provider.AUDIO_EXTS and p.is_file():
                    files.append(p)
                elif (p.with_suffix(".mp3")).is_file():
                    files.append(p.with_suffix(".mp3"))
        files = sorted(dict.fromkeys(files), key=lambda p: p.stat().st_mtime)
        return files, rc, tail