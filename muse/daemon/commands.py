"""Command registry — the single source of truth for the IPC API (spec §2).

Every CLI/TUI/IPC command maps to a function here returning a JSON-able dict.
The daemon exposes these over a Unix-domain JSON-RPC socket.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from muse import db as dbmod
from muse import paths
from muse.analysis import worker as analysis_worker
from muse.audio import playback as audio
from muse.providers import media
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

# YouTube download jobs: keyed by job id, shared by every registry in this
# process (the daemon's shared registry + each direct-mode fallback registry
# the CLI/TUI creates), so poll/download cycles stay consistent.
_DL_JOBS: dict[int, dict] = {}
_DL_LOCK = threading.Lock()
_DL_NEXT = [0]


class MuseCommands:
    def __init__(self, daemon=None):
        self.daemon = daemon
        self.engine: audio.Engine | None = None
        self.database: dbmod.Database | None = None
        self.daemon_stop_signal: threading.Event | None = None

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
            self._media_now_playing(t)
            return {"playing": t["title"], "duration": t.get("duration")}
        if not query:
            if eng.is_playing():
                return {"playing": (db.get_track(eng.current.track_id) or {}).get("title")}
            return self.cmd_toggle()
        results = db.search_tracks(query, limit=1)
        if not results:
            return {"error": f"no local match for {query!r} (try `muse search`)"}
        return self.cmd_play(track_id=results[0]["id"])

    def _make_voice(self, db: dbmod.Database, t: dict,
                    tempo_scale: float = 1.0, seek: float = 0.0) -> audio.Voice:
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
        voice = audio.Voice(t["id"], path, duration, gain=gain,
                            tempo=tempo_scale, seek=seek)
        voice.tempo = tempo_scale  # keeps working if seek rebuilds the decoder
        return voice

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
        """Play next: live crossfade when AutoMix is on, else hard switch;
        Smart Shuffle pick when the queue is empty."""
        db = self.db()
        eng = self.audio_engine()
        smart = db.get_setting_bool("shuffle_smart", True) if smart is None else smart
        nxt_id = db.pop_queue_head()
        if not nxt_id and smart:
            current_id = eng.current.track_id if eng.current else None
            pick = shuffle_mod.smart_shuffle_next(db, current_id)
            if pick:
                nxt_id = pick["id"]
        if not nxt_id:
            return {"state": "queue empty"}
        if eng.current and not eng.current.done and \
                db.get_setting_bool("automix_enabled", True):
            plan = self._plan_to_track(eng.current.track_id, nxt_id)
            overlap = db.get_setting_float("automix_transition_length", 8.0)
            if plan and plan.get("score", 0) >= 45:
                out = self._live_crossfade(nxt_id, plan["overlap_sec"],
                                           tempo=plan["tempo_scale"],
                                           band=db.get_setting_bool("automix_bandpass", True),
                                           seek=plan.get("b_start_sec") or 0.0)
                out["plan_score"] = plan.get("score")
                return out
            remaining = (eng.current.duration or 0) - eng.position()
            if remaining > 1.0:
                out = self._live_crossfade(nxt_id, min(overlap, remaining))
                return out
        return self.cmd_play(track_id=nxt_id)

    def _live_crossfade(self, nxt_id: int, fade_seconds: float, tempo: float = 1.0,
                        band: bool = False, seek: float = 0.0) -> dict:
        db = self.db()
        nxt = db.get_track(nxt_id)
        if not nxt:
            return {"error": f"no track {nxt_id}"}
        voice = self._make_voice(db, nxt, tempo_scale=tempo, seek=seek)
        eng = self.audio_engine()
        eng.begin_crossfade(voice, fade_seconds, band_limited=band)
        eng.resume()
        db.record_play(nxt_id, 0)
        self._media_now_playing(nxt)
        return {"playing": nxt["title"], "duration": nxt.get("duration"),
                "crossfade": round(float(fade_seconds), 2)}

    def cmd_prev(self) -> dict:
        """Real previous: restart the current track; before 3s, return to the
        last distinct play (history-based, spec §4)."""
        eng = self.audio_engine()
        db = self.db()
        pos = eng.position() if eng.current else 0.0
        if eng.current and pos < 3.0:
            eng.stop()
            tid = db.pop_last_played(exclude_id=eng.current.track_id)
            if tid is None:
                return {"state": "no previous track"}
            t = db.get_track(tid)
            if not t:
                return {"state": "no previous track"}
            return self.cmd_play(track_id=tid)
        eng.seek(0.0)
        return {"state": "restarted", "position": 0.0}

    def cmd_seek(self, seconds: float, relative: bool = False) -> dict:
        """Seek the current track (spec §4); `relative` adds to the position."""
        eng = self.audio_engine()
        if eng.current is None:
            return {"error": "nothing playing"}
        try:
            s = float(seconds)
        except (TypeError, ValueError):
            return {"error": f"bad seek value: {seconds}"}
        target = eng.position() + s if relative else s
        pos = eng.seek(target)
        return {"position": round(pos, 1), "duration": eng.current.duration}

    def cmd_history(self, limit: int = 20) -> dict:
        return {"history": [self._track_view(t, self.db())
                            for t in self.db().history_tracks(limit=limit)]}

    def cmd_stats(self) -> dict:
        db = self.db()
        top = db.conn.execute(
            "SELECT t.id, t.title, a.name AS artist, t.play_count "
            "FROM tracks t LEFT JOIN artists a ON a.id = t.artist_id "
            "ORDER BY t.play_count DESC, t.id LIMIT 10").fetchall()
        return {"library": db.stats(),
                "top": [dict(r) for r in top]}

    # -- queue / library --------------------------------------------------------
    def cmd_queue(self, action: str = "list", ref: str | None = None,
                  keep_first: bool = False) -> dict:
        db = self.db()
        if action == "clear":
            db.clear_queue()
            return {"queue": [], "cleared": True}
        if action == "shuffle":
            n = db.shuffle_queue(keep_first=keep_first)
            return {"queue": self._queue_view(db), "shuffled": n}
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
                    offset: int = 0, limit: int = 100, kind: str | None = None) -> dict:
        db = self.db()
        if kind == "artists":
            return {"artists": db.list_artists()}
        if kind == "albums":
            return {"albums": db.list_albums(artist=artist)}
        rows = db.list_tracks(limit=limit, artist=artist, album=album)
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

    # -- YouTube download interface (search -> queue -> poll -> library) ----------
    # Jobs live at module level so direct-mode handle() calls (a fresh registry
    # per request in CLI/TUI fallback) still see one job list across the
    # search/get/downloads cycle; inside the daemon the shared registry uses
    # the same store.

    def _dl_job(self, label: str, kind: str) -> int:
        with _DL_LOCK:
            _DL_NEXT[0] += 1
            jid = _DL_NEXT[0]
            _DL_JOBS[jid] = {"id": jid, "label": label, "kind": kind,
                             "state": "running", "progress": "", "error": None,
                             "items": [], "track_ids": []}
            return jid

    @staticmethod
    def _dl_update(jid: int, **fields) -> None:
        with _DL_LOCK:
            job = _DL_JOBS.get(jid)
            if job:
                job.update(fields)

    def cmd_yt_search(self, query: str, limit: int = 10) -> dict:
        """Search YouTube via yt-dlp --flat-playlist (offline: empty results)."""
        from muse import providers as prov
        offline = paths.offline()
        results = [] if offline else prov.youtube.Provider().search(query, limit)
        return {"query": query, "offline": offline, "results": results}

    def cmd_ytsearch(self, query: str, limit: int = 5) -> dict:
        """Search-download: top YouTube hits for a song query as candidates.

        Returns the ranked list (songs before videos): the caller passes one
        to `yt_get`/`get` to actually download it. No library changes here."""
        r = self.cmd_yt_search(query, limit=limit)
        return {"query": query, "offline": r["offline"],
                "candidates": r["results"]}

    def cmd_yt_available(self) -> dict:
        """yt-dlp presence probe (TUI uses it for 'install yt-dlp' hints)."""
        from muse.providers import youtube as yt
        return {"available": yt.available()}

    def cmd_yt_get(self, ref: str, playlist: bool = False, workers: int = 4) -> dict:
        """Queue a YouTube URL / id for a background audio download (yt-dlp).

        Accepts full URLs, 'yt:<video-id>', 'ytpl:<playlist-id>' (or any
        /playlist URL). Returns {'job': id, ...} — poll via `downloads`;
        completed items are auto-imported into the library.
        """
        from muse.providers import youtube as yt
        if paths.offline():
            return {"error": "downloads disabled in MUSE_OFFLINE mode"}
        if not yt.available():
            return {"error": "yt-dlp not installed (pip install yt-dlp)"}
        raw = ref.strip()
        if raw.startswith(("search:", "ytsearch:")):
            # convenience: resolve the query inline (top hit) then download
            hits = self.cmd_ytsearch(raw.split(":", 1)[1], limit=5)["candidates"]
            if not hits:
                return {"error": f"no YouTube results for {raw.split(':', 1)[1]!r}"}
            raw = hits[0].get("url") or ""
            if not raw:
                return {"error": "top YouTube hit has no downloadable URL"}
        if raw.startswith("yt:"):
            url = "https://www.youtube.com/watch?v=" + raw[3:].strip()
        elif raw.startswith("ytpl:"):
            url = "https://www.youtube.com/playlist?list=" + raw[5:].strip()
        elif raw.startswith(("http://", "https://")):
            url = raw
        else:
            url = "https://" + raw
        is_playlist = playlist or "/playlist" in url
        from muse.providers.youtube import clean_meta, split_title
        if is_playlist:
            label = url
        else:
            artist, title = split_title(raw[3:].replace("+", " ")) \
                if raw.startswith("yt:") else ("", "")
            label = f"{artist} — {title}" if artist and title else clean_meta(
                (raw[3:].replace("+", " ") if raw.startswith("yt:") else url))
        jid = self._dl_job(label, "playlist" if is_playlist else "single")
        threading.Thread(target=self._dl_run, args=(jid, url, is_playlist, workers),
                         name=f"muse-dl-{jid}", daemon=True).start()
        return {"job": jid, "url": url, "kind": _DL_JOBS[jid]["kind"],
                "label": label, "notice": DOWNLOAD_NOTICE}

    def _dl_run(self, jid: int, url: str, playlist: bool, workers: int) -> None:
        """Background worker: yt-dlp -> register files in the library."""
        from muse.providers import youtube as yt
        dest = paths.download_dir()

        def prog(line: str) -> None:
            self._dl_update(jid, progress=line[-110:])

        try:
            if playlist:
                files, rc, tail = yt.Provider.download_playlist(
                    url, dest, workers=workers, on_progress=prog)
            else:
                file = yt.Provider.download(url, dest, on_progress=prog)
                files = [file] if file else []
                rc, tail = 0, ""
            track_ids = []
            for file in files:
                tid = self.register_downloaded_file(file, url=url)
                if tid:
                    track_ids.append(tid)
            self._dl_update(
                jid, state="done" if track_ids else "failed",
                track_ids=track_ids,
                progress=f"{len(track_ids)} track(s) imported",
                error=None if track_ids else
                (f"yt-dlp exit {rc}: {tail}" if tail else
                 "yt-dlp produced no audio files (see daemon log)"))
            if track_ids:
                threading.Thread(target=self._prewarm_extras,
                                 args=(track_ids,), name=f"muse-prewarm-{jid}",
                                 daemon=True).start()
        except Exception as e:
            log.exception("download job %s failed", jid)
            self._dl_update(jid, state="failed", error=str(e))

    def _prewarm_extras(self, track_ids: list[int]) -> None:
        """Fetch lyrics + cover art for fresh imports (best-effort, offline-safe)."""
        db = self.db()
        offline = paths.offline()
        for tid in track_ids:
            t = db.get_track(tid)
            if not t:
                continue
            try:
                if not offline:
                    media.fetch_lyrics(t.get("artist") or "", t["title"],
                                       t.get("album") or "",
                                       int(t.get("duration") or 0),
                                       offline=offline)
                if not offline and not t.get("cover_art_path"):
                    self.cmd_cover(str(tid))
            except Exception as e:
                log.debug("prewarm extras failed for track %s: %s", tid, e)

    def cmd_downloads(self) -> dict:
        """Queue/job overview for running + completed downloads."""
        with _DL_LOCK:
            jobs = [dict(j) for j in _DL_JOBS.values()]
        jobs.sort(key=lambda j: j["id"], reverse=True)
        return {"jobs": jobs}

    def cmd_service_tick(self) -> dict:
        """Manual tick (TUI direct mode has no daemon loop; harmless in-daemon)."""
        return {"ticked": self.service_tick()}

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
        if action == "play" and name:
            for pl in db.list_playlists():
                if pl["name"] == name:
                    rows = db.playlist_tracks(pl["id"])
                    if not rows:
                        return {"error": f"playlist empty: {name}"}
                    db.clear_queue()
                    db.add_to_queue([r["id"] for r in rows[1:]])
                    result = self.cmd_play(track_id=rows[0]["id"])
                    result["playlist"] = name
                    result["queued"] = len(rows) - 1
                    return result
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

    # -- discover (smart-playlist recs) ------------------------------------------
    def cmd_discover(self, seed: str | None = None, length: int = 12,
                     enqueue: bool = False, variety: float = 0.5) -> dict:
        """Spec §4 `discover`: recommendation shuffle across the library.
        Like radio, but biased for exploration (no seed required, no enqueues
        by default, wider randomization among good matches via top-k)."""
        db = self.db()
        eng = self.engine
        seed_id = None
        if seed:
            rows = self._resolve_tracks(db, seed)
            if not rows:
                return {"error": f"no match for {seed!r}"}
            seed_id = rows[0]["id"]
        elif eng and eng.current:
            seed_id = eng.current.track_id
        top_k = max(3, min(15, round(10 * variety)))
        picked: list[dict] = []
        exclude: set[int] = set()
        cur = seed_id
        for _ in range(length):
            pick = shuffle_mod.smart_shuffle_next(db, cur, exclude_ids=exclude,
                                                  top_k=top_k)
            if not pick:
                break
            picked.append(pick)
            exclude.add(pick["id"])
            cur = pick["id"]
        if enqueue and picked:
            db.add_to_queue([t["id"] for t in picked])
        return {"discover": [self._track_view(t, db) for t in picked]}

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
        onoff = (str(value).lower() in ("1", "on", "true", "yes")
                 if value is not None else None)
        if action in ("on", "off"):
            db.set_setting("automix_enabled", "1" if action == "on" else "0")
        elif action == "length" and value:
            try:
                db.set_setting("automix_transition_length", str(max(2.0, min(16.0, float(value)))))
            except ValueError:
                return {"error": f"bad length: {value}"}
        elif action in ("bandpass", "band"):
            if onoff is None:
                return {"error": "usage: automix bandpass on|off"}
            db.set_setting("automix_bandpass", "1" if onoff else "0")
        elif action == "vocal":
            if onoff is None:
                return {"error": "usage: automix vocal on|off"}
            db.set_setting("automix_vocal_mode", "1" if onoff else "0")
        return {
            "enabled": db.get_setting_bool("automix_enabled", True),
            "transition_length": db.get_setting_float("automix_transition_length", 8.0),
            "beat_match": db.get_setting_bool("automix_beat_match", True),
            "harmonic_match": db.get_setting_bool("automix_harmonic_match", True),
            "bandpass": db.get_setting_bool("automix_bandpass", True),
            "vocal_mode": db.get_setting_bool("automix_vocal_mode", False),
        }

    def cmd_daemon_stop(self) -> dict:
        """Stop playback and signal the daemon process to exit (spec: `stop`)."""
        ev = self.daemon_stop_signal or _exit_event()
        ev.set()
        return {"state": "stopped", "daemon_exiting": bool(ev.is_set())}

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
        plan = automix_mod.plan_transition(
            aa, bb, transition_length=length,
            beat_match=db.get_setting_bool("automix_beat_match", True),
            harmonic_match=db.get_setting_bool("automix_harmonic_match", True),
            vocal_mode=db.get_setting_bool("automix_vocal_mode", False))
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

    def register_downloaded_file(self, file: Path, url: str | None = None,
                                 thumb_url: str | None = None) -> int | None:
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
                # yt-dlp embeds the raw (pre-replace) title in TIT2 as a
                # fallback tag; normalize it with the same cleaner so the
                # final rename below sees the noise-free form
                from muse.providers.youtube import clean_meta
                if title:
                    title = clean_meta(title)
                if artist:
                    artist = clean_meta(artist).strip() or None
            except Exception:
                pass
        try:
            audio_info = mutagen_file(str(file), easy=True)
            if audio_info and audio_info.info:
                duration = audio_info.info.length
        except Exception:
            pass
        # uploaded as "Artist - Song ..."-style filename without tags? split it
        if not artist:
            from muse.providers.youtube import split_title
            artist, title2 = split_title(title)
            if artist:
                title = title2
        # yt-dlp writes the full "Artist - Song" (noise-stripped) into TIT2
        # while TPE1 carries the artist alone: strip the redundant prefix so
        # titles/files don't become "Artist - Artist - Song"
        if artist and title and title.lower().startswith(
                f"{artist.lower()} - "):
            title = title[len(artist) + 3:].strip()
        # final filesystem name: "Artist - Song" (clean per muse naming);
        # sidecar jpg (yt-dlp thumbnail) travels with the rename
        if artist and title and file.stem != f"{artist} - {title}":
            target = file.with_name(f"{artist} - {title}{file.suffix}")
            try:
                file.rename(target)
                sidecar_src = file.with_suffix(".jpg")
                if sidecar_src.is_file():
                    sidecar_src.rename(target.with_suffix(".jpg"))
                file = target
            except OSError:
                log.warning("could not rename %s to %s", file, target)
        cover = None
        sidecar = file.with_suffix(".jpg")
        if sidecar.is_file():
            cover = str(sidecar)
        path_str = str(file)
        if url:
            db.conn.execute(
                "DELETE FROM tracks WHERE provider='youtube' AND url=? AND file_path IS NULL",
                (url,))
            db.conn.commit()
        return db.upsert_track(title=title, artist=artist, album=album,
                               duration=duration, file_path=path_str,
                               provider="youtube", url=url,
                               cover_art_path=cover)

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
        # a YouTube-downloaded track's best art is its own video thumbnail
        thumb = t.get("url") or ""
        thumb_url = (f"https://i.ytimg.com/vi/{thumb.split('watch?v=', 1)[1]}"
                     "/hqdefault.jpg") if "watch?v=" in thumb else None
        path = media.fetch_cover(t.get("artist") or "", t.get("album") or "",
                                 offline=paths.offline(), thumb_url=thumb_url)
        if path:
            db.conn.execute("UPDATE tracks SET cover_art_path=COALESCE(?, cover_art_path) WHERE id=?",
                            (path, t["id"]))
            db.conn.commit()
            return {"cover": path, "cached": False}
        return {"cover": "", "cached": False}

    # helper -----------------------------------------------------------------------
    def _probe_duration(self, path: str) -> float:
        try:
            from muse.analysis.decoder import ffprobe_duration
            return ffprobe_duration(path)
        except Exception:
            return 0.0

    # -- daemon tick: auto-advance + live AutoMix ---------------------------------
    def service_tick(self) -> dict | None:
        """Called ~every 0.5s by the daemon's main loop (and safe for tests).

        Responsibilities (spec §1 'gapless', §3 AutoMix):
        - when the current voice has finished with no crossfade, advance to the
          queue head (or Smart Shuffle) — no dead air;
        - when the remaining time reaches the transition length and AutoMix is
          on, schedule the crossfade with the planned tempo scale / anchors.
        Returns a small dict describing what happened (for tests/logs), else None.
        """
        eng = self.engine
        if eng is None or self.database is None or eng.paused:
            return None
        if eng.needs_advance():
            return self._advance_after_end()
        cur = eng.current
        if not cur or cur.done or not self.db().get_setting_bool("automix_enabled", True):
            return None
        duration = cur.duration or 0
        if duration <= 0:
            return None
        remaining = duration - cur.position_seconds
        overlap = self.db().get_setting_float("automix_transition_length", 8.0)
        if remaining > overlap + 0.05 or remaining <= 0:
            return None
        if getattr(eng, "upcoming", None):  # already fading
            return None
        nxt_id = self.db().peek_queue_head()
        if not nxt_id:
            return None
        plan = self._plan_to_track(cur.track_id, nxt_id)
        if not plan:
            return None
        if plan.get("score", 0) < 45:
            return None
        # honor the planned beat anchor: wait until A reaches a_end_sec, then
        # fade for (duration - a_end). The earlier "remaining <= overlap" gate
        # only arms the planner once we're inside the transition zone.
        a_end = plan.get("a_end_sec")
        if a_end is None:
            a_end = duration
        if cur.position_seconds < a_end - 0.05:
            return None
        nxt_id = self.db().pop_queue_head()
        nxt = self.db().get_track(nxt_id)
        if not nxt or not nxt.get("file_path"):
            # leave a missing/broken entry for the plain-advance path to skip;
            # the automix handoff just won't happen this tick
            return None
        voice = self._make_voice(self.db(), nxt, tempo_scale=plan["tempo_scale"],
                                 seek=plan.get("b_start_sec") if
                                 plan.get("b_start_sec") is not None else 0.0)
        band = self.db().get_setting_bool("automix_bandpass", True)
        eng.begin_crossfade(voice, plan["overlap_sec"], band_limited=band)
        self.db().record_play(nxt_id, 0)
        self._media_now_playing(nxt)
        log.info("automix: track %s -> %s (score %s, overlap %ss)",
                 cur.track_id, nxt_id, plan.get("score"), plan.get("overlap_sec"))
        return {"automix": True, "from": cur.track_id, "to": nxt_id,
                "overlap": plan["overlap_sec"], "score": plan.get("score")}

    def _plan_to_track(self, from_id: int, to_id: int) -> dict | None:
        try:
            aa = analysis_worker.ensure_analysis(self.db(), from_id)
            bb = analysis_worker.ensure_analysis(self.db(), to_id)
            if not aa or not bb:
                return None
            db = self.db()
            cur_t = db.get_track(from_id) or {}
            nxt_t = db.get_track(to_id) or {}
            aa = dict(aa); bb = dict(bb)
            aa["duration"] = aa.get("duration") or cur_t.get("duration")
            bb["duration"] = bb.get("duration") or nxt_t.get("duration")
            length = db.get_setting_float("automix_transition_length", 8.0)
            return automix_mod.plan_transition(
                aa, bb, transition_length=length,
                beat_match=db.get_setting_bool("automix_beat_match", True),
                harmonic_match=db.get_setting_bool("automix_harmonic_match", True),
                vocal_mode=db.get_setting_bool("automix_vocal_mode", False))
        except Exception:
            log.exception("automix planning failed")
            return None

    def _advance_after_end(self) -> dict | None:
        """Track ended with no crossfade: pop the queue (or Smart Shuffle)."""
        eng, db = self.engine, self.db()

        def start(tid: int, smart: bool = False) -> dict | None:
            t = db.get_track(tid)
            if not t or not t.get("file_path"):
                return None
            path = t["file_path"].replace("~", str(Path.home()))
            if not Path(path).is_file():
                return None
            duration = t.get("duration") or 0
            if not duration:
                duration = self._probe_duration(path)
            db.record_play(tid, 0)
            eng.set_voice_at(tid, path, duration, self._gain_for(t))
            self._media_now_playing(t)
            return {"advanced_to": tid, "smart": smart}

        while True:
            nxt_id = db.pop_queue_head()
            if not nxt_id:
                break
            advanced = start(nxt_id)
            if advanced:
                return advanced
            log.info("auto-advance: skipping broken queue entry %s", nxt_id)
        if db.get_setting_bool("shuffle_smart", True):
            current_id = eng.current.track_id if eng.current else None
            pick = shuffle_mod.smart_shuffle_next(db, current_id)
            if pick:
                return start(pick["id"], smart=True)
        return None

    def _gain_for(self, t: dict) -> float:
        db = self.db()
        ainfo = db.get_analysis(t["id"]) or {}
        lufs = ainfo.get("loudness")
        if lufs and lufs > -60:
            delta_db = min(9.0, max(-9.0, -16.0 - lufs))
            return 10 ** (delta_db / 20)
        return 1.0

    def _media_now_playing(self, t: dict) -> None:
        md = getattr(self.daemon, "media", None) if self.daemon else None
        if md:
            try:
                md.update_now_playing(t.get("title") or "", t.get("artist") or "",
                                      t.get("album") or "",
                                      float(t.get("duration") or 0.0), 0.0, True)
            except Exception:
                pass

    def _track_view(self, t: dict, db: dbmod.Database) -> dict:
        a = db.get_analysis(t["id"]) or {}
        return {
            "id": t["id"], "title": t["title"], "artist": t.get("artist"),
            "album": t.get("album"), "duration": t.get("duration"),
            "provider": t.get("provider"), "liked": t.get("liked", 0),
            "position": t.get("position"),
            "bpm": a.get("bpm"), "key": a.get("key"),
            "vocal_end": a.get("vocal_end_sec"), "vocal_intro": a.get("vocal_intro_sec"),
        }


# -- command dispatch, used by CLI directly and by daemon over IPC ----------------

_SHARED: MuseCommands | None = None
_EXIT_REQUESTED: threading.Event | None = None


def exit_requested() -> bool:
    """True after a `daemon_stop` command (daemon main loop polls this)."""
    return _EXIT_REQUESTED is not None and _EXIT_REQUESTED.is_set()


def _exit_event() -> threading.Event:
    """Module-level stop event, created on first use (also by direct-mode
    registries that never went through enable_shared_registry)."""
    global _EXIT_REQUESTED
    if _EXIT_REQUESTED is None:
        _EXIT_REQUESTED = threading.Event()
    return _EXIT_REQUESTED


def enable_shared_registry() -> MuseCommands:
    """Daemon calls this once: all IPC requests then share engine + DB state."""
    global _SHARED
    _SHARED = MuseCommands()
    _SHARED.daemon_stop_signal = _exit_event()
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