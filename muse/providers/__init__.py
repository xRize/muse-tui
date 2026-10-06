"""muse.providers — source, lyrics and artwork providers."""
from __future__ import annotations

from muse.providers import apple, covers, local, lyrics, youtube  # noqa: F401


def all_providers() -> dict:
    return {
        "local": local.Provider,
        "youtube": youtube.Provider,
        "apple": apple.Provider,
    }


def search_all(query: str, offline: bool = False) -> list[dict]:
    """Search across providers. Offline mode uses local only."""
    results = []
    results.extend(local.Provider().search(query))
    if not offline:
        try:
            results.extend(youtube.Provider().search(query))
        except Exception:
            pass
    return results


def search_local(query: str) -> list[dict]:
    return local.Provider().search(query)