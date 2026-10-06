"""Database layer tests: CRUD, queue integrity, playlists, settings, analysis JSON."""


def test_settings_roundtrip(db):
    db.set_setting("volume", "0.55")
    assert db.get_setting("volume") == "0.55"
    assert db.get_setting_bool("shuffle_smart") is True
    assert db.get_setting_float("volume", 0.0) == 0.55


def test_track_upsert_and_search(db):
    tid = db.upsert_track(title="Test Song", artist="Artist", album="Album",
                          duration=123.0, file_path="/tmp/x.mp3")
    assert tid > 0
    # upsert same path updates, not duplicates
    tid2 = db.upsert_track(title="Test Song 2", artist="Artist", album="Album",
                           duration=125.0, file_path="/tmp/x.mp3")
    assert tid2 == tid
    assert len(db.search_tracks("test")) == 1
    assert db.get_track(tid)["duration"] == 125.0


def test_queue_operations(db):
    ids = [db.upsert_track(title=f"t{i}", file_path=f"/tmp/q{i}.mp3") for i in range(5)]
    db.add_to_queue(ids)
    db.add_to_queue([ids[0]], at=1)  # insert at head
    positions = db.queue_positions()
    assert [p["track_id"] for p in positions] == [ids[0]] + ids
    popped = db.pop_queue_head()
    assert popped == ids[0]
    db.remove_from_queue(2)
    remaining = db.queue_positions()
    assert len(remaining) == 4
    # positions are contiguous
    assert [r["position"] for r in remaining] == list(range(1, 5))


def test_playlists(db):
    pid = db.create_playlist("Rock Classics")
    tid = db.upsert_track(title="Bohemian", artist="Queen", file_path="/tmp/q.mp3")
    db.playlist_add_track(pid, tid)
    pls = db.list_playlists()
    assert pls[0]["name"] == "Rock Classics" and pls[0]["n_tracks"] == 1
    tracks = db.playlist_tracks(pid)
    assert tracks[0]["title"] == "Bohemian"


def test_record_play_and_stats(db):
    tid = db.upsert_track(title="history", file_path="/tmp/h.mp3")
    db.record_play(tid, 0)
    db.record_play(tid, 1)
    t = db.get_track(tid)
    assert t["play_count"] == 2
    assert db.stats()["history"] == 2


def test_analysis_roundtrip(db, tmp_dirs):
    from muse.analysis.synth import click_track
    path = click_track(tmp_dirs / "c.wav", 120.0, bars=8)
    tid = db.upsert_track(title="click", file_path=path)
    from muse.analysis import worker
    assert worker.run_analysis(db, tid)
    a = db.get_analysis(tid)
    assert 112 <= a["bpm"] <= 128
    assert a["beat_count"] > 4
    assert a["tiers_done"] == 2
    # pending list no longer contains it
    assert tid not in db.pending_analysis(tiers=1, limit=10)