"""Lyrics fetching (LRCLIB, cached)."""
from __future__ import annotations

from muse.providers.media import fetch_lyrics


class LyricsProvider:
    name = "lyrics"

    def get(self, artist: str, title: str, album: str = "", duration: int = 0) -> dict:
        return fetch_lyrics(artist, title, album, duration)