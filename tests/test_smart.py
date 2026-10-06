"""Smart Shuffle + AutoMix planner tests."""
import time


def test_bpm_score_shape():
    from muse.smart.shuffle import bpm_score
    assert bpm_score(120, 120) == 1.0
    assert bpm_score(120, 121.5) > 0.8        # ~1% off
    assert bpm_score(120, 144) < 0.2          # 20% off
    assert bpm_score(120, 60) > 0.8           # half-time is compatible
    assert bpm_score(120, 0) == 0.4           # unknown analysis


def test_energy_score():
    from muse.smart.shuffle import energy_score
    assert energy_score(0.5, 0.5) == 1.0
    assert energy_score(0.2, 0.8) == 0.0
    assert energy_score(None, 0.5) == 0.5


def test_transition_score_ordering(db, tmp_dirs):
    """A better-matched candidate must outscore a bad match."""
    from muse.smart import shuffle as S

    good = db.upsert_track(title="Match", file_path="/tmp/match.wav")
    bad = db.upsert_track(title="Clash", file_path="/tmp/clash.wav")
    db.save_analysis(good, bpm=120.0, key="Amin", energy=0.5, tiers_done=2)
    db.save_analysis(bad, bpm=170.0, key="C#maj", energy=0.05, tiers_done=2)
    current = db.upsert_track(title="Now", file_path="/tmp/now.wav")
    db.save_analysis(current, bpm=122.0, key="Amin", energy=0.48, tiers_done=2)

    cur = db.get_track(current); cur["analysis"] = db.get_analysis(current)
    g = db.get_track(good); g["analysis"] = db.get_analysis(good)
    btrack = db.get_track(bad); btrack["analysis"] = db.get_analysis(bad)
    s_good = S.transition_score(cur, g)
    s_bad = S.transition_score(cur, btrack)
    assert s_good > s_bad + 10


def test_no_repeat_penalty(db):
    from muse.smart.shuffle import repeat_penalty
    cand = {"last_played": time.time() - 30}
    assert repeat_penalty({}, cand, pool_size=50) < 0.2
    cand2 = {"last_played": time.time() - 10 * 3600}
    assert repeat_penalty({}, cand2, pool_size=50) == 1.0
    assert repeat_penalty({}, {"last_played": 0}, 50) == 1.0


def test_smart_shuffle_picks_good_match(db, tmp_dirs):
    from muse.smart import shuffle as S
    good = db.upsert_track(title="Match", file_path="/tmp/match.wav")
    bad = db.upsert_track(title="Clash", file_path="/tmp/clash.wav")
    db.save_analysis(good, bpm=120.0, key="Amin", energy=0.5, tiers_done=2)
    db.save_analysis(bad, bpm=180.0, key="F#maj", energy=0.0, tiers_done=2)
    current = db.upsert_track(title="Now", file_path="/tmp/now.wav")
    db.save_analysis(current, bpm=121.0, key="Amin", energy=0.5, tiers_done=2)
    # run several times: with top-5 randomization and only 2 candidates, both can
    # be picked, but the good match must be chosen substantially more often
    wins = sum(
        1 for _ in range(40)
        if S.smart_shuffle_next(db, current, pool_size=10)["id"] == good
    )
    assert wins >= 30, f"good match won only {wins}/40"


def test_radio_sequence_no_immediate_repeats(db, tmp_dirs):
    from muse.smart import shuffle as S
    ids = []
    for i in range(6):
        tid = db.upsert_track(title=f"R{i}", file_path=f"/tmp/r{i}.wav")
        db.save_analysis(tid, bpm=100.0 + i * 3, key="Amin", energy=0.5, tiers_done=2)
        ids.append(tid)
    seq = S.radio_sequence(db, ids[0], length=6)
    got = [t["id"] for t in seq]
    assert len(got) == 6
    assert len(set(got)) == 6, "radio must not repeat within a sequence"


# -- generate queue (metadata-QoL wave 2) ----------------------------------------
def test_genre_bonus(db):
    from muse.smart.shuffle import genre_bonus
    a = {"genre": "Rock"}
    assert genre_bonus(a, {"genre": "rock"}) == genre_bonus(a, {"genre": "Rock"})
    assert genre_bonus(a, {"genre": "Pop"}) == 0.0
    assert genre_bonus(a, {"genre": None}) == 0.0
    # shared word partial credit ("Hip-Hop" vs "Hip Hop")
    assert 0.0 < genre_bonus({"genre": "Hip-Hop"}, {"genre": "Hip Hop"}) <= \
        __import__("muse.smart.shuffle", fromlist=["W_GENRE"]).W_GENRE


