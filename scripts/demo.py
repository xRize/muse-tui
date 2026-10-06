"""Offline demo: generate a small synthetic library, run the full workflow.

Usage: .venv/bin/python scripts/demo.py
Generates 6 tracks (varied BPM/keys) under ~/Music/muse-demo/, imports them,
analyzes them, then exercises search/queue/radio/automix-preview through the
muse API. Audio playback is skipped (device not required).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from muse.analysis.synth import click_track, chord_pad_track, hybrid_track
from muse import paths

OUT = Path.home() / "Music" / "muse-demo"

SPECS = [  # (name, kind, bpm, key_pc)
    ("Sunrise_Click", "hybrid", 120.0, 9),   # A minor
    ("Night_Drive", "hybrid", 124.0, 4),     # E minor
    ("Paper Planes", "click", 104.0, 0),
    ("Deep_Waters", "hybrid", 88.0, 5),      # F minor
    ("Neon_Motion", "hybrid", 128.0, 11),    # B minor
    ("Afterglow", "hybrid", 96.0, 2),        # D minor
]


def generate() -> list[Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    files = []
    for name, kind, bpm, pc in SPECS:
        p = OUT / f"{name.replace(' ', '_')}.wav"
        if not p.exists():
            if kind == "click":
                click_track(p, bpm, bars=24)
            elif kind == "pad":
                chord_pad_track(p, pc, bpm=bpm, seconds=20.0)
            else:
                hybrid_track(p, bpm, pc, seconds=20.0)
        files.append(p)
    return files


def main() -> int:
    files = generate()
    print(f"generated {len(files)} demo tracks in {OUT}")

    from muse.db import Database
    from muse.analysis import worker as aw
    from muse.smart import shuffle as S
    from muse.smart import automix as AM

    db = Database(paths.db_path())
    for f in files:
        tid = aw.import_file(db, f)
        # synthetic files carry no tags; give the demo a nicer library view
        t = db.get_track(tid)
        db.upsert_track(title=t["title"], artist="muse demo",
                        album="Synthetic Sessions", file_path=str(f))
    print(f"library: {db.stats()}")

    ids = db.all_track_ids()
    for tid in ids:
        aw.run_analysis(db, tid)
    print("analyzed all pending")

    for tid in ids:
        t = db.get_track(tid)
        a = db.get_analysis(tid) or {}
        print(f"  {t['title']:<16} BPM {a.get('bpm')!s:<6} key {a.get('key')!s:<6} "
              f"LUFS {a.get('loudness')} energy {a.get('energy')}")

    # smart shuffle sequence from track 1
    seq = S.radio_sequence(db, ids[0], length=4)
    print("\nSmart Shuffle radio from:", db.get_track(ids[0])["title"])
    for t in seq:
        print(f"  -> {t['title']:<16} score {t.get('transition_score')}")

    # automix dry run between two neighbors
    a1 = db.get_analysis(ids[0]) or {}
    a2 = db.get_analysis(ids[1]) or {}
    a1["duration"] = db.get_track(ids[0])["duration"] or 20.0
    a2["duration"] = db.get_track(ids[1])["duration"] or 20.0
    plan = AM.plan_transition(a1, a2)
    print("\nAutoMix preview:\n" + AM.format_plan(
        db.get_track(ids[0])["title"], db.get_track(ids[1])["title"], plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())