"""Album-art fetching/caching."""
from __future__ import annotations

from muse.providers.media import fetch_cover


class CoverProvider:
    name = "covers"

    def get(self, artist: str, album: str) -> str:
        return fetch_cover(artist, album)