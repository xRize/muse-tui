"""YouTube provider (yt-dlp subprocess).

Search resolves to URLs; `muse get <url>` downloads with legal disclaimers and
registers the file + tags in the library. Never bundles DRM circumvention.
"""
from __future__ import annotations

import logging
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
                                 check=True).stdout
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

    @staticmethod
    def download(url: str, dest_dir: str | Path, extra_opts: list[str] | None = None,
                 on_progress=None) -> Path | None:
        """Download audio via yt-dlp, return the file path. Applies TERMS_NOTICE."""
        exe = _ytdlp()
        if not exe:
            raise RuntimeError("yt-dlp not installed (pip install yt-dlp)")
        dest_dir = Path(dest_dir).expanduser()
        dest_dir.mkdir(parents=True, exist_ok=True)
        cmd = [exe, "--no-playlist", "--newline", "-o", str(dest_dir / "%(title)s.%(ext)s"),
               "-x", "--audio-format", "mp3", "--embed-thumbnail", "--add-metadata"]
        if extra_opts:
            cmd += extra_opts
        cmd.append(url)
        log.info("starting download of %s (%s)", url, TERMS_NOTICE)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        last_file: Path | None = None
        for line in proc.stdout or []:
            line = line.strip()
            if on_progress and (line.startswith("[download]") or line.startswith("[ExtractAudio]")):
                on_progress(line)
            if line.startswith("[ExtractAudio] Destination:") or "Destination:" in line:
                path = line.split("Destination:", 1)[1].strip()
                last_file = Path(path) if path else None
            if line.startswith("[EmbedThumbnail]") or line.startswith("[Metadata]"):
                pass
        proc.wait()
        if proc.returncode != 0:
            log.error("yt-dlp exited %s", proc.returncode)
            return None
        if last_file is None:
            # newest audio file in dest
            audio = sorted(dest_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
            last_file = audio[-1] if audio else None
        return last_file