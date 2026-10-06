"""Apple Music provider (MusicKit scaffold).

Playback of Apple Music catalog content is DRM-protected: muse intentionally
does NOT decrypt streams. This provider syncs metadata via the official
MusicKit REST API when the user supplies a developer token, and always fails
playback with an explicit DRM notice instead of pretending otherwise.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("muse.apple")

DRM_NOTICE = (
    "Apple Music tracks are DRM-protected and cannot be decoded by muse. "
    "Playback via MusicKit web playback requires a browser session; this "
    "prototype reports, syncs and queues them but cannot play them."
)

CREDENTIALS_FILE = "~/.config/muse/credentials.json"


def credentials_path() -> Path:
    return Path(CREDENTIALS_FILE).expanduser()


def load_credentials() -> dict:
    p = credentials_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_credentials(token: str, storefront: str = "us") -> None:
    p = credentials_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"developer_token": token, "storefront": storefront}))
    p.chmod(0o600)  # user-only permissions (spec §6 security)


def catalog_search(query: str, token: str, storefront: str = "us", limit: int = 10) -> list[dict]:
    """MusicKit REST catalog search (official API). Requires network + token."""
    import requests

    resp = requests.get(
        f"https://api.music.apple.com/v1/catalog/{storefront}/search",
        params={"term": query, "limit": limit, "types": "songs"},
        headers={"Authorization": f"Bearer {token}", "Origin": "https://music.apple.com"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    songs = (data.get("results", {}).get("catalog", {}) or {}).get("songs", {})
    out = []
    for s in songs.get("data", []):
        attrs = s.get("attributes", {})
        out.append({
            "title": attrs.get("name", ""),
            "artist": attrs.get("artistName", ""),
            "album": attrs.get("albumName", ""),
            "duration": attrs.get("durationInMillis", 0) / 1000.0,
            "url": s.get("url", ""),
            "apple_id": s.get("id", ""),
            "provider": "apple",
            "drm": True,
        })
    return out


class Provider:
    name = "apple"

    def __init__(self, database=None):
        self._db = database

    @property
    def configured(self) -> bool:
        return bool(load_credentials().get("developer_token"))

    def search(self, query: str, limit: int = 10) -> list[dict]:
        creds = load_credentials()
        token = creds.get("developer_token")
        if not token:
            return []
        try:
            return catalog_search(query, token, storefront=creds.get("storefront", "us"),
                                  limit=limit)
        except Exception as e:
            log.warning("MusicKit search failed: %s", e)
            return []

    def get_stream(self, track_id: int) -> dict | None:
        from muse import db as dbmod
        db = self._db or dbmod.default_db()
        t = db.get_track(track_id)
        if not t:
            return None
        return {"track": t, "drm": True}