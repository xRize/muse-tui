"""Local file provider — search over the SQLite library."""
from __future__ import annotations


class Provider:
    name = "local"

    def __init__(self, database=None):
        self._db = database

    def _database(self):
        if self._db is not None:
            return self._db
        from muse import db as dbmod
        return dbmod.default_db()

    def search(self, query: str, limit: int = 20) -> list[dict]:
        db = self._database()
        return db.search_tracks(query, limit=limit)

    def fetch_metadata(self, track_id: int) -> dict | None:
        db = self._database()
        return db.get_track(track_id)

    def get_stream(self, track_id: int) -> dict | None:
        """Return playback info: {'track': ..., 'path': ...} for the audio engine."""
        db = self._database()
        t = db.get_track(track_id)
        if not t or not t.get("file_path"):
            return None
        return {"track": t, "path": t["file_path"]}