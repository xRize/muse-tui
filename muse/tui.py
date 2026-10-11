"""muse TUI (Textual) — Search / Queue / Library / Playlists / Downloads tabs.

Wireframe (spec §4): tab bar, fuzzy search pane, scrollable track lists,
progress bar; bottom controls with hotkeys. The Downloads tab is a YouTube
front-end (search hits + job queue + live progress) built on yt-dlp. Falls
back to in-process command handling when no daemon runs (same as the CLI).
"""
from __future__ import annotations

import os

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from muse.daemon import ipc
from muse.providers.youtube import _LIVEISH_RE, _NOISE_RE


def _kitty_like() -> bool:
    """True when the TUI runs in kitty (the only terminal we draw images in).

    Kitty reports TERM=xterm-kitty (and TERM_PROGRAM=kitty); SSH hosts keep
    the client's value, so forwarding through the socket is preserved."""
    term = os.environ.get("TERM", "")
    prog = os.environ.get("TERM_PROGRAM", "")
    return term == "xterm-kitty" or "kitty" in term.lower() \
        or "kitty" in prog.lower()


# Cover-art widget class, decided once before Textual owns the terminal (the
# textual_image import probes terminal capabilities over stdin/stdout; doing
# that mid-TUI would corrupt the event stream). None -> no art at all.
_cover_widget_class = None
if _kitty_like():
    try:
        from textual_image.widget import TGPImage as _cover_widget_class
    except Exception:  # textual-image not importable: no art, text unaffected
        _cover_widget_class = None


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
        super().__init__(Static(label, markup=False))


class PlaylistItem(ListItem):
    def __init__(self, pl: dict):
        self.playlist = pl
        kind = "smart" if pl.get("is_smart") else ""
        super().__init__(Static(
            f"{pl['name']}  ({pl.get('n_tracks', 0)} tracks {kind})", markup=False))


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
        super().__init__(Static(
            f"{title}  —  {artist}  [{dur}]  ({url}){tag}", markup=False))


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
        super().__init__(Static("\n".join(lines), markup=False))


