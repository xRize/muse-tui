"""muse TUI (Textual) — Search / Queue / Library / Playlists / Downloads tabs.

Wireframe (spec §4): tab bar, fuzzy search pane, scrollable track lists,
progress bar; bottom controls with hotkeys. The Downloads tab is a YouTube
front-end (search hits + job queue + live progress) built on yt-dlp. Falls
back to in-process command handling when no daemon runs (same as the CLI).
"""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    TabbedContent,
    TabPane,
)

from muse.daemon import ipc
from muse.providers.youtube import _LIVEISH_RE, _NOISE_RE


def _fmt(s: float | None) -> str:
    if not s:
        return "?:??"
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


class TrackItem(ListItem):
    def __init__(self, track: dict, prefix: str = ""):
        self.track = track
        dur = _fmt(track.get("duration"))
        artist = track.get("artist") or "?"
        title = track.get("title") or "?"
        provider = track.get("provider") or "local"
        bpm = track.get("bpm")
        genre = track.get("genre")
        meta = f"  · {bpm:g} BPM" if bpm else ""
        if genre:
            meta += f"  · {genre}"
        label = f"{prefix}{title}  —  {artist}  [{dur}] ({provider}){meta}"
        super().__init__(Static(label))


class PlaylistItem(ListItem):
    def __init__(self, pl: dict):
        self.playlist = pl
        kind = "smart" if pl.get("is_smart") else ""
        super().__init__(Static(f"{pl['name']}  ({pl.get('n_tracks', 0)} tracks {kind})"))


class ResultItem(ListItem):
    """A YouTube search hit (no local track id yet)."""

    def __init__(self, result: dict):
        self.result = result
        dur = _fmt(result.get("duration"))
        artist = result.get("artist") or "?"
        title = result.get("title") or "?"
        url = result.get("url") or ""
        if url.startswith("https://www.youtube.com/watch?v=") and not result.get("is_playlist"):
            url = "yt:" + url.split("watch?v=", 1)[1]
        raw = (result.get("raw_title") or title or "").lower()
        noisy = _NOISE_RE.search(raw) or _LIVEISH_RE.search(raw)
        tag = " video" if noisy else " song"
        super().__init__(Static(f"{title}  —  {artist}  [{dur}]  ({url}){tag}"))


class DownloadItem(ListItem):
    """A download job row in the Downloads tab."""

    def __init__(self, job: dict):
        self.job = job
        state = job.get("state", "?")
        icon = {"running": "⏳", "done": "✓", "failed": "✗"}.get(state, "·")
        label = job.get("label") or ""
        extra = (job.get("error") or
                 ("" if state == "done" else job.get("progress") or ""))
        ids = job.get("track_ids") or []
        if ids:
            extra = f"imported: {', '.join(map(str, ids))}"
        extra = f"  {extra}" if extra else ""
        lines = [f"{icon} #{job['id']} [{state}] {job.get('kind', '?')}: "
                 f"{label}{extra}"]
        if state == "running" and job.get("progress"):
            lines.append(f"   {job['progress']}")
        super().__init__(Static("\n".join(lines)))