def test_similar_tracks_ranks_and_scores(db, tmp_dirs):
    """Generate Queue pool: same-BPM/key candidates outrank clashing ones,
    same-genre gets a bonus, low-scoring junk is excluded."""
    from muse.smart import shuffle as S

    seed = db.upsert_track(title="Seed", file_path="/tmp/seed.wav",
                           genre="Rock")
    db.save_analysis(seed, bpm=120.0, key="Amin", energy=0.5, tiers_done=2)
    good = db.upsert_track(title="Good", file_path="/tmp/good.wav",
                           genre="Rock")
    db.save_analysis(good, bpm=122.0, key="Amin", energy=0.48, tiers_done=2)
    good_nogenre = db.upsert_track(title="GoodNG", file_path="/tmp/gn.wav")
    db.save_analysis(good_nogenre, bpm=121.0, key="Amin", energy=0.5, tiers_done=2)
    bad = db.upsert_track(title="Bad", file_path="/tmp/bad.wav", genre="Pop")
    db.save_analysis(bad, bpm=175.0, key="C#maj", energy=0.05, tiers_done=2)

    picks = S.similar_tracks(db, seed, length=5)
    ids = [t["id"] for t in picks]
    assert good in ids and good_nogenre in ids
    assert bad not in ids, "clash must stay below the min-score gate"
    # genre bonus lifts the same-genre match above the equally-tempoed one
    assert ids.index(good) < ids.index(good_nogenre)
    for t in picks:
        assert t["transition_score"] >= 40.0


def test_similar_tracks_excludes_seed_and_unanalyzed(db, tmp_dirs):
    from muse.smart import shuffle as S
    seed = db.upsert_track(title="Seed", file_path="/tmp/s.wav")
    db.save_analysis(seed, bpm=120.0, key="Amin", energy=0.5, tiers_done=2)
    no_analysis = db.upsert_track(title="Raw", file_path="/tmp/raw.wav")
    picks = S.similar_tracks(db, seed, length=5)
    assert seed not in [t["id"] for t in picks]
    assert no_analysis not in [t["id"] for t in picks]


def test_automix_plan_beat_alignment(tmp_dirs):
    import os

    from muse.analysis.synth import click_track
    p = click_track(os.path.join(tmp_dirs, "a.wav"), 120.0, bars=16)
    from muse.analysis.features import analyze_file
    a = analyze_file(p)
    b = dict(a)
    plan_b = dict(b)
    plan_b["bpm"] = 120.0
    plan_b["duration"] = 30.0
    plan = __import__("muse.smart.automix", fromlist=["x"]).plan_transition(
        dict(a, duration=30.0), plan_b, transition_length=8.0)
    # a_end must sit on (or within 0.1s of) a beat-grid anchor
    grid = a["beat_grid"]
    assert min(abs(plan["a_end_sec"] - g) for g in grid) < 0.15
    assert plan["overlap_sec"] == 8.0
    # both tracks nominally 120 BPM: any tempo correction must be tiny
    assert abs(plan["tempo_scale"] - 1.0) <= 0.02
    assert plan["score_label"] in ("highly compatible", "compatible")


def test_automix_plan_tempo_adjustment():
    from muse.smart.automix import plan_transition
    a = {"bpm": 120.0, "key": "Amin", "energy": 0.5, "duration": 200.0,
         "beat_grid": [i * 0.5 for i in range(400)], "outro_sec": 0.0}
    b = {"bpm": 123.0, "key": "Amin", "energy": 0.5, "intro_sec": 1.0}
    plan = plan_transition(a, b)
    # scale S means B's decode engine runs atempo=S so B's 123 BPM lands at 120
    assert abs(plan["tempo_scale"] - 123.0 / 120.0) < 1e-3
    assert "tempo-adjust" in " ".join(plan["notes"])


def test_automix_plan_rejects_big_tempo_gap():
    from muse.smart.automix import plan_transition
    a = {"bpm": 90.0, "key": "Amin", "energy": 0.5, "duration": 200.0, "beat_grid": []}
    b = {"bpm": 150.0, "key": "D#maj", "energy": 0.9, "intro_sec": 0.0}
    plan = plan_transition(a, b)
    assert plan["tempo_scale"] == 1.0
    assert plan["score_label"] in ("marginal", "poor")


def test_format_plan_text():
    from muse.smart.automix import format_plan, plan_transition
    a = {"bpm": 120.0, "key": "Amin", "energy": 0.5, "duration": 200.0,
         "beat_grid": [i * 0.5 for i in range(400)]}
    b = {"bpm": 120.5, "key": "Emaj", "energy": 0.5, "intro_sec": 2.0}
    plan = plan_transition(a, b)
    text = format_plan("Song A", "Song B", plan)
    assert "Track A: Song A" in text
    assert "Transition Score" in text
    assert "8A" in text or "8B" in text or "3B" in text  # camelot labels present