class MuseTUI(App):
    TITLE = "muse"
    SUB_TITLE = "your local radio"
    CSS = """
    Screen { layout: vertical; background: $background; }

    #now-playing {
        dock: bottom; height: 7; padding: 0 2;
        border: round $accent; background: $panel;
        layout: horizontal; align-vertical: middle;
    }
    #np-cover { width: 12; height: 5; margin-right: 2; align-vertical: middle; }
    #np-text { width: 1fr; padding: 0 1; }
    #np-title { text-style: bold; }
    #np-status { color: $text-muted; }
    #progress { color: $accent; }

    Tabs { background: $panel; }
    Tabs Tab.-active { color: $accent; text-style: bold; }
    TabPane { padding: 1 2; }

    ListView { border: round $primary; background: $surface; }
    ListView:focus { border: round $accent; }
    ListItem { padding: 0 1; }

    Input { border: round $primary; background: $surface; }
    Input:focus { border: round $accent; }

    #search-info, #dl-info, #queue-info {
        padding: 0 1; color: $text-muted;
    }

    #dl-results { height: 1fr; }
    #dl-jobs { height: 8; }
    #dl-jobs-header { padding: 1 1 0 1; color: $text-muted; text-style: bold; }

    Footer { background: $panel; border-top: heavy $primary; }

    #fullscreen-panel {
        display: none;
        dock: top;
        width: 100%;
        height: 100%;
        layout: horizontal;
        background: $background;
        layer: above;
    }
    #fs-left {
        width: 50%;
        height: 100%;
        layout: vertical;
        align: center middle;
        padding: 1 2;
    }
    #fs-cover {
        width: auto;
        height: auto;
        max-height: 20;
        margin-bottom: 1;
    }
    #fs-title-text {
        width: 90%;
        background: $surface;
        border: round $accent;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #fs-status-text {
        width: 90%;
        color: $text-muted;
        text-align: center;
        margin-bottom: 1;
    }
    #fs-lyrics-pane {
        width: 50%;
        height: 100%;
        layout: vertical;
        padding: 1 2;
    }
    #fs-lyrics {
        height: 1fr;
        border: round $primary;
    }
    #fs-hint {
        height: auto;
        text-align: center;
        color: $text-muted;
    }
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
        Binding("5", "downloads_tab", "Downloads tab", show=False),
        Binding("s", "shuffle_queue", "Shuffle queue"),
        Binding("g", "genq", "Generate Queue"),
        Binding("d", "discover", "Discover"),
        Binding("L", "lyrics", "Lyrics", key_display="Shift+L"),
        Binding("F", "toggle_panel", "Fullscreen", key_display="Shift+F"),
        Binding("Q", "queue_selected", "Queue highlighted", key_display="Shift+Q"),
        Binding("a", "automix_toggle", "AutoMix"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.theme = "tokyo-night"
        self._last_search = ""
        self._last_yt_query: str | None = None
        self._cover_track_id = None
        self._cover_failed = False
        self._panel_visible = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="search", id="tabs"):
            with TabPane("Search", id="search"):
                yield Input(placeholder="Search library… (Enter to search)", id="search-box")
                yield Label("", id="search-info", markup=False)
                yield ListView(id="results")
            with TabPane("Queue", id="queue"):
                yield Label("Enter removes a track · g generates a similar queue",
                            id="queue-info", markup=False)
                yield ListView(id="queue-list")
            with TabPane("Library", id="library"):
                yield ListView(id="library-list")
            with TabPane("Playlists", id="playlists"):
                yield ListView(id="playlists-list")
            with TabPane("Downloads", id="downloads"):
                yield Input(placeholder="YouTube URL, 'yt:<id>' or search… "
                                        "(Enter searches YouTube)",
                            id="dl-box")
                yield Label("", id="dl-info", markup=False)
                yield ListView(id="dl-results")
                yield Static("jobs", id="dl-jobs-header", markup=False)
                yield ListView(id="dl-jobs")
        with Static(id="now-playing"):
            if _cover_widget_class is not None:
                yield _cover_widget_class(None, id="np-cover")
            with Static(id="np-text"):
                yield Label("idle", id="np-title", markup=False)
                yield Label("", id="progress", markup=False)
                yield Label("", id="np-status", markup=False)
        yield Footer()
        # -- fullscreen lyrics panel --
        with Static(id="fullscreen-panel"):
            with Static(id="fs-left"):
                if _cover_widget_class is not None:
                    yield _cover_widget_class(None, id="fs-cover")
                yield Label("", id="fs-title-text", markup=False)
                yield Label("", id="fs-status-text", markup=False)
            with Static(id="fs-lyrics-pane"):
                yield RichLog(id="fs-lyrics", wrap=True,
                              highlight=False, markup=False, max_lines=5000)
                yield Label("Shift+F: close · scroll arrows move lyrics",
                            id="fs-hint", markup=False)

    def on_mount(self) -> None:
        self.set_interval(2.0, self.refresh_status)
        self.set_interval(0.5, self.refresh_progress)
        self.set_interval(6.0, self.refresh_passive_lists)
        self.set_interval(1.0, self.refresh_downloads)
        self.query_one("#search-box", Input).focus()
        self.query_one("#now-playing").border_title = " now playing "
        self.query_one("#results", ListView).border_title = " search results "
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

    def action_queue_selected(self) -> None:
        """Shift+Q: queue the highlighted track (Search results or Library)."""
        tabs = self.query_one("#tabs", TabbedContent)
        active = tabs.active
        if active not in ("search", "library"):
            self.query_one("#queue-info", Label).update(
                "Shift+Q queues from the Search or Library tab")
            self.action_tab("queue")
            return
        lv_id = "#results" if active == "search" else "#library-list"
        lv = self.query_one(lv_id, ListView)
        item = lv.highlighted_child
        if info_id := ("#search-info" if active == "search"
                      else "#queue-info"):
            info = self.query_one(info_id, Label)
        else:
            info = None
        if not isinstance(item, TrackItem):
            # ListView highlights nothing until the cursor moves — fall back
            # to the first item so Shift+Q works immediately after focusing.
            if lv.children:
                lv.index = 0
                item = lv.highlighted_child
        if not isinstance(item, TrackItem):
            if info:
                info.update("nothing here to queue — Shift+Q queues from "
                            "Search or Library lists")
            return
        self._call("queue", {"action": "add", "ref": str(item.track["id"])})
        if info:
            info.update(
                f"queued: {item.track.get('title')!r} — "
                + ("Enter plays; Shift+Q queues" if active == "search"
                   else "Enter removes; g generates a similar queue"))
        self.refresh_queue()

    def action_toggle_panel(self) -> None:
        """Toggle fullscreen lyrics panel."""
        self._panel_visible = not self._panel_visible
        if self._panel_visible:
            self._show_panel()
        else:
            self._hide_panel()

    def _show_panel(self) -> None:
        panel = self.query_one("#fullscreen-panel")
        panel.display = True
        self.query_one("#fs-lyrics", RichLog).border_title = " lyrics "
        self._refresh_panel()
        self.query_one("#fs-lyrics").focus()

    def _hide_panel(self) -> None:
        panel = self.query_one("#fullscreen-panel")
        panel.display = False
        self.call_after_refresh(lambda: self.query_one("#tabs").focus())

    def _refresh_panel(self) -> None:
        self.query_one("#fs-title-text").update(self._get_now_playing())
        self.query_one("#fs-status-text").update(self._get_status_line())
        try:
            lyrics = self._call("lyrics").get("lyrics")
        except Exception:
            lyrics = None
        log = self.query_one("#fs-lyrics", RichLog)
        log.clear()
        log.write(lyrics or "(no lyrics)")

    @staticmethod
    def _artist_title_line(artist: str, title: str) -> str:
        """Rendered now-playing line; many local filenames already embed the
        artist ("Artist - Title"), so avoid printing it twice."""
        if artist and not title.lower().startswith(artist.lower()):
            return f"{artist} — {title}"
        return title

    def _get_now_playing(self) -> str:
        st = self._call("status")
        return self._artist_title_line(st.get("artist") or "",
                                       st.get("title") or "idle")

    def _get_status_line(self) -> str:
        st = self._call("status")
        vol = st.get("volume", "")
        am = self._call("automix", {"action": "config"}).get("enabled", False)
        state = st.get("state", "idle")
        am_text = "ON" if am else "OFF"
        return "Vol %s%%  AutoMix %s   %s" % (vol, am_text, state)

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
            f"{len(results)} result(s) for {q!r} — Enter plays; Shift+Q queues")
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
        state = st.get("state") or "idle"
        icon = {"playing": "▶", "paused": "‖", "stopped": "■"}.get(state, "•")
        title = st.get("title") or "idle"
        artist = st.get("artist") or ""
        self.query_one("#np-title", Label).update(
            f"{icon} {self._artist_title_line(artist, title)}")
        am_on = self._call("automix", {"action": "config"}).get("enabled")
        self.query_one("#np-status", Label).update(
            f"Vol {st.get('volume', '?')}%  ·  AutoMix {'ON' if am_on else 'OFF'}"
            f"  ·  {state}")
        self.sub_title = state
        self.refresh_cover(st)

    def refresh_cover(self, st: dict) -> None:
        """Now-playing album art (kitty TGP image only, no-op elsewhere)."""
        tid = st.get("id")
        if tid == self._cover_track_id and not self._cover_failed:
            return
        self._cover_track_id = tid
        self._cover_failed = False
        if _cover_widget_class is None:  # not kitty: text-only layout
            return
        widget = self.query_one("#np-cover")
        if not tid:
            widget.image = None
            return
        try:
            r = self._call("cover", {"ref": str(tid)})
            path = r.get("cover") or ""
            if not path:
                widget.image = None
                return
            widget.image = path
        except Exception:
            self._cover_failed = True

    def refresh_progress(self) -> None:
        st = self._call("status")
        pos, dur = st.get("position") or 0, st.get("duration") or 0
        width = 40
        frac = min(1.0, (pos / dur)) if dur else 0
        filled = frac * width
        full = int(filled)
        half = 1 if filled - full >= 0.5 else 0
        bar = "▏" + "█" * full + ("▌" if half else "") \
            + "░" * (width - full - half) + "▕"
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
                    "g generates a similar queue")

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