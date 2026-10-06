"""Analysis worker: import files, run tiered analysis, persist to DB + cache."""

from __future__ import annotations

import logging
from pathlib import Path

from muse import db as dbmod

log = logging.getLogger("muse.analysis")


def audio_for_vocals(path: str, seconds: float = 90.0):
    """Decode the last 90s (where AutoMix overlap planning needs vocal info)."""
    from muse.analysis.decoder import ffprobe_duration

    dur = ffprobe_duration(path)
    if dur and dur > seconds:
        import subprocess

        import numpy as np

        from muse.analysis.decoder import FFMPEG
        sr = 22050
        proc = subprocess.run(
            [FFMPEG, "-v", "error", "-nostdin", "-ss", f"{dur - seconds:g}",
             "-i", path, "-f", "f32le", "-ac", "1", "-ar", str(sr), "pipe:1"],
            capture_output=True, check=True)
        audio = np.frombuffer(proc.stdout, dtype=np.float32).copy()
        if audio.size:
            return audio
    from muse.analysis.decoder import decode
    return decode(path, sr=22050, max_seconds=seconds)


def read_metadata(path: Path) -> dict:
    """Best-effort tag/duration read via mutagen (MP3/FLAC/M4A/OGG/...)."""
    title = artist = album = genre = None
    year = track_number = None
    duration = 0.0
    try:
        from mutagen import File as mutagen_file
        m = mutagen_file(str(path), easy=True)
        if m is not None:
            duration = float(getattr(m, "info", None) and m.info.length or 0.0)
            tags = m.tags or {}
            if tags.get("title"):
                title = str(tags["title"][0])
            if tags.get("artist"):
                artist = str(tags["artist"][0])
            if tags.get("album"):
                album = str(tags["album"][0])
            if tags.get("genre"):
                genre = str(tags["genre"][0])
            if tags.get("date"):
                try:
                    year = int(str(tags["date"][0])[:4])
                except (ValueError, TypeError):
                    pass
            if tags.get("tracknumber"):
                try:
                    track_number = int(str(tags["tracknumber"][0]).split("/")[0])
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass
    return {"title": title, "artist": artist, "album": album,
            "duration": duration, "year": year, "track_number": track_number,
            "genre": genre}


def import_file(database: dbmod.Database, path: str | Path) -> int | None:
    """Import a single audio file into the library. Returns track_id or None.

    The file is copied into the managed library dir (paths.library_dir) so
    the library keeps working even when the original moves/deletes; the copy
    becomes the canonical file_path (re-importing the same source is
    idempotent — same content hash maps to the same target path)."""
    src = Path(path).expanduser()
    canonical = _library_copy(src)
    meta = read_metadata(canonical)
    return database.upsert_track(
        title=meta["title"] or canonical.stem,
        artist=meta["artist"],
        album=meta["album"],
        duration=meta["duration"] or None,
        file_path=str(canonical),
        year=meta["year"],
        track_number=meta["track_number"],
        genre=meta["genre"],
    )


def _library_copy(src: Path) -> Path:
    """Copy src into paths.library_dir (<Artist> subdir) as
    '<stem> [<hash16>].<ext>': readable original name, and re-importing the
    identical content maps to the same canonical path (idempotent)."""
    import hashlib

    import muse.paths as muse_paths

    try:
        data = src.read_bytes()
    except OSError:
        return src  # unreadable: import will fail naturally downstream
    digest = hashlib.sha256(data).hexdigest()[:16]
    meta = read_metadata(src)
    artist = meta.get("artist") or "Unknown Artist"
    safe_artist = "".join(c for c in artist if c not in '/\\:*?"<>|').strip() \
        or "Unknown Artist"
    dest_dir = muse_paths.library_dir() / safe_artist
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{src.stem} [{digest}]{src.suffix.lower() or '.audio'}"
    if not dest.exists():
        dest.write_bytes(data)
    return dest


def import_directory(database: dbmod.Database, root: str | Path) -> list[int]:
    """Import all supported audio files under root (recursive)."""
    exts = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".opus", ".aiff"}
    ids = []
    root = Path(root).expanduser()
    if not root.is_dir():
        return ids
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() in exts:
            tid = import_file(database, p)
            if tid is not None:
                ids.append(tid)
    return ids


def run_analysis(database: dbmod.Database, track_id: int, tiers: int = 2) -> bool:
    """Analyze one track (tier 1+2). Stores results. Returns True on success."""
    track = database.get_track(track_id)
    if not track or not track.get("file_path"):
        return False
    path = track["file_path"].replace("~", str(Path.home()))
    if not Path(path).is_file():
        return False
    try:
        from muse.analysis.features import analyze_file

        result = analyze_file(path)
    except Exception as e:  # decode errors etc: mark tier-0 done so we don't retry forever
        log.warning("analysis failed for track %s: %s", track_id, e)
        database.save_analysis(track_id, tiers_done=0, bpm=0.0, key="", loudness=-70.0,
                               energy=0.0, beat_grid=[], beat_count=0, structure={})
        return False
    try:
        from muse.analysis.vocals import vocal_end_sec, vocal_intro_sec, vocal_regions

        regions = vocal_regions(audio_for_vocals(path))
        database.save_analysis(
            track_id,
            vocal_regions=regions,
            vocal_intro_sec=vocal_intro_sec(regions),
            vocal_end_sec=vocal_end_sec(regions),
        )
    except Exception as e:  # vocal pass is best-effort Tier-2
        log.debug("vocal analysis failed for track %s: %s", track_id, e)
    database.save_analysis(
        track_id,
        bpm=result["bpm"],
        key=result["key"],
        loudness=result["loudness"],
        energy=result["energy"],
        intro_sec=result["intro_sec"],
        outro_sec=result["outro_sec"],
        beat_count=result["beat_count"],
        beat_grid=result["beat_grid"],
        structure=result["structure"],
        tiers_done=result["tiers_done"],
    )
    return True


def analyze_pending(database: dbmod.Database, tiers: int = 2, limit: int = 50) -> list[int]:
    """Analyze up to `limit` tracks missing analysis.

    Missing files are pruned first: without this, dead rows occupy the
    pending batch head but fail `run_analysis` invisibly, and once 50+ such
    rows exist `analyze --all` can never reach any live pending track."""
    database.prune_missing_files()
    done = []
    for tid in database.pending_analysis(tiers=tiers, limit=limit):
        if run_analysis(database, tid, tiers=tiers):
            done.append(tid)
    return done


def ensure_analysis(database: dbmod.Database, track_id: int, tiers: int = 1) -> dict | None:
    """Get analysis, running it synchronously if missing (used by automix preview)."""
    a = database.get_analysis(track_id)
    if a and a.get("tiers_done", 0) >= tiers:
        return a
    run_analysis(database, track_id, tiers=2)
    return database.get_analysis(track_id)


def analysis_summary(database: dbmod.Database, track_id: int) -> str:
    a = database.get_analysis(track_id)
    if not a:
        return "not analyzed"
    return (f"BPM {a.get('bpm') or '?'}  key {a.get('key') or '?'}  "
            f"LUFS {a.get('loudness') or '?'}  energy {a.get('energy') or '?'}")