class MuseTUI(App):
    TITLE = "muse"
    CSS = """
    Screen { layout: vertical; }
    #now-playing { dock: bottom; height: 7; padding: 0 1; border: round $accent;
                   background: $surface; }
    #np-cover { width: 12; height: 5; margin-right: 2; }
    #progress { color: $accent; }
    ListView { border: round $primary; background: $surface; }
    #dl-results { height: 1fr; }
    #dl-jobs { height: 8; }
    #dl-jobs-header { padding: 0 1; color: $text-muted; }
    Input { border: round $primary; }
    #search-info { padding: 0 1; color: $text-muted; }
    #dl-info { padding: 0 1; color: $text-muted; }
    TabPane { padding: 0 1; }
    """
    BINDINGS = [
        Binding("space", "toggle", "Play/Pause"),
        Binding("n", "next", "Next"),
        Binding("b", "prev", "Prev"),
        Binding("+", "vol_up", "Vol+", show=False),
        Binding("-", "vol_down", "Vol-", show=False),
        Binding("t", "focus_search", "Search"),
        Binding("1", "tab('search')", "Search tab", show=False),
        Binding("2", "tab('queue')", "Queue tab", show=False),
        Binding("3", "tab('library')", "Library tab", show=False),
        Binding("4", "tab('playlists')", "Playlists tab", show=False),
        Binding("5", "tab('downloads')", "Downloads tab", show=False),
        Binding("s", "shuffle_queue", "Shuffle queue"),
        Binding("g", "genq", "Generate Queue"),
        Binding("d", "discover", "Discover"),
        Binding("L", "lyrics", "Lyrics"),
        Binding("a", "automix_toggle", "AutoMix"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self._last_search = ""
        self._last_yt_query: str | None = None
        self._cover_track_id = None
        self._cover_failed = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="search", id="tabs"):
            with TabPane("Search", id="search"):
                yield Input(placeholder="Search library… (Enter to search)", id="search-box")
                yield Label("", id="search-info")
                yield ListView(id="results")
            with TabPane("Queue", id="queue"):
                yield Label("Enter removes a track; G generates a similar queue",
                            id="queue-info")
                yield ListView(id="queue-list")
            with TabPane("Library", id="library"):
                yield ListView(id="library-list")
            with TabPane("Playlists", id="playlists"):
                yield ListView(id="playlists-list")
            with TabPane("Downloads", id="downloads"):
                yield Input(placeholder="YouTube URL, 'yt:<id>' or search… "
                                        "(Enter searches YouTube)",
                            id="dl-box")
                yield Label("", id="dl-info")
                yield ListView(id="dl-results")
                yield Static("jobs", id="dl-jobs-header")
                yield ListView(id="dl-jobs")
        with Static(id="now-playing"):
            yield Static("", id="np-cover")
            yield Label("idle", id="np-title")
            yield Label("", id="progress")
            yield Label("", id="np-status")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(2.0, self.refresh_status)
        self.set_interval(0.5, self.refresh_progress)
        self.set_interval(6.0, self.refresh_passive_lists)
        self.set_interval(1.0, self.refresh_downloads)
        self.query_one("#search-box", Input).focus()
        self.refresh_status()
        self.refresh_queue()
        self.refresh_library()
        self.refresh_playlists()
        self.refresh_downloads()

    # -- helpers ----------
    def _call(self, method: str, args: dict | None = None) -> dict:
        resp = ipc.request(method, args or {})
        if not resp.get("ok") and "daemon not running" in str(resp.get("error", "")):
            from muse.daemon.commands import handle
            resp = handle(method, args or {})
            resp.setdefault("ok", "error" not in resp)
        return resp

    # -- actions ----------
    def action_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab

    def action_toggle(self) -> None:
        self._call("toggle")
        self.refresh_status()

    def action_next(self) -> None:
        self._call("next", {"smart": True})
        self.refresh_status()
        self.refresh_queue()

    def action_prev(self) -> None:
        self._call("prev")
        self.refresh_status()

    def action_vol_up(self) -> None:
        self._call("volume", {"value": "up"})
        self.refresh_status()

    def action_vol_down(self) -> None:
        self._call("volume", {"value": "down"})
        self.refresh_status()

    def action_focus_search(self) -> None:
        self.action_tab("search")
        self.query_one("#search-box", Input).focus()

    def action_automix_toggle(self) -> None:
        r = self._call("automix", {"action": "config"})
        new = "off" if r.get("enabled") else "on"
        self._call("automix", {"action": new})
        self.refresh_status()

    def action_shuffle_queue(self) -> None:
        self._call("queue", {"action": "shuffle", "keep_first": True})
        self.refresh_queue()

    def action_genq(self) -> None:
        """Generate Queue: scored similar tracks queued to play next."""
        r = self._call("genq", {"length": 5})
        picks = r.get("generated", [])
        info = self.query_one("#queue-info", Label)
        if r.get("error"):
            info.update(f"⚠ {r['error']}")
        elif not picks:
            info.update("no similar tracks found — analyze the library first "
                        "(`muse analyze --all`)")
        else:
            names = ", ".join(
                f"{t.get('artist') or '?'} — {t['title']}" for t in picks[:3])
            more = f" +{len(picks) - 3}" if len(picks) > 3 else ""
            info.update(f"generated {len(picks)} similar track(s): {names}{more}"
                        " — queued to play next")
        self.refresh_queue()

    def action_discover(self) -> None:
        r = self._call("discover", {"length": 12})
        lv = self.query_one("#results", ListView)
        lv.clear()
        self.query_one("#search-info", Label).update(
            f"discover: {len(r.get('discover', []))} picks (Enter plays)")
        for t in r.get("discover", []):
            lv.append(TrackItem(t))
        self.action_tab("search")

    def action_lyrics(self) -> None:
        r = self._call("lyrics")
        text = r.get("lyrics") or "(no lyrics)"
        self.query_one("#np-status", Label).update(text.splitlines()[0] if text else "")

    # -- events ----------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-box":
            q = event.value.strip()
            if q:
                self.run_search(q)
        elif event.input.id == "dl-box":
            q = event.value.strip()
            if q:
                self.run_yt_search(q)

    def run_search(self, q: str) -> None:
        r = self._call("search", {"query": q, "provider": "local"})
        lv = self.query_one("#results", ListView)
        lv.clear()
        results = r.get("results", [])
        self.query_one("#search-info", Label).update(
            f"{len(results)} result(s) for {q!r} — Enter plays; Q queues")
        for i, t in enumerate(results, 1):
            lv.append(TrackItem(t, prefix=f"{i}. "))
        self._last_search = q

    # -- downloads tab (YouTube search -> queue -> poll -> library) -----------
    def run_yt_search(self, q: str) -> None:
        """dl-box Enter: a URL/id queues a download directly; text searches
        YouTube (yt-dlp) with Enter-to-download hits."""
        if q.startswith(("http://", "https://", "yt:", "ytpl:")):
            r = self._call("yt_get", {"ref": q, "playlist": "/playlist" in q})
            info = self.query_one("#dl-info", Label)
            if r.get("error"):
                info.update(f"⚠ {r['error']}")
                return
            info.update(f"queued job #{r.get('job')} ({r.get('kind')}) — "
                        "see Jobs below; notice: for personal/archival use only")
            self._last_yt_query = None
            self.refresh_downloads()
            return
        self._last_yt_query = q
        r = self._call("yt_search", {"query": q})
        lv = self.query_one("#dl-results", ListView)
        lv.clear()
        results = r.get("results", [])
        if r.get("offline"):
            self.query_one("#dl-info", Label).update(
                "offline mode — YouTube search disabled (unset MUSE_OFFLINE)")
            return
        if not results:
            yt_ok = self._call("yt_available").get("available", False)
            self.query_one("#dl-info", Label).update(
                f"no YouTube results for {q!r}" + ("" if yt_ok else
                                                   " — is yt-dlp installed?"))
            return
        self.query_one("#dl-info", Label).update(
            f"{len(results)} YouTube hit(s) for {q!r} — Enter downloads into "
            "muse library")
        for t in results:
            lv.append(ResultItem(t))

    def action_downloads_tab(self) -> None:
        """Tab-5 binding: focus the dl-box."""
        self.action_tab("downloads")
        self.query_one("#dl-box", Input).focus()

    def refresh_downloads(self) -> None:
        """1s poller: job rows live-update (progress/imports/errors)."""
        r = self._call("downloads")
        jobs = r.get("jobs", [])
        header = self.query_one("#dl-jobs-header", Static)
        running = sum(1 for j in jobs if j.get("state") == "running")
        header.update(
            f"jobs ({running} running)" if running else
            ("jobs (idle)" if jobs else "jobs — paste a YouTube URL above"))
        lv = self.query_one("#dl-jobs", ListView)
        sig = [(j.get("id"), j.get("state"),
                (j.get("error") or j.get("progress") or "")[-40:])
               for j in jobs]
        if sig == getattr(self, "_last_dl_sig", None):
            return
        self._last_dl_sig = sig
        keep = lv.highlighted_child
        keep_idx = lv.index
        lv.clear()
        for j in jobs:
            lv.append(DownloadItem(j))
        if keep is not None and keep_idx is not None:
            try:
                lv.index = keep_idx
            except Exception:
                pass

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if not isinstance(item, (TrackItem, PlaylistItem, ResultItem, DownloadItem)):
            return
        lv_id = event.list_view.id
        if isinstance(item, PlaylistItem):
            r = self._call("playlist", {"action": "show", "name": item.playlist["name"]})
            results = self.query_one("#results", ListView)
            results.clear()
            self.query_one("#search-info", Label).update(
                f"playlist {item.playlist['name']!r}: {len(r.get('tracks', []))} tracks")
            for t in r.get("tracks", []):
                results.append(TrackItem(t))
            self.action_tab("search")
        elif isinstance(item, ResultItem):
            # YouTube hit -> download into the library (job appears below)
            self.query_one("#dl-info", Label).update(
                f"queued download: {item.result.get('title')!r} — for personal/"
                "archival use only, respect YouTube ToS and copyright")
            self.run_yt_search(item.result.get("url") or item.result.get("id") or "")
        elif isinstance(item, DownloadItem):
            tid = (item.job.get("track_ids") or [None])[0]
            if tid:
                self._call("play", {"track_id": tid})
                self.refresh_status()
        elif lv_id == "results":
            self._call("play", {"track_id": item.track["id"]})
            self.refresh_status()
        elif lv_id == "library-list":
            self._call("play", {"track_id": item.track["id"]})
            self.refresh_status()
        elif lv_id == "queue-list":
            self._call("queue", {"action": "remove", "ref": str(item.track.get("position"))})
        self.refresh_queue()

    # -- refreshers ----------
    def refresh_status(self) -> None:
        st = self._call("status")
        title = st.get("title") or "idle"
        artist = st.get("artist") or ""
        self.query_one("#np-title", Label).update(
            f"▶ {artist} — {title}" if artist else f"▶ {title}")
        am = self._call("automix", {"action": "config"})
        self.query_one("#np-status", Label).update(
            f"Vol {st.get('volume', '?')}%  ·  AutoMix {'ON' if am.get('enabled') else 'OFF'}"
            f"  ·  state {st.get('state')}")
        self.sub_title = f"{st.get('state', 'idle')}"
        self.refresh_cover(st)

    def refresh_cover(self, st: dict) -> None:
        """Now-playing album art (5-row half-block pixels, no-op on failure)."""
        tid = st.get("id")
        if tid == getattr(self, "_cover_track_id", object()) and not (
                self._cover_failed):
            return
        self._cover_track_id = tid
        self._cover_failed = False
        widget = self.query_one("#np-cover", Static)
        if not tid:
            widget.update("")
            return
        try:
            r = self._call("cover", {"ref": str(tid)})
            path = r.get("cover") or ""
            if not path:
                widget.update("")
                return
            from PIL import Image
            from rich_pixels import Pixels
            img = Image.open(path).convert("RGB")
            widget.update(Pixels.from_image(img, resize=(18, 9)))
        except Exception:
            self._cover_failed = True
            widget.update("")

    def refresh_progress(self) -> None:
        st = self._call("status")
        pos, dur = st.get("position") or 0, st.get("duration") or 0
        width = 40
        frac = min(1.0, (pos / dur)) if dur else 0
        filled = int(frac * width)
        bar = "▏" + "█" * filled + "·" * (width - filled) + "▕"
        self.query_one("#progress", Label).update(
            f"{bar}  {_fmt(pos)} / {_fmt(dur)}")

    def refresh_queue(self) -> None:
        r = self._call("queue")
        lv = self.query_one("#queue-list", ListView)
        lv.clear()
        for q in r.get("queue", []):
            lv.append(TrackItem(q, prefix=f"{q.get('position')}. "))
        info = self.query_one("#queue-info", Label)
        info.update(f"{len(r.get('queue', []))} queued — Enter removes; "
                    "G generates a similar queue")

    def refresh_library(self) -> None:
        r = self._call("library", {"limit": 500})
        lv = self.query_one("#library-list", ListView)
        lv.clear()
        for i, t in enumerate(r.get("tracks", []), 1):
            lv.append(TrackItem(t, prefix=f"{i}. "))

    def refresh_playlists(self) -> None:
        r = self._call("playlist")
        lv = self.query_one("#playlists-list", ListView)
        lv.clear()
        for pl in r.get("playlists", []):
            lv.append(PlaylistItem(pl))

    def refresh_passive_lists(self) -> None:
        self.refresh_queue()
        self.refresh_library()
        self.refresh_playlists()


def run() -> int:
    app = MuseTUI()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())