"""Command registry — the single source of truth for the IPC API (spec §2).

Every CLI/TUI/IPC command maps to a function here returning a JSON-able dict.
The daemon exposes these over a Unix-domain JSON-RPC socket.
"""
from __future__ import annotations

import logging
from pathlib import Path

from muse import db as dbmod
from muse import paths
from muse.analysis import worker as analysis_worker
from muse.audio import playback as audio
from muse.smart import automix as automix_mod
from muse.smart import shuffle as shuffle_mod

log = logging.getLogger("muse.daemon")

DRM_NOTICE = (
    "DRM: this track comes from a DRM-protected source "
    "(Apple Music catalogue). muse plays only DRM-free audio."
)
DOWNLOAD_NOTICE = (
    "Downloads via yt-dlp: for personal/archival use only — respect "
    "YouTube ToS and copyright (see muse.legal)."
)


class MuseCommands:
    def __init__(self, daemon=None):
        self.daemon = daemon
        self.engine: audio.Engine | None = None
        self.database: dbmod.Database | None = None

    # -- wiring ---------------------------------------------------------------
    def db(self) -> dbmod.Database:
        if self.database is None:
            self.database = dbmod.default_db()
        return self.database

    def audio_engine(self) -> audio.Engine:
        if self.engine is None:
            self.engine = audio.Engine()
            self.engine.start()
        return self.engine

    # -- playback ---------------------------------------------------------------
    def cmd_play(self, query: str | None = None, track_id: int | None = None) -> dict:
        """Play a track (by id or search query) or resume."""
        db = self.db()
        eng = self.audio_engine()
        if track_id is not None:
            t = db.get_track(track_id)
            if not t:
                return {"error": f"no track {track_id}"}
            if (t.get("provider") == "apple") or (not t.get("file_path")):
                return {"error": DRM_NOTICE}
            voice = self._make_voice(db, t)
            eng.set_voice(voice)
            eng.resume()
            db.record_play(track_id, 0)
            return {"playing": t["title"], "duration": t.get("duration")}
        if not query:
            if eng.is_playing():
                return {"playing": (db.get_track(eng.current.track_id) or {}).get("title")}
            return self.cmd_toggle()
        results = db.search_tracks(query, limit=1)
        if not results:
            return {"error": f"no local match for {query!r} (try `muse search`)"}
        return self.cmd_play(track_id=results[0]["id"])

    def _make_voice(self, db: dbmod.Database, t: dict, tempo_scale: float = 1.0) -> audio.Voice:
        ainfo = db.get_analysis(t["id"]) or {}
        lufs = ainfo.get("loudness")
        # ReplayGain-style: target -16 LUFS (typical stream loudness), cap ±9dB
        gain = 1.0
        if lufs and lufs > -60:
            delta_db = min(9.0, max(-9.0, -16.0 - lufs))
            gain = 10 ** (delta_db / 20)
        path = t["file_path"].replace("~", str(Path.home()))
        duration = t.get("duration") or ainfo.get("duration") or 0
        if not duration:
            duration = self._probe_duration(path)
        return audio.Voice(t["id"], path, duration, gain=gain, tempo=tempo_scale)

    def _probe_duration(self, path: str) -> float:
        try:
            from muse.analysis.decoder import ffprobe_duration
            return ffprobe_duration(path)
        except Exception:
            return 0.0

    def cmd_pause(self) -> dict:
        eng = self.audio_engine()
        eng.pause()
        return {"state": "paused"}

    def cmd_resume(self) -> dict:
        eng = self.audio_engine()
        eng.resume()
        return {"state": "playing"}

    def cmd_toggle(self) -> dict:
        eng = self.audio_engine()
        if eng.is_playing():
            eng.pause()
            return {"state": "paused"}
        eng.resume()
        return {"state": "playing"}

    def cmd_stop(self) -> dict:
        eng = self.audio_engine()
        eng.set_voice(None)
        eng.pause()
        return {"state": "stopped"}

    def cmd_next(self, smart: bool | None = None) -> dict:
        """Play next: engine crossfade handoff, or Smart Shuffle when queue empty."""
        db = self.db()
        eng = self.audio_engine()
        smart = db.get_setting_bool("shuffle_smart", True) if smart is None else smart
        nxt_id = db.pop_queue_head()
        if nxt_id:
            result = self.cmd_play(track_id=nxt_id)
            return result
        if smart:
            current_id = eng.current.track_id if eng.current else None
            pick = shuffle_mod.smart_shuffle_next(db, current_id)
            if pick:
                result = self.cmd_play(track_id=pick["id"])
                result["smart_shuffle"] = True
                result["score"] = pick.get("transition_score")
                return result
        return {"state": "queue empty"}

    def cmd_prev(self) -> dict:
        # prototype: history tail
        hist = self.db().stats()
        return {"state": "no previous track (history-based prev is a later milestone)",
                "history_entries": hist["history"]}

    # -- queue / library --------------------------------------------------------
    def cmd_queue(self, action: str = "list", ref: str | None = None) -> dict:
        db = self.db()
        if action == "clear":
            db.clear_queue()
            return {"queue": [], "cleared": True}
        if action in ("add", "remove") and ref:
            if action == "add":
                rows = self._resolve_tracks(db, ref)
                ids = [r["id"] for r in rows]
                db.add_to_queue(ids)
                return {"queue": self._queue_view(db), "added": len(ids)}
            pos = int(ref) if ref.isdigit() else None
            if pos is None:
                return {"error": "provide a queue position to remove"}
            removed = db.remove_from_queue(pos)
            return {"removed": removed}
        return {"queue": self._queue_view(db)}

    def _queue_view(self, db: dbmod.Database) -> list[dict]:
        items = []
        for q in db.queue_positions():
            t = db.get_track(q["track_id"])
            if t:
                t["position"] = q["position"]
                items.append(self._track_view(t, db))
        return items

    def _resolve_tracks(self, db: dbmod.Database, ref: str) -> list[dict]:
        if ref.isdigit():
            t = db.get_track(int(ref))
            return [t] if t else []
        hits = db.search_tracks(ref, limit=5)
        return hits

    def cmd_search(self, query: str, provider: str = "all") -> dict:
        from muse import providers as prov
        offline = paths.offline()
        if provider == "local":
            rows = prov.local.Provider(self.db()).search(query)
        else:
            rows = prov.search_all(query, offline=offline)
        return {"query": query, "results": rows}

    def cmd_library(self, artist: str | None = None, album: str | None = None,
                    offset: int = 0, limit: int = 100) -> dict:
        rows = self.db().list_tracks(limit=limit, artist=artist, album=album)
        rows = rows[offset:]
        return {"tracks": rows}

    def cmd_import(self, path: str) -> dict:
        p = Path(path).expanduser()
        db = self.db()
        if p.is_dir():
            ids = analysis_worker.import_directory(db, p)
            return {"imported": len(ids), "track_ids": ids}
        if p.is_file():
            tid = analysis_worker.import_file(db, p)
            return {"imported": 1, "track_ids": [tid]}
        return {"error": f"not found: {p}"}

    def cmd_analyze(self, track_id: int | None = None, all_pending: bool = False) -> dict:
        db = self.db()
        if all_pending:
            done = analysis_worker.analyze_pending(db)
            return {"analyzed": done}
        if track_id is None:
            eng = self.engine
            track_id = eng.current.track_id if eng and eng.current else None
        if track_id is None:
            return {"error": "no track specified"}
        ok = analysis_worker.run_analysis(db, track_id)
        return {"track_id": track_id, "success": ok,
                "analysis_summary": analysis_worker.analysis_summary(db, track_id)}

    # -- playlists ---------------------------------------------------------------
    def cmd_playlist(self, action: str = "list", name: str | None = None,
                     ref: str | None = None) -> dict:
        db = self.db()
        if action == "create" and name:
            pid = db.create_playlist(name)
            return {"playlist_id": pid, "name": name}
        if action == "delete" and name:
            for pl in db.list_playlists():
                if pl["name"] == name:
                    db.delete_playlist(pl["id"])
                    return {"deleted": name}
            return {"error": f"playlist not found: {name}"}
        if action == "add" and name and ref:
            for pl in db.list_playlists():
                if pl["name"] == name:
                    rows = self._resolve_tracks(db, ref)
                    added = 0
                    for r in rows:
                        db.playlist_add_track(pl["id"], r["id"])
                        added += 1
                    return {"added": added, "playlist": name}
            return {"error": f"playlist not found: {name}"}
        if action in ("list", "show") and name:
            for pl in db.list_playlists():
                if pl["name"] == name:
                    rows = db.playlist_tracks(pl["id"])
                    return {"playlist": name, "tracks": [self._track_view(r, db) for r in rows]}
            return {"error": f"playlist not found: {name}"}
        return {"playlists": db.list_playlists()}

    def cmd_like(self, ref: str | None = None, like: bool = True) -> dict:
        db = self.db()
        tid = None
        if ref and ref.isdigit():
            tid = int(ref)
        elif ref:
            rows = self._resolve_tracks(db, ref)
            if rows:
                tid = rows[0]["id"]
        else:
            eng = self.engine
            tid = eng.current.track_id if eng and eng.current else None
        if tid is None:
            return {"error": "no track"}
        db.conn.execute("UPDATE tracks SET liked = ? WHERE id = ?", (int(like), tid))
        db.conn.commit()
        return {"track_id": tid, "liked": like}

    # -- volume / status ------------------------------------------------------------
    def cmd_volume(self, value: str | None = None) -> dict:
        eng = self.audio_engine()
        if not value:
            return {"volume": round(eng.get_volume() * 100)}
        v = value.strip().lower()
        cur = eng.get_volume()
        if v in ("up", "+"):
            new = min(1.0, cur + 0.05)
        elif v in ("down", "-"):
            new = max(0.0, cur - 0.05)
        elif v == "mute":
            new = 0.0
        elif v.startswith("unmute"):
            new = self.db().get_setting_float("volume", 0.8) or 0.8
        elif v.endswith("%"):
            pct = float(v[:-1])  # CLI semantics: `volume 60` == 60%
            new = max(0.0, min(1.0, pct / 100))
        else:
            try:
                f = float(v)
            except ValueError:
                return {"error": f"bad volume: {value}"}
            new = max(0.0, min(1.0, (f / 100 if 1 < f <= 100 else f)))
        eng.set_volume(new)
        self.db().set_setting("volume", f"{new:g}")
        return {"volume": round(new * 100)}

    def cmd_status(self) -> dict:
        eng = self.engine
        db = self.db()
        if eng and eng.current:
            t = db.get_track(eng.current.track_id) or {}
            state = ("playing" if not eng.paused
                     else "paused")
            return {
                "state": state,
                "title": t.get("title"),
                "artist": t.get("artist"),
                "id": t.get("id"),
                "position": round(eng.position(), 1),
                "duration": t.get("duration"),
                "volume": round(eng.get_volume() * 100),
            }
        return {"state": "idle", "volume": round(self.db().get_setting_float("volume", 0.8) * 100),
                "library": db.stats()}

    # -- automix ---------------------------------------------------------------------
    def cmd_automix(self, action: str = "config", value: str | None = None) -> dict:
        db = self.db()
        if action in ("on", "off"):
            db.set_setting("automix_enabled", "1" if action == "on" else "0")
        if action == "length" and value:
            try:
                db.set_setting("automix_transition_length", str(max(2.0, min(16.0, float(value)))))
            except ValueError:
                return {"error": f"bad length: {value}"}
        return {
            "enabled": db.get_setting_bool("automix_enabled", True),
            "transition_length": db.get_setting_float("automix_transition_length", 8.0),
            "beat_match": True,
            "harmonic_match": True,
        }

    def cmd_automix_preview(self, ref_a: str, ref_b: str) -> dict:
        """Dry-run two-track transition plan without playing (spec §3)."""
        db = self.db()
        rows_a = self._resolve_tracks(db, ref_a)
        rows_b = self._resolve_tracks(db, ref_b)
        if not rows_a or not rows_b:
            return {"error": "both tracks required"}
        a, b = rows_a[0], rows_b[0]
        aa = analysis_worker.ensure_analysis(db, a["id"])
        bb = analysis_worker.ensure_analysis(db, b["id"])
        aa = dict(aa or {}); aa["duration"] = aa.get("duration") or a.get("duration")
        bb = dict(bb or {}); bb["duration"] = bb.get("duration") or b.get("duration")
        length = db.get_setting_float("automix_transition_length", 8.0)
        plan = automix_mod.plan_transition(aa, bb, transition_length=length)
        text = automix_mod.format_plan(a["title"], b["title"], plan)
        return {"plan": plan, "text": text,
                "track_a": self._track_view(a, db), "track_b": self._track_view(b, db)}

    # -- radio -------------------------------------------------------------------
    def cmd_radio(self, ref: str | None = None, length: int = 12, enqueue: bool = True) -> dict:
        db = self.db()
        seed = None
        if ref:
            rows = self._resolve_tracks(db, ref)
            if not rows:
                return {"error": f"no match for {ref!r}"}
            seed = rows[0]["id"]
        seq = shuffle_mod.radio_sequence(db, seed, length=length)
        if enqueue and seq:
            db.add_to_queue([t["id"] for t in seq])
        return {"radio": [self._track_view(t, db) for t in seq]}

    # -- get / lyrics / covers ----------------------------------------------------
    def cmd_get(self, url: str, on_progress=None) -> dict:
        from muse.providers import youtube as yt
        offline = paths.offline()
        if offline:
            return {"error": "downloads disabled in MUSE_OFFLINE mode"}
        dest = paths.download_dir()
        try:
            file = yt.Provider.download(url, dest, on_progress=on_progress)
        except Exception as e:
            return {"error": f"download failed: {e}"}
        if not file:
            return {"error": "download failed (see daemon log)"}
        track_id = self.register_downloaded_file(file, url)
        return {"downloaded": str(file), "track_id": track_id,
                "notice": "for personal/archival use only — respect YouTube ToS and copyright"}

    def register_downloaded_file(self, file: Path, url: str | None = None) -> int | None:
        from mutagen import File as mutagen_file
        db = self.db()
        title = file.stem
        artist = album = None
        duration = 0.0

        if file.suffix.lower() == ".mp3":
            try:
                from mutagen.id3 import ID3
                tags = ID3(str(file))
                title = tags.get("TIT2").text[0] if tags.get("TIT2") else title
                artist = tags.get("TPE1").text[0] if tags.get("TPE1") else None
                album = tags.get("TALB").text[0] if tags.get("TALB") else None
            except Exception:
                pass
        try:
            audio_info = mutagen_file(str(file), easy=True)
            if audio_info and audio_info.info:
                duration = audio_info.info.length
        except Exception:
            pass
        path_str = str(file)
        if url:
            db.conn.execute(
                "DELETE FROM tracks WHERE provider='youtube' AND url=? AND file_path IS NULL",
                (url,))
            db.conn.commit()
        return db.upsert_track(title=title, artist=artist, album=album,
                               duration=duration, file_path=path_str,
                               provider="youtube", url=url)

    def cmd_lyrics(self, ref: str | None = None) -> dict:
        db = self.db()
        tid = None
        if ref and ref.isdigit():
            tid = int(ref)
        elif ref:
            rows = self._resolve_tracks(db, ref)
            if rows:
                tid = rows[0]["id"]
        else:
            eng = self.engine
            tid = eng.current.track_id if eng and eng.current else None
        t = db.get_track(tid) if tid else None
        if not t:
            return {"error": "no track"}
        from muse.providers.media import fetch_lyrics
        res = fetch_lyrics(t.get("artist") or "", t["title"],
                           t.get("album") or "", int(t.get("duration") or 0),
                           offline=paths.offline())
        return {"lyrics": res["text"], "synced": res.get("synced", False),
                "cached": res.get("cached", False)}

    def cmd_cover(self, ref: str | None = None) -> dict:
        db = self.db()
        rows = self._resolve_tracks(db, ref) if ref else []
        if not ref and self.engine and self.engine.current:
            rows = [db.get_track(self.engine.current.track_id)]
        if not rows:
            return {"error": "no track"}
        t = rows[0]
        if t.get("cover_art_path"):
            return {"cover": t["cover_art_path"], "cached": True}
        path = None
        from muse.providers.media import fetch_cover
        path = fetch_cover(t.get("artist") or "", t.get("album") or "",
                           offline=paths.offline())
        if path:
            db.conn.execute("UPDATE albums SET cover_art_path=? WHERE id=?",
                            (path, t["album_id"]))
            db.conn.commit()
        return {"cover": path or "", "cached": False}

    # helper -----------------------------------------------------------------------
    def _probe_duration(self, path: str) -> float:
        try:
            from muse.analysis.decoder import ffprobe_duration
            return ffprobe_duration(path)
        except Exception:
            return 0.0

    def _track_view(self, t: dict, db: dbmod.Database) -> dict:
        a = db.get_analysis(t["id"]) or {}
        return {
            "id": t["id"], "title": t["title"], "artist": t.get("artist"),
            "album": t.get("album"), "duration": t.get("duration"),
            "provider": t.get("provider"), "liked": t.get("liked", 0),
            "position": t.get("position"),
            "bpm": a.get("bpm"), "key": a.get("key"),
        }


# -- command dispatch, used by CLI directly and by daemon over IPC ----------------

_SHARED: MuseCommands | None = None


def enable_shared_registry() -> MuseCommands:
    """Daemon calls this once: all IPC requests then share engine + DB state."""
    global _SHARED
    _SHARED = MuseCommands()
    return _SHARED


def handle(command: str, args: dict) -> dict:
    """Run a named command. Uses the shared registry inside the daemon;
    a fresh one per call when the CLI falls back to direct mode."""
    from muse import __version__

    if command == "version":
        return {"version": __version__}
    reg = _SHARED or MuseCommands()
    fn = getattr(reg, f"cmd_{command}", None)
    if fn is None:
        return {"error": f"unknown command: {command}"}
    try:
        return fn(**args)
    except TypeError as e:
        return {"error": f"bad arguments: {e}"}
    except Exception as e:
        log.exception("command %s failed", command)
        return {"error": str(e)}