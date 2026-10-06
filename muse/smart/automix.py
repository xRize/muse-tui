"""AutoMix transition planner: beat alignment, tempo ratio, overlap timing.

Dry-run planner (spec §3 'AutoMix Example (Dry-Run Visualizer)') + helpers used
by the daemon to schedule crossfades. Pure math on cached analysis — no audio.
"""
from __future__ import annotations

from muse.analysis.keys import camelot_label, harmonic_compatibility

MAX_TEMPO_ADJUST = 0.04  # beyond ±4%, don't time-stretch; plain crossfade


def plan_transition(a: dict, b: dict, transition_length: float = 8.0,
                    beat_match: bool = True, harmonic_match: bool = True,
                    vocal_mode: bool = False) -> dict:
    """Plan A -> B from cached analysis dicts. Returns a printable plan dict.

    Returns keys: overlap_sec, tempo_scale (1.0 = none), a_end_sec, b_start_sec,
    alignment (A anchor time -> B start), curve, score, score_label, notes.

    vocal_mode (spec §3 Tier-2): uses cached vocal regions so A's vocals are
    not faded mid-line and B's first vocal ideally waits until the overlap ends.
    """
    notes: list[str] = []
    bpm_a = a.get("bpm") or 0
    bpm_b = b.get("bpm") or 0
    grid_a = a.get("beat_grid") or []
    dur_a = a.get("duration") or 0
    intro_b = b.get("intro_sec") or 0

    # --- tempo ------------------------------------------------------------
    tempo_scale = 1.0
    if bpm_a > 0 and bpm_b > 0:
        r = bpm_b / bpm_a
        if abs(r - 1.0) <= MAX_TEMPO_ADJUST:
            tempo_scale = r        # B plays at scale; effective tempo = bpm_a
            notes.append(f"tempo-adjust B {bpm_b:g}->{bpm_a:g} BPM ({(r-1)*100:+.2f}%)")
        elif bpm_b > bpm_a and abs(bpm_b / 2 - bpm_a) / max(bpm_a, 1) <= MAX_TEMPO_ADJUST:
            tempo_scale = bpm_b / (2 * bpm_a)
            notes.append(f"half-tempo B: {bpm_b:g}->{2*tempo_scale*bpm_a:g} BPM")
        elif bpm_b < bpm_a and abs(bpm_b * 2 - bpm_a) / max(bpm_a, 1) <= MAX_TEMPO_ADJUST:
            tempo_scale = (bpm_b * 2) / bpm_a
            notes.append(f"double-tempo B: {bpm_b:g}->{tempo_scale*bpm_a:g} BPM")
        else:
            notes.append("tempo gap too wide; plain crossfade (no time-stretch)")

    # --- beat alignment -----------------------------------------------------
    a_end = dur_a or 0
    if beat_match and grid_a and dur_a:
        # fade starts `transition_length` before outro end; anchor on a beat within
        # the final 8 bars
        target = dur_a - transition_length
        window = min(8 * 4 * 60.0 / bpm_a, dur_a * 0.25) if bpm_a else 8.0
        lo = max(0.0, target - window)
        hi = min(dur_a, target + window)

        # vocal_mode: never fade A mid-phrase — anchor on the beat nearest to
        # A's last vocal end (an instrumental tail carries the transition)
        vocal_end = (a.get("vocal_end_sec") or 0) if vocal_mode else 0.0
        if vocal_end and dur_a and vocal_end < dur_a - 0.5:
            target = max(vocal_end, dur_a - 1.5 * transition_length)
        # grid is phase-locked, so choose nearest bar-aligned beat (every 4th)
        downbeats = grid_a[::4]
        anchors = [t for t in grid_a if lo <= t <= hi]
        cands = [t for t in downbeats if lo <= t <= hi] or anchors
        if cands:
            closest = min(cands, key=lambda t: abs(t - target))
            if vocal_end and abs(closest - target) > window:
                closest = None
            if closest is not None:
                a_end = closest
                notes.append(f"beat align at A[{a_end:g}s]")
            else:
                notes.append("no downbeat in window; using outro end")
        else:
            a_end = target if vocal_end else dur_a
            if a_end and not vocal_end:
                notes.append("no downbeat in window; using outro end")
    else:
        if beat_match and not grid_a:
            notes.append("no beat grid; using outro end")
        a_end = dur_a

    b_start = max(0.0, intro_b * 0.5) if intro_b else 0.0
    if intro_b:
        notes.append(f"skip {b_start:g}s of B's intro")

    # --- vocal awareness (spec §3 'vocal avoidance') --------------------------
    if vocal_mode:
        overlap = transition_length
        a_vent = a.get("vocal_end_sec") or 0.0
        if a_vent and dur_a and (dur_a - a_vent) < 1.0 \
                and a_end and (a_end - overlap) < a_vent:
            overlap = max(2.0, round(overlap / 2, 1))
            notes.append("A's vocals run to the end; halving overlap")
        b1v = b.get("vocal_intro_sec") or 0.0
        if b1v and b1v < overlap:
            overlap = b1v  # B starts singing inside the overlap; shorten it
            notes.append(f"B's first vocal at {b1v:g}s; overlap shortened")
        transition_length = overlap

    # --- harmony / score ------------------------------------------------------
    key_a, key_b = a.get("key") or "", b.get("key") or ""
    harm = harmonic_compatibility(key_a, key_b)
    bpm_compat = 1.0
    if bpm_a and bpm_b:
        bpm_compat = max(0.0, 1.0 - abs(bpm_b - bpm_a) / max(bpm_a, 1) / 0.20) ** 1.5
    energy_a = a.get("energy")
    energy_b = b.get("energy")
    energy_compat = max(0.0, min(1.0,
        1.0 - abs((energy_a or 0) - (energy_b or 0)) * 4.0)) \
        if energy_a is not None and energy_b is not None else 0.5
    bpm_compat = max(0.0, min(1.0, bpm_compat))
    harm = max(0.0, min(1.0, harm))
    score = 100 * (0.35 * bpm_compat + 0.30 * harm + 0.20 * energy_compat
                   + 0.15 * (1.0 if (tempo_scale != 1.0 or bpm_compat > 0.7) else 0.0))
    score = max(0.0, min(100.0, score))
    if harmonic_match and harm < 0.5:
        notes.append("keys clash; keeping overlap short")
        score = round(score * 0.9, 1)
    score = round(score, 1)
    label = ("highly compatible" if score >= 80 else
             "compatible" if score >= 60 else
             "marginal" if score >= 40 else "poor")

    overlap = transition_length
    if harm < 0.5:
        overlap = min(overlap, 4.0)

    return {
        "overlap_sec": round(overlap, 2),
        "tempo_scale": round(tempo_scale, 4),
        "a_end_sec": round(a_end, 2),
        "b_start_sec": round(b_start, 2),
        "curve": "equal-power",
        "score": score,
        "score_label": label,
        "bpm_a": bpm_a,
        "bpm_b": bpm_b,
        "key_a": key_a,
        "key_b": key_b,
        "camelot_a": camelot_label(key_a) if key_a else "?",
        "camelot_b": camelot_label(key_b) if key_b else "?",
        "a_vocal_end": a.get("vocal_end_sec"),
        "b_vocal_intro": b.get("vocal_intro_sec"),
        "notes": notes,
    }


def format_plan(a_title: str, b_title: str, plan: dict) -> str:
    lines = [
        f"Track A: {a_title}  ({plan['bpm_a']:g} BPM, {plan['key_a'] or '?'} "
        f"[{plan['camelot_a']}], outro@{plan['a_end_sec']:g}s)",
        f"Track B: {b_title}  ({plan['bpm_b']:g} BPM, {plan['key_b'] or '?'} "
        f"[{plan['camelot_b']}], intro {plan['b_start_sec']:g}s)",
        "Transition:",
        f"  Overlap length: {plan['overlap_sec']:g}s ({plan['curve']} fade)",
    ]
    if plan["tempo_scale"] != 1.0:
        lines.append(f"  Tempo scale on B: x{plan['tempo_scale']:.4f}")
    if plan.get("a_vocal_end") is not None and plan.get("b_vocal_intro") is not None:
        lines.append(
            f"  Vocal crossfade: A's vocals end at {plan['a_vocal_end']:g}s, "
            f"B's vocals enter at {plan['b_vocal_intro']:g}s")
    for n in plan["notes"]:
        lines.append(f"  - {n}")
    lines.append(f"Transition Score: {plan['score']:g}/100 ({plan['score_label']})")
    return "\n".join(lines)