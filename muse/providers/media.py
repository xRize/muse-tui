"""Lyrics + album art providers.

Lyrics: LRCLIB (free, no key) fetching synced/plain lyrics, cached to disk.
Covers: coverartarchive / Deezer open endpoints, cached; falls back to a
deterministic generated placeholder (no broken art in the prototype).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

import requests

from muse import paths

log = logging.getLogger("muse.lyrics")

_UA = {"User-Agent": "muse/0.1 (terminal music client prototype)"}
_cache_ttl = 86400


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._")[:80] or "untitled"


# -- lyrics -------------------------------------------------------------------

def fetch_lyrics(artist: str, title: str, album: str = "", duration: int = 0,
                 offline: bool = False) -> dict:
    """LRCLIB lookup. Returns {'text': str, 'synced': bool, 'cached': bool} or text=''."""
    key = hashlib.sha1(f"{artist}|{title}".lower().encode()).hexdigest()[:16]
    cache = paths.lyrics_dir() / f"{key}.json"
    if cache.exists():
        try:
            data = json.loads(cache.read_text())
            data["cached"] = True
            return data
        except (json.JSONDecodeError, OSError):
            pass
    if offline:
        return {"text": "", "synced": False, "cached": False}
    try:
        resp = requests.get(
            "https://lrclib.net/api/get",
            params={"artist_name": artist, "track_name": title,
                    "album_name": album, "duration": duration},
            headers=_UA, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.info("lyrics fetch failed for %s - %s: %s", artist, title, e)
        return {"text": "", "synced": False, "cached": False}
    result = {"text": data.get("plainLyrics") or "", "synced": bool(data.get("syncedLyrics"))}
    result["synced_text"] = data.get("syncedLyrics") or ""
    cache.write_text(json.dumps(result))
    return result


# -- covers --------------------------------------------------------------------

def fetch_cover(artist: str, album: str, offline: bool = False) -> str:
    """Return path to a cached cover image (real art if network, else generated)."""
    key = hashlib.sha1(f"{artist}|{album}".lower().encode()).hexdigest()[:16]
    cache = paths.covers_dir() / f"{key}.png"
    if cache.exists():
        return str(cache)
    if not offline:
        # try Deezer open API (no key) -> album art URL
        try:
            resp = requests.get("https://api.deezer.com/search/album",
                                params={"q": f"{artist} {album}", "limit": 1},
                                headers=_UA, timeout=10)
            resp.raise_for_status()
            data = resp.json().get("data") or []
            if data:
                cover_url = data[0].get("cover_medium") or data[0].get("cover")
                if cover_url:
                    img = requests.get(cover_url, headers=_UA, timeout=15,
                                       allow_redirects=True)
                    img.raise_for_status()
                    cache.write_bytes(img.content)
                    return str(cache)
        except Exception as e:
            log.debug("cover fetch fallback for %s/%s: %s", artist, album, e)
    generated = _generate_cover(artist, album, cache)
    return str(generated) if generated else ""


def _generate_cover(artist: str, album: str, dest: Path) -> Path | None:
    """Deterministic gradient placeholder with initials — never broken art."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    seed = int(hashlib.sha1(f"{artist}|{album}".encode()).hexdigest()[:8], 16)
    h1, h2 = seed % 360, (seed // 360) % 360
    img = Image.new("RGB", (300, 300))
    top = tuple(int(c * 0.85) for c in _hsv_to_rgb(h1, 0.6, 0.55))
    bottom = tuple(int(c * 0.85) for c in _hsv_to_rgb(h2, 0.6, 0.35))
    for y in range(300):
        t = y / 299.0
        row = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(300):
            img.putpixel((x, y), row)
    d = ImageDraw.Draw(img)
    initials = ((artist or "?")[:1] + (album or "?")[:1]).upper()
    d.text((150, 150), initials, fill=(255, 255, 255), anchor="mm")
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG")
    return dest


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[float, float, float]:
    i = int(h / 60) % 6
    f = h / 60 - int(h / 60)
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    table = [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)]
    return table[i]