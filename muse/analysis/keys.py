"""Camelot wheel harmony helpers for Smart Shuffle / AutoMix.

Camelot number 1..12; mode letter A (minor) / B (major). Identical number is a
modal swap; ±1 on the wheel is an energy move.
"""
from __future__ import annotations

from muse.analysis.features import _PITCHES, CAMELOT_MAJOR, CAMELOT_MINOR

_ALIASES = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}


def key_to_camelot(key: str) -> int:
    """'Emin' -> 9, 'Cmaj' -> 8. Returns 0 for empty/unknown."""
    if not key or len(key) < 4:
        return 0
    major = key.endswith("maj")
    if not major and not key.endswith("min"):
        return 0
    body = key[:-3]
    body = _ALIASES.get(body, body)
    try:
        pc = _PITCHES.index(body)
    except ValueError:
        return 0
    return (CAMELOT_MAJOR if major else CAMELOT_MINOR)[pc]


def camelot_label(key: str) -> str:
    n = key_to_camelot(key)
    if not n:
        return "?"
    mode = "B" if key.endswith("maj") else "A"
    return f"{n}{mode}"


def harmonic_compatibility(k1: str, k2: str) -> float:
    """Score 0..1. Same camelot = 1.0 (0.9 across mode swap); ±1 wheel = 0.75.
    Compatible mode pair 8A->8B style included; else 0.3."""
    n1, n2 = key_to_camelot(k1), key_to_camelot(k2)
    if not n1 or not n2:
        return 0.3
    m1 = not k1.endswith("maj")  # minor default when unparseable
    m2 = not k2.endswith("maj")
    if n1 == n2:
        return 0.9 if m1 != m2 else 1.0

    def adjacent(a: int, b: int) -> bool:
        return (a - b) % 12 in (1, 11)

    if adjacent(n1, n2):
        return 0.75
    return 0.3