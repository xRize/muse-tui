"""muse TUI (Textual) — Search / Library / Queue / Now Playing (spec §4 wireframe)."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static

from muse.daemon import ipc


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
        meta = f"  · {bpm:g} BPM" if bpm else ""
        label = f"{prefix}{title}  —  {artist}  [{dur}] ({provider}){meta}"
        super().__init__(Static(label))


class MuseTUI(App):
    TITLE = "muse"
    CSS = """
    Screen { layout: vertical; }
    #panes { height: 1fr; }
    #search-pane { height: auto; }
    #lists { height: 1fr; }
    #now-playing { dock: bottom; height: 7; padding: 0 1; border: round $accent;
                   background: $surface; }
    #progress { color: $accent; }
    ListView { border: round $primary; background: $surface; }
    Input { border: round $primary; }
    .pane-label { padding: 0 1; color: $text-muted; }
    """
    BINDINGS = [
        Binding("space", "toggle", "Play/Pause"),
        Binding("n", "next", "Next"),
        Binding("b", "prev", "Prev"),
        Binding("+", "vol_up", "Vol+", show=False),
        Binding("-", "vol_down", "Vol-", show=False),
        Binding("t", "focus_search", "Search"),
        Binding("q", "quit", "Quit"),
        Binding("L", "lyrics", "Lyrics"),
        Binding("a", "automix_toggle", "AutoMix"),
    ]

    def __init__(self):
        super().__init__()
        self._last_search = ""
        self._search_timer = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="panes"):
            with Vertical(id="search-pane"):
                yield Input(placeholder="Search library… (Enter to search)", id="search-box")
                yield Label("", id="search-info")
            with Horizontal(id="lists"):
                yield ListView(id="results", classes="pane")
                yield ListView(id="queue", classes="pane")
        with Static(id="now-playing"):
            yield Label("idle", id="np-title")
            yield Label("", id="progress")
            yield Label("", id="np-status")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(2.0, self.refresh_status)
        self.set_interval(0.5, self.refresh_progress)
        self.query_one("#search-box", Input).focus()
        self.refresh_status()
        self.refresh_queue()

    # -- helpers ----------
    def _call(self, method: str, args: dict | None = None) -> dict:
        resp = ipc.request(method, args or {})
        if not resp.get("ok") and "daemon not running" in str(resp.get("error", "")):
            from muse.daemon.commands import handle
            resp = handle(method, args or {})
            resp.setdefault("ok", "error" not in resp)
        return resp

    # -- actions ----------
    def action_toggle(self) -> None:
        self._call("toggle")
        self.refresh_status()

    def action_next(self) -> None:
        self._call("next", {"smart": True})
        self.refresh_status()
        self.refresh_queue()

    def action_prev(self) -> None:
        self._call("prev")

    def action_vol_up(self) -> None:
        self._call("volume", {"value": "up"})
        self.refresh_status()

    def action_vol_down(self) -> None:
        self._call("volume", {"value": "down"})
        self.refresh_status()

    def action_focus_search(self) -> None:
        self.query_one("#search-box", Input).focus()

    def action_automix_toggle(self) -> None:
        r = self._call("automix", {"action": "config"})
        new = "off" if r.get("enabled") else "on"
        self._call("automix", {"action": new})
        self.refresh_status()

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

    def run_search(self, q: str) -> None:
        r = self._call("search", {"query": q, "provider": "local"})
        lv = self.query_one("#results", ListView)
        lv.clear()
        results = r.get("results", [])
        self.query_one("#search-info", Label).update(
            f"{len(results)} result(s) for {q!r} — Enter plays; Space plays under cursor; "
            "Q queues")
        for t in results:
            lv.append(TrackItem(t))
        self._last_search = q

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, TrackItem):
            track = item.track
            if event.list_view.id == "results":
                self._call("play", {"track_id": track["id"]})
                self.refresh_status()
            elif event.list_view.id == "queue":
                pass
            self.refresh_queue()

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
        lv = self.query_one("#queue", ListView)
        lv.clear()
        for q in r.get("queue", []):
            lv.append(TrackItem(q, prefix=f"{q.get('position')}. "))


def run() -> int:
    app = MuseTUI()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())