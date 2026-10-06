"""SQLite persistence layer (spec §2 database schema)."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artists (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS albums (
  id INTEGER PRIMARY KEY,
  artist_id INTEGER REFERENCES artists(id),
  name TEXT NOT NULL,
  cover_art_path TEXT,
  UNIQUE(artist_id, name)
);
CREATE TABLE IF NOT EXISTS tracks (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  artist_id INTEGER REFERENCES artists(id),
  album_id INTEGER REFERENCES albums(id),
  duration REAL,
  file_path TEXT,
  provider TEXT NOT NULL DEFAULT 'local',
  url TEXT,
  year INTEGER,
  track_number INTEGER,
  play_count INTEGER NOT NULL DEFAULT 0,
  last_played REAL,
  liked INTEGER NOT NULL DEFAULT 0,
  UNIQUE(file_path)
);
CREATE TABLE IF NOT EXISTS playlists (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, is_smart INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
  playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
  track_id INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  PRIMARY KEY (playlist_id, position)
);
CREATE TABLE IF NOT EXISTS queue (
  position INTEGER PRIMARY KEY,
  track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  enqueued_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
  play_time REAL NOT NULL,
  track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis (
  track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
  bpm REAL, key TEXT, loudness REAL, energy REAL,
  intro_sec REAL, outro_sec REAL, beat_count INTEGER,
  beat_grid TEXT, structure TEXT, tiers_done INTEGER NOT NULL DEFAULT 0,
  completed_at REAL
);
CREATE TABLE IF NOT EXISTS providers (
  id INTEGER PRIMARY KEY, type TEXT NOT NULL, metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks(title);
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist_id);
CREATE INDEX IF NOT EXISTS idx_history_time ON history(play_time);
"""

DEFAULTS = {
    "volume": "0.8",
    "automix_enabled": "1",
    "automix_transition_length": "8",
    "shuffle_smart": "1",
}

# camelot key values for harmonic matching (see smart/keys.py)
# stored as "8A"/"5d" style strings in analysis.key


class Database:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.execute("PRAGMA foreign_keys = ON")
        for k, v in DEFAULTS.items():
            self.conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        self.conn.commit()

    # -- settings -----------------------------------------------------------
    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def get_setting_bool(self, key: str, default: bool = False) -> bool:
        v = self.get_setting(key)
        return default if v is None else v in ("1", "true", "yes", "on")

    def get_setting_float(self, key: str, default: float = 0.0) -> float:
        v = self.get_setting(key)
        try:
            return default if v is None else float(v)
        except ValueError:
            return default

    # -- artists / albums / tracks -----------------------------------------
    def get_or_create_artist(self, name: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM artists WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return row["id"]
        cur = self.conn.execute("INSERT INTO artists(name) VALUES (?)", (name,))
        self.conn.commit()
        return cur.lastrowid

    def get_or_create_album(self, name: str, artist_id: int | None) -> int:
        row = self.conn.execute(
            "SELECT id FROM albums WHERE name = ? AND (artist_id IS ? OR artist_id IS NULL)",
            (name, artist_id),
        ).fetchone()
        if row:
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO albums(name, artist_id) VALUES (?, ?)", (name, artist_id)
        )
        self.conn.commit()
        return cur.lastrowid

    def upsert_track(
        self,
        title: str,
        artist: str | None = None,
        album: str | None = None,
        duration: float | None = None,
        file_path: str | None = None,
        provider: str = "local",
        url: str | None = None,
        year: int | None = None,
        track_number: int | None = None,
        cover_art_path: str | None = None,
    ) -> int:
        """Insert or update by file_path (local) or (provider,url). Returns track id."""
        artist_id = self.get_or_create_artist(artist) if artist else None
        album_id = self.get_or_create_album(album, artist_id) if album else None
        existing = None
        if file_path:
            existing = self.conn.execute(
                "SELECT id FROM tracks WHERE file_path = ?", (file_path,)
            ).fetchone()
        elif url:
            existing = self.conn.execute(
                "SELECT id FROM tracks WHERE provider = ? AND url = ?", (provider, url)
            ).fetchone()
        if existing:
            self.conn.execute(
                """UPDATE tracks SET title=?, artist_id=COALESCE(?, artist_id),
                   album_id=COALESCE(?, album_id), duration=COALESCE(?, duration),
                   year=COALESCE(?, year), track_number=COALESCE(?, track_number)
                   WHERE id = ?""",
                (title, artist_id, album_id, duration, year, track_number, existing["id"]),
            )
            track_id = existing["id"]
        else:
            cur = self.conn.execute(
                """INSERT INTO tracks
                   (title, artist_id, album_id, duration, file_path, provider, url,
                    year, track_number)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (title, artist_id, album_id, duration, file_path, provider, url,
                 year, track_number),
            )
            track_id = cur.lastrowid
        if cover_art_path and album_id:
            self.conn.execute(
                "UPDATE albums SET cover_art_path = COALESCE(cover_art_path, ?) WHERE id = ?",
                (cover_art_path, album_id),
            )
        self.conn.commit()
        return track_id

    def get_track(self, track_id: int) -> dict | None:
        row = self.conn.execute(_TRACK_QUERY + " WHERE t.id = ?", (track_id,)).fetchone()
        return dict(row) if row else None

    def search_tracks(self, query: str, limit: int = 20) -> list[dict]:
        like = f"%{query.lower()}%"
        rows = self.conn.execute(
            _TRACK_QUERY
            + " WHERE (t.title LIKE ? OR a.name LIKE ? "
            "   OR (al.name IS NOT NULL AND al.name LIKE ?)) "
            " ORDER BY t.play_count DESC LIMIT ?",
            (like, like, like, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_tracks(self, limit: int = 500, artist: str | None = None,
                    album: str | None = None) -> list[dict]:
        rows = self.conn.execute(
            _TRACK_QUERY + " ORDER BY a.name, al.name, t.track_number, t.title LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_track(self, track_id: int) -> None:
        self.conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        self.conn.commit()

    def all_track_ids(self, provider: str | None = None) -> list[int]:
        if provider:
            rows = self.conn.execute(
                "SELECT id FROM tracks WHERE provider = ?", (provider,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT id FROM tracks").fetchall()
        return [r["id"] for r in rows]

    # -- analysis -----------------------------------------------------------
    def has_analysis(self, track_id: int, tiers: int) -> bool:
        row = self.conn.execute(
            "SELECT tiers_done FROM analysis WHERE track_id = ?", (track_id,)
        ).fetchone()
        return bool(row) and row["tiers_done"] >= tiers

    def save_analysis(self, track_id: int, **fields) -> None:
        allowed = {
            "bpm", "key", "loudness", "energy", "intro_sec", "outro_sec",
            "beat_count", "beat_grid", "structure", "tiers_done", "completed_at",
        }
        cols, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if hasattr(v, "item"):          # numpy scalars -> python (BLOB otherwise)
                try:
                    v = v.item()
                except (AttributeError, ValueError):
                    pass
            if k in ("beat_grid", "structure") and isinstance(v, (list, tuple, dict)):
                v = json.dumps(v)
            cols.append(k)
            vals.append(v)
        cols.append("completed_at")
        vals.append(time.time())
        cols.insert(0, "track_id")
        vals.insert(0, track_id)
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols)
        self.conn.execute(
            f"INSERT INTO analysis ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(track_id) DO UPDATE SET {updates}",
            vals,
        )
        self.conn.commit()

    def get_analysis(self, track_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM analysis WHERE track_id = ?", (track_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for jf in ("beat_grid", "structure"):  # stored as JSON text
            if d.get(jf) and isinstance(d[jf], str):
                try:
                    d[jf] = json.loads(d[jf])
                except (json.JSONDecodeError, ValueError):
                    d[jf] = None
        return d

    def pending_analysis(self, tiers: int = 1, limit: int = 50) -> list[int]:
        rows = self.conn.execute(
            """SELECT t.id FROM tracks t
               LEFT JOIN analysis a ON a.track_id = t.id
               WHERE a.track_id IS NULL OR a.tiers_done < ?
               ORDER BY t.id LIMIT ?""",
            (tiers, limit),
        ).fetchall()
        return [r["id"] for r in rows]

    # -- queue ----------------------------------------------------------------
    def queue_positions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT position, track_id, enqueued_at FROM queue ORDER BY position"
        ).fetchall()
        return [dict(r) for r in rows]

    def clear_queue(self) -> None:
        self.conn.execute("DELETE FROM queue")
        self.conn.commit()

    def add_to_queue(self, track_ids: list[int], at: int | None = None) -> None:
        now = time.time()
        tail = self.queue_positions()
        if at is None:
            at = tail[-1]["position"] + 1 if tail else 1
        n = len(track_ids)
        if at <= (tail[-1]["position"] if tail else 0):
            # make room via unused negative space (SQLite checks UNIQUE per row,
            # so a direct in-place shift would transiently collide)
            self.conn.execute(
                "UPDATE queue SET position = -position WHERE position >= ?", (at,)
            )
            self.conn.execute(
                "UPDATE queue SET position = -position + ? WHERE position < 0", (n,)
            )
        rows = [(at + i, tid, now) for i, tid in enumerate(track_ids)]
        self.conn.executemany(
            "INSERT INTO queue(position, track_id, enqueued_at) VALUES (?, ?, ?)", rows
        )
        self.conn.commit()

    def pop_queue_head(self) -> int | None:
        row = self.conn.execute(
            "SELECT position, track_id FROM queue ORDER BY position LIMIT 1"
        ).fetchone()
        if not row:
            return None
        self.conn.execute("DELETE FROM queue WHERE position = ?", (row["position"],))
        self.conn.execute(
            "UPDATE queue SET position = position - 1 WHERE position > ?",
            (row["position"],),
        )
        self.conn.commit()
        return row["track_id"]

    def remove_from_queue(self, position: int) -> int | None:
        row = self.conn.execute(
            "SELECT track_id FROM queue WHERE position = ?", (position,)
        ).fetchone()
        if not row:
            return None
        self.conn.execute("DELETE FROM queue WHERE position = ?", (position,))
        self.conn.execute(
            "UPDATE queue SET position = position - 1 WHERE position > ?",
            (position,),
        )
        self.conn.commit()
        return row["track_id"]

    def record_play(self, track_id: int, position: int) -> None:
        self.conn.execute(
            "UPDATE tracks SET play_count = play_count + 1, last_played = ? WHERE id = ?",
            (time.time(), track_id),
        )
        self.conn.execute(
            "INSERT INTO history(play_time, track_id, position) VALUES (?, ?, ?)",
            (time.time(), track_id, position),
        )
        self.conn.commit()

    # -- playlists -------------------------------------------------------------
    def create_playlist(self, name: str, smart: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO playlists(name, is_smart) VALUES (?, ?)", (name, int(smart))
        )
        self.conn.commit()
        return cur.lastrowid

    def playlist_add_track(self, playlist_id: int, track_id: int) -> None:
        next_pos = self.conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS p FROM playlist_tracks "
            "WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()["p"]
        self.conn.execute(
            "INSERT INTO playlist_tracks(playlist_id, track_id, position) VALUES (?, ?, ?)",
            (playlist_id, track_id, next_pos),
        )
        self.conn.commit()

    def list_playlists(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT p.id, p.name, p.is_smart, COUNT(pt.track_id) AS n_tracks "
            "FROM playlists p LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id "
            "GROUP BY p.id ORDER BY p.name"
        ).fetchall()
        return [dict(r) for r in rows]

    def playlist_tracks(self, playlist_id: int) -> list[dict]:
        rows = self.conn.execute(
            _TRACK_QUERY
            + " JOIN playlist_tracks pt ON pt.track_id = t.id "
            " WHERE pt.playlist_id = ? ORDER BY pt.position",
            (playlist_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_playlist(self, playlist_id: int) -> None:
        self.conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        self.conn.commit()

    # -- stats / misc ------------------------------------------------------------
    def stats(self) -> dict:
        n = self.conn.execute("SELECT COUNT(*) AS n FROM tracks").fetchone()["n"]
        q = self.conn.execute("SELECT COUNT(*) AS n FROM queue").fetchone()["n"]
        h = self.conn.execute("SELECT COUNT(*) AS n FROM history").fetchone()["n"]
        an = self.conn.execute(
            "SELECT COUNT(*) AS n FROM analysis WHERE tiers_done >= 1"
        ).fetchone()["n"]
        return {"tracks": n, "queue": q, "history": h, "analyzed": an}

    def close(self) -> None:
        self.conn.close()


_TRACK_QUERY = """
SELECT t.id, t.title, a.name AS artist, al.name AS album, t.duration, t.file_path,
       t.provider, t.url, t.year, t.track_number, t.play_count, t.last_played,
       t.liked, al.cover_art_path
FROM tracks t
LEFT JOIN artists a ON a.id = t.artist_id
LEFT JOIN albums al ON al.id = t.album_id
"""


def default_db() -> Database:
    # allow tests to isolate via MUSE_TEST_DB
    path = os.environ.get("MUSE_TEST_DB")
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return Database(path)
    from muse import paths
    return Database(paths.db_path())