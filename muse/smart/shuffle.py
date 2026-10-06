"""Smart Shuffle: next-track scoring (BPM/key/energy match, no repeats)."""
from __future__ import annotations

import math
import random
import time

from muse import db as dbmod
from muse.analysis.keys import harmonic_compatibility

# scoring weights (spec §3; tunable)
W_BPM = 0.35
W_KEY = 0.25
W_ENERGY = 0.20
W_REPEAT = 0.15
W_LIKE = 0.05
# generate-queue bonus for same/similar genre (added to transition_score)
W_GENRE = 8.0


def genre_bonus(a: dict, b: dict) -> float:
    """0..W_GENRE: same genre tag scores full, shared words partial."""
    ga = (a.get("genre") or "").strip().lower()
    gb = (b.get("genre") or "").strip().lower()
    if not ga or not gb:
        return 0.0
    if ga == gb:
        return W_GENRE
    wa, wb = set(ga.replace("-", " ").replace("/", " ").split()), \
        set(gb.replace("-", " ").replace("/", " ").split())
    overlap = wa & wb
    if overlap:
        return round(W_GENRE * len(overlap) / max(len(wa | wb), 1), 1)
    return 0.0


def bpm_score(bpm_a: float, bpm_b: float) -> float:
    """1.0 at equal tempo, 0.5 at ±6%, 0 at ±20%+ (tolerates half/double)."""
    if not bpm_a or not bpm_b:
        return 0.4
    r = abs(bpm_b - bpm_a) / max(bpm_a, 1e-6)
    r = min(r, abs(bpm_b * 2 - bpm_a) / max(bpm_a, 1e-6),
            abs(bpm_b - bpm_a * 2) / max(bpm_a, 1e-6))
    return max(0.0, 1.0 - r / 0.20) ** 1.5


def energy_score(e_a: float, e_b: float) -> float:
    if e_a is None or e_b is None:
        return 0.5
    return max(0.0, 1.0 - abs(e_a - e_b) * 4.0)


def repeat_penalty(a: dict, cand: dict, pool_size: int) -> float:
    """0..1; 0 for very recent plays, rising until the pool has rotated."""
    lp = cand.get("last_played") or 0
    if not lp:
        return 1.0
    age = time.time() - lp
    # considered 'recent' for half the pool rotation time (rough proxy: pool tracks * 0.5h)
    horizon = max(900.0, pool_size * 45.0)
    if age >= horizon:
        return 1.0
    return max(0.0, age / horizon) ** 2


def transition_score(a: dict, b: dict, pool_size: int = 50) -> float:
    """Score A -> B: 0..100 (spec §3 dry-run 'Transition Score')."""
    aa = a.get("analysis") or {}
    ba = b.get("analysis") or {}
    s = 0.0
    s += W_BPM * bpm_score(aa.get("bpm") or 0, ba.get("bpm") or 0) * 100
    s += W_KEY * harmonic_compatibility(aa.get("key") or "", ba.get("key") or "") * 100
    s += W_ENERGY * energy_score(aa.get("energy"), ba.get("energy")) * 100
    s += W_REPEAT * repeat_penalty(a, b, pool_size) * 100
    liked = b.get("liked")
    s += W_LIKE * (100.0 if liked else 0.0)
    return round(s, 1)


def load_features(database: dbmod.Database, ids: list[int]) -> list[dict]:
    """Join track rows with their analysis (bpm/key/energy)."""
    out = []
    for tid in ids:
        t = database.get_track(tid)
        if not t:
            continue
        t["analysis"] = database.get_analysis(tid) or {}
        out.append(t)
    return out


def smart_shuffle_next(database: dbmod.Database, current_track_id: int | None,
                       exclude_ids: set[int] | None = None,
                       pool_size: int = 50, top_k: int = 5) -> dict | None:
    """Pick the best next track; randomize among top-k to keep variety."""
    exclude = exclude_ids or set()
    all_ids = [tid for tid in database.all_track_ids() if tid not in exclude]
    if not all_ids:
        return None
    pool = all_ids[:pool_size] if len(all_ids) <= pool_size else random.sample(all_ids, pool_size)
    feats = load_features(database, pool)
    if not feats:
        return None
    cur = None
    if current_track_id:
        cur_rows = load_features(database, [current_track_id])
        cur = cur_rows[0] if cur_rows else None
    scored = []
    for cand in feats:
        if cur and cand["id"] == cur["id"]:
            continue
        ts = transition_score(cur or {}, cand, pool_size) if cur else 60.0
        scored.append((ts, cand))
    if not scored:
        return None  # only candidate is the current track itself
    scored.sort(key=lambda x: -x[0])
    top = scored[: max(1, min(top_k, len(scored)))]
    # score-weighted randomization: a clear winner is picked almost always,
    # closer matches still get variety
    best = top[0][0]
    weights = [math.exp((ts - best) / 12.0) for ts, _ in top]
    (ts, pick), = random.choices(top, weights=weights, k=1) or [(None, None)]
    if pick is None:  # pragma: no cover
        ts, pick = top[0]
    pick["transition_score"] = ts
    return pick


def radio_sequence(database: dbmod.Database, seed_track_id: int | None,
                   length: int = 12) -> list[dict]:
    """Muse Radio: greedily sequence smart-shuffled picks from a seed track."""
    seq = []
    picked: set[int] = set()
    cur_id = seed_track_id
    for _ in range(length):
        pick = smart_shuffle_next(database, cur_id, exclude_ids=picked)
        if not pick:
            break
        seq.append(pick)
        picked.add(pick["id"])
        cur_id = pick["id"]
    return seq


def similar_tracks(database: dbmod.Database, seed_track_id: int,
                   length: int = 5, min_score: float = 40.0,
                   exclude_ids: set[int] | None = None) -> list[dict]:
    """Best transition-scored matches for a seed (Generate Queue).

    Candidates are ranked by transition_score plus a genre bonus ("similar
    in genre, bpm ... good transition score"); only matches scoring at least
    `min_score` are returned so a quiet library yields a short queue rather
    than junk. Scored candidates also carry their score in 'transition_score'.
    """
    exclude = set(exclude_ids or set()) | {seed_track_id}
    seed_rows = load_features(database, [seed_track_id])
    if not seed_rows:
        return []
    seed = seed_rows[0]
    scored = []
    for tid in database.all_track_ids():
        if tid in exclude:
            continue
        cand_rows = load_features(database, [tid])
        if not cand_rows:
            continue
        cand = cand_rows[0]
        # unanalyzed candidates can't guarantee a good transition: skip them
        a = cand.get("analysis") or {}
        if not a.get("bpm"):
            continue
        ts = transition_score(seed, cand) + genre_bonus(seed, cand)
        if ts >= min_score:
            cand["transition_score"] = round(ts, 1)
            scored.append(cand)
    scored.sort(key=lambda t: -t["transition_score"])
    return scored[:length]