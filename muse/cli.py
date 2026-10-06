"""muse — CLI client (spec §4).

Commands map 1:1 to the daemon IPC API; if no daemon is running, commands run
directly against a fresh command registry (useful for scripting/tests).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from muse import __version__
from muse.daemon import ipc


def _call(method: str, args: dict | None = None, as_json: bool = False,
          direct: bool = False):
    """Call daemon over IPC; fall back to in-process handling when absent."""
    resp = ipc.request(method, args or {})
    if not resp.get("ok") and "daemon not running" in str(resp.get("error", "")):
        from muse.daemon.commands import handle
        resp = handle(method, args or {})
        resp.setdefault("ok", "error" not in resp)
    if as_json:
        print(json.dumps(resp, indent=2, default=str))
    return resp


def _fail(resp: dict) -> bool:
    if not resp.get("ok") or resp.get("error"):
        print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
        return True
    return False


def _fmt_seconds(s: float | None) -> str:
    if not s:
        return "?:??"
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


def main(argv: list[str] | None = None) -> int:
    # shared parent: --json accepted before OR after the subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="emit raw JSON (for scripting)")
    p = argparse.ArgumentParser(
        prog="muse", description="terminal music client (daemon-backed)",
        parents=[common])
    p.add_argument("--version", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    def mk(*args, **kwargs):
        return sub.add_parser(*args, parents=[common], **kwargs)

    sp = mk("play", help="play track by id or search query")
    sp.add_argument("ref", nargs="?")
    sp = mk("pause")
    sp = mk("resume")
    sp = mk("toggle")
    sp = mk("stop")
    sp = mk("next")
    sp.add_argument("--plain", action="store_true", help="skip Smart Shuffle")
    sp = mk("prev")
    sp = mk("status")
    sp = mk("stats", help="library/play counts summary")

    sp = mk("seek", help="seek [+-]seconds (absolute or relative)")
    sp.add_argument("seconds")

    sp = mk("queue", help="list/add/remove/shuffle/clear queue")
    sp.add_argument("action", nargs="?", default="list")
    sp.add_argument("ref", nargs="?")
    sp.add_argument("--keep-first", action="store_true",
                    help="shuffle: keep the playing track first")

    sp = mk("shuffle", help="shuffle the queue (alias of `queue shuffle`)")
    sp.add_argument("--keep-first", action="store_true",
                    help="keep the playing track first")

    sp = mk("search", help="search providers (local by default)")
    sp.add_argument("query")
    sp.add_argument("--provider", default="local", choices=["local", "youtube", "all"])

    sp = mk("library", help="list tracks (--kind artists|albums for groups)")
    sp.add_argument("--artist")
    sp.add_argument("--album")
    sp.add_argument("--kind", choices=["artists", "albums"], default=None)

    sp = mk("import", help="add audio file/directory to library")
    sp.add_argument("path")

    sp = mk("analyze", help="run offline analysis")
    sp.add_argument("ref", nargs="?", help="track id (default: all pending)")
    sp.add_argument("--all", action="store_true")

    sp = mk("playlist", help="list/create/delete/add/show")
    sp.add_argument("action", default="list")
    sp.add_argument("name", nargs="?")
    sp.add_argument("ref", nargs="?")

    sp = mk("like")
    sp.add_argument("ref", nargs="?")
    sp = mk("unlike")
    sp.add_argument("ref", nargs="?")

    sp = mk("volume")
    sp.add_argument("value", nargs="?")

    sp = mk("automix", help="on/off/length N/config")
    sp.add_argument("action", nargs="?", default="config")
    sp.add_argument("value", nargs="?")

    sp = mk("automix-preview", help="dry-run transition between two tracks")
    sp.add_argument("a")
    sp.add_argument("b")

    sp = mk("radio", help="build a Smart Shuffle sequence from a track")
    sp.add_argument("ref", nargs="?")
    sp.add_argument("--length", type=int, default=12)
    sp.add_argument("--no-enqueue", action="store_true")

    sp = mk("genq", help="Generate Queue: similar songs around the playing "
                         "track, queued to play next")
    sp.add_argument("ref", nargs="?")
    sp.add_argument("--length", type=int, default=5)
    sp.add_argument("--no-enqueue", action="store_true")

    sp = mk("discover", help="exploration shuffle across the library (spec §4)")
    sp.add_argument("seed", nargs="?")
    sp.add_argument("--length", type=int, default=12)
    sp.add_argument("--enqueue", action="store_true")
    sp.add_argument("--variety", type=float, default=0.5,
                    help="0..1, higher = wider picks")

    sp = mk("history", help="recently played")
    sp.add_argument("--limit", type=int, default=20)

    sp = mk("get", help="download from YouTube via yt-dlp (url, yt:<id>, ytpl:<id>, search:<query>)")
    sp.add_argument("url")
    sp.add_argument("--playlist", action="store_true",
                    help="force playlist mode (whole list, not just one video)")
    sp.add_argument("--workers", type=int, default=4,
                    help="parallel fragments per item (default 4)")
    sp.add_argument("--watch", action="store_true",
                    help="poll until no jobs are running, then return")
    sp.add_argument("--name", action="store_true",
                    help="show the resolved Artist — Song label for search refs")

    sp = mk("ytsearch", help="top YouTube hits for a song query (ranked: songs first)")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=5)

    sp = mk("downloads", help="download job status (poll queue/results)")

    sp = mk("lyrics")
    sp.add_argument("ref", nargs="?")
    sp = mk("cover")
    sp.add_argument("ref", nargs="?")

    sp = mk("daemon", help="start/stop/status/restart the background daemon")
    sp.add_argument("action", nargs="?", default="start",
                    choices=["start", "stop", "status", "restart"])

    mk("tui", help="launch the interactive TUI")
    mk("legal", help="YouTube/Apple download+DRM notices")

    sp = mk("completions", help="print shell-completion script (bash/zsh/fish)")
    sp.add_argument("shell", nargs="?", default="bash",
                    choices=["bash", "zsh", "fish"])

    args = p.parse_args(argv)

    if args.version:
        print(f"muse {__version__}")
        return 0
    if not args.cmd:
        p.print_help()
        return 0

    js = getattr(args, "json", False)
    c = args.cmd
    if c == "tui":
        from muse import tui
        return tui.run()
    if c == "legal":
        from muse.legal import full_text
        print(full_text())
        return 0
    if c == "daemon":
        return _daemon(args.action, js)
    if c == "completions":
        return _print_completions(args.shell)

    # map CLI -> ipc method/args
    if c == "play":
        if args.ref and args.ref.isdigit():
            args2 = {"track_id": int(args.ref)}
        else:
            args2 = {"query": args.ref} if args.ref else {}
        resp = _call("play", args2, js)
    elif c == "next":
        resp = _call("next", {"smart": not args.plain}, js)
    elif c == "queue":
        resp = _call("queue", {"action": args.action, "ref": args.ref,
                               "keep_first": getattr(args, "keep_first", False)}, js)
    elif c == "shuffle":
        resp = _call("queue", {"action": "shuffle",
                               "keep_first": getattr(args, "keep_first", False)}, js)
    elif c == "search":
        resp = _call("search", {"query": args.query, "provider": args.provider}, js)
    elif c == "library":
        resp = _call("library", {"artist": args.artist, "album": args.album,
                                 "kind": getattr(args, "kind", None)}, js)
    elif c == "import":
        resp = _call("import", {"path": args.path}, js)
    elif c == "analyze":
        resp = _call("analyze", {"all_pending": args.all,
                                 "track_id": int(args.ref) if args.ref and args.ref.isdigit() else None}, js)
    elif c == "playlist":
        resp = _call("playlist", {"action": args.action, "name": args.name,
                                  "ref": args.ref}, js)
    elif c in ("like", "unlike"):
        resp = _call("like", {"ref": args.ref, "like": c == "like"}, js)
    elif c == "volume":
        resp = _call("volume", {"value": args.value}, js)
    elif c == "seek":
        try:
            resp = _call("seek", {"seconds": float(args.seconds),
                                  "relative": args.seconds.startswith(("+", "-"))}, js)
        except ValueError:
            print(f"error: bad seek value: {args.seconds}", file=sys.stderr)
            return 2
    elif c == "stats":
        resp = _call("stats", {}, js)
    elif c == "history":
        resp = _call("history", {"limit": args.limit}, js)
    elif c == "discover":
        resp = _call("discover", {"seed": args.seed, "length": args.length,
                                  "enqueue": args.enqueue,
                                  "variety": args.variety}, js)
    elif c in ("pause", "resume", "toggle", "stop", "prev", "status"):
        resp = _call(c, {}, js)
    elif c == "automix":
        resp = _call("automix", {"action": args.action, "value": args.value}, js)
    elif c == "automix-preview":
        resp = _call("automix_preview", {"ref_a": args.a, "ref_b": args.b}, js)
    elif c == "radio":
        resp = _call("radio", {"ref": args.ref, "length": args.length,
                               "enqueue": not args.no_enqueue}, js)
    elif c == "genq":
        resp = _call("genq", {"ref": args.ref, "length": args.length,
                              "enqueue": not args.no_enqueue}, js)
    elif c == "get":
        resp = _call("yt_get", {"ref": args.url, "playlist": args.playlist,
                                "workers": args.workers}, js)
        if _fail(resp):
            return 1
        if args.name and args.url.startswith(("search:", "ytsearch:")):
            print(f"resolved: {resp.get('label') or args.url}")
        if args.watch:
            resp = _cli_get_watch(js) or resp
        if js:
            print(json.dumps(resp, indent=2, default=str))
            return 0
        if args.watch and resp.get("jobs"):
            _print_human("downloads", resp, args)
        else:
            print(f"download job #{resp.get('job')} queued ({resp.get('kind')}) — "
                  "poll with: muse downloads")
            print(resp.get("notice", ""))
        return 0
    elif c == "downloads":
        resp = _call("downloads", {}, js)
    elif c == "ytsearch":
        resp = _call("ytsearch", {"query": args.query, "limit": args.limit}, js)
    elif c == "lyrics":
        resp = _call("lyrics", {"ref": args.ref}, js)
    elif c == "cover":
        resp = _call("cover", {"ref": args.ref}, js)
    else:
        p.print_help()
        return 2

    if _fail(resp):
        return 1
    if js:
        return 0  # already printed
    _print_human(c, resp, args)
    return 0


def _looks_songish(raw_title: str) -> bool:
    """Heuristic display tag: a title without video-noise words is a song."""
    from muse.providers.youtube import _LIVEISH_RE, _NOISE_RE
    raw = (raw_title or "").lower()
    return not (_NOISE_RE.search(raw) or _LIVEISH_RE.search(raw))


def _cli_get_watch(as_json: bool) -> dict:
    """Poll `downloads` until no jobs are running; final snapshot is returned"""
    last: dict = {}
    try:
        while True:
            last = _call("downloads", {})
            running = [j for j in last.get("jobs", []) if j.get("state") == "running"]
            if not running:
                break
            for j in running:
                p = j.get("progress") or ""
                if p:
                    print(f"  #{j['id']} {p}", flush=True)
            time.sleep(2.0)
    except KeyboardInterrupt:
        print("(detached — job keeps running; check `muse downloads`)", flush=True)
    return last


def _print_human(c: str, resp: dict, args) -> None:
    if c == "status":
        for k in ("state", "title", "artist", "position", "duration", "volume"):
            if k in resp:
                print(f"{k}: {resp[k]}")
        return
    if c == "search":
        for i, r in enumerate(resp.get("results", []), 1):
            dur = _fmt_seconds(r.get("duration"))
            album = f" [{r.get('album')}]" if r.get("album") else ""
            print(f"{i:3d}. {r.get('artist') or '?'} — {r.get('title')} "
                  f"{album} ({dur}, {r.get('provider')}) id={r.get('id') or '-'}")
        return
    if c == "library":
        kind = getattr(args, "kind", None)
        items = resp.get(kind or "tracks", [])
        for r in items:
            if kind == "artists":
                print(f"{r['id']:5d}. {r['name']}  ({r.get('n_tracks', 0)} tracks)")
            elif kind == "albums":
                print(f"{r['id']:5d}. {r.get('artist') or '?'} — {r['name']}  "
                      f"({r.get('n_tracks', 0)} tracks)")
            else:
                dur = _fmt_seconds(r.get("duration"))
                genre = f" [{r.get('genre')}]" if r.get("genre") else ""
                print(f"{r['id']:5d}. {r.get('artist') or '?'} — {r['title']} "
                      f"{genre} [{r.get('album') or '-'}] {dur} {r.get('provider')}")
        return
    if c == "queue":
        for q in resp.get("queue", []):
            dur = _fmt_seconds(q.get("duration"))
            genre = f" [{q.get('genre')}]" if q.get("genre") else ""
            print(f"{q.get('position', '?'):>4}. {q.get('artist') or '?'} — "
                  f"{q.get('title')}{genre} {dur} {q.get('provider')}")
        if resp.get("shuffled"):
            print(f"shuffled {resp['shuffled']} track(s)")
        return
    if c == "shuffle":
        for q in resp.get("queue", []):
            dur = _fmt_seconds(q.get("duration"))
            print(f"{q.get('position', '?'):>4}. {q.get('artist') or '?'} — "
                  f"{q.get('title')} {dur} {q.get('provider')}")
        return
    if c == "automix_preview":
        print(resp.get("text", ""))
        return
    if c == "radio":
        for i, t in enumerate(resp.get("radio", []), 1):
            score = t.get("transition_score")
            sc = f" (score {score:g})" if score else ""
            print(f"{i:3d}. {t.get('artist') or '?'} — {t['title']}{sc}")
        return
    if c == "genq":
        picks = resp.get("generated", [])
        if not picks:
            print("no similar tracks found (needs analyzed library)")
            return
        for i, t in enumerate(picks, 1):
            score = t.get("transition_score")
            sc = f" (score {score:g})" if score else ""
            print(f"{i:3d}. {t.get('artist') or '?'} — {t['title']}{sc}")
        print("queued to play next")
        return
    if c == "discover":
        for i, t in enumerate(resp.get("discover", []), 1):
            print(f"{i:3d}. {t.get('artist') or '?'} — {t['title']}")
        return
    if c == "history":
        for i, t in enumerate(resp.get("history", []), 1):
            print(f"{i:3d}. {t.get('artist') or '?'} — {t['title']}")
        return
    if c == "stats":
        lib, top = resp.get("library", {}), resp.get("top", [])
        print(f"tracks {lib.get('tracks', 0)} · queue {lib.get('queue', 0)} · "
              f"history {lib.get('history', 0)} · analyzed {lib.get('analyzed', 0)}")
        for i, t in enumerate(top, 1):
            print(f"{i:3d}. {t.get('artist') or '?'} — {t['title']} "
                  f"({t.get('play_count', 0)} plays)")
        return
    if c == "seek":
        print(f"position: {resp.get('position', '?')}s / {resp.get('duration') or '??:??'}s")
        return
    if c == "lyrics":
        print(resp.get("lyrics") or "(no lyrics found)")
        return
    if c == "cover":
        path = resp.get("cover")
        print(path or "(no cover)")
        return
    if c == "downloads":
        jobs = resp.get("jobs", [])
        if not jobs:
            print("no download jobs")
            return
        for j in jobs:
            state = j.get("state", "?")
            icon = {"running": "⏳", "done": "✓", "failed": "✗"}.get(state, "·")
            line = f"{icon} #{j['id']} [{state}] {j.get('kind', '?')}: {j.get('label', '')}"
            print(line)
            if j.get("error"):
                print(f"    error: {j['error']}")
            elif j.get("progress") and state != "done":
                print(f"    {j['progress']}")
            elif j.get("track_ids"):
                print(f"    imported track id(s): {', '.join(map(str, j['track_ids']))}")
        return
    if c == "ytsearch":
        for i, r in enumerate(resp.get("candidates", []), 1):
            dur = _fmt_seconds(r.get("duration"))
            kind = "song" if _looks_songish(r.get("raw_title") or "") else "video"
            print(f"{i:3d}. {r.get('artist') or '?'} — {r.get('title')} "
                  f"({dur}, youtube, {kind})")
            print(f"      muse get {r.get('url')}")
        return
    if c == "play":
        # compact
        for k in ("playing", "duration"):
            if k in resp:
                print(f"{k}: {resp[k]}")
        return
    # default: print simple scalar dict
    for k, v in resp.items():
        if k not in ("ok",):
            print(f"{k}: {v}")


def _print_completions(shell: str) -> int:
    """Print a completion script (spec §4: shell-completion). Static command
    list; per-argument values (track ids, playlist names) are not completed."""
    cmds = ("play pause resume toggle stop next prev status stats seek queue "
            "shuffle search library import analyze playlist like unlike volume "
            "automix automix-preview radio genq discover history get downloads "
            "ytsearch lyrics cover daemon tui legal completions")
    if shell == "bash":
        print(f"""# muse bash completion
_muse_completions() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}"
  case "${{COMP_WORDS[1]}}" in
    queue) COMPREPLY=( $(compgen -W "list add remove shuffle clear" -- "$cur") );;
    get) COMPREPLY=( $(compgen -W "--playlist --workers --watch" -- "$cur") );;
    playlist) COMPREPLY=( $(compgen -W "list create delete add show play" -- "$cur") );;
    automix) COMPREPLY=( $(compgen -W "on off length config bandpass vocal" -- "$cur") );;
    daemon) COMPREPLY=( $(compgen -W "start stop status restart" -- "$cur") );;
    completions) COMPREPLY=( $(compgen -W "bash zsh fish" -- "$cur") );;
    library) COMPREPLY=( $(compgen -W "--kind --artist --album" -- "$cur") );;
    downloads) COMPREPLY=();;
    *)
      if [ "$COMP_CWORD" = 1 ]; then
        COMPREPLY=( $(compgen -W "{cmds}" -- "$cur") )
      fi
      ;;
  esac
}}
complete -F _muse_completions muse""")
    elif shell == "zsh":
        print(f"""#compdef muse
_muse() {{
  local -a commands
  commands=({' '.join(cmds.split())})
  _describe 'command' commands
  case $words[2] in
    (queue) _values 'action' list add remove shuffle clear;;
    (playlist) _values 'action' list create delete add show play;;
    (automix) _values 'action' on off length config bandpass vocal;;
    (daemon) _values 'action' start stop status restart;;
  esac
}}
compdef _muse muse""")
    else:  # fish
        for c in cmds.split():
            print(f"complete -c muse -n '__fish_use_subcommand' -a '{c}'")
        print("""complete -c muse -n '__fish_seen_subcommand_from queue' -a 'list add remove shuffle clear'
complete -c muse -n '__fish_seen_subcommand_from automix' -a 'on off length config bandpass vocal'
complete -c muse -n '__fish_seen_subcommand_from daemon' -a 'start stop status restart'
complete -c muse -n '__fish_seen_subcommand_from playlist' -a 'list create delete add show play'""")
    return 0


def _daemon(action: str, as_json: bool) -> int:
    if action == "status":
        running = ipc.daemon_running()
        print("running" if running else "not running")
        return 0
    if action == "stop":
        if not ipc.daemon_running():
            print("not running")
            return 0
        resp = ipc.request("daemon_stop", timeout=10.0)
        if resp.get("ok"):
            # wait for the socket to disappear (clean shutdown)
            import time as _t
            for _ in range(30):
                if not ipc.daemon_running():
                    break
                _t.sleep(0.2)
            else:
                # graceful path failed; SIGTERM the process as a fallback
                _terminate_daemon()
                return 0
            print("daemon stopped")
            return 0
        print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
        return 1
    if action == "restart":
        rc = _daemon("stop", as_json)
        if rc:
            return rc
        return _daemon("start", as_json)
    if action == "start":
        if ipc.daemon_running():
            print("daemon already running")
            return 0
        import subprocess as _sp
        env = dict(os.environ)
        log_file = os.path.join(str(__import__("pathlib").Path.home()), ".cache/muse/daemon.log")
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        with open(log_file, "ab") as lf:
            proc = _sp.Popen(
                [sys.executable, "-m", "muse.daemon.main"],
                env=env, stdout=lf, stderr=lf, start_new_session=True)
        time.sleep(1.0)
        if ipc.daemon_running():
            print(f"daemon started (pid {proc.pid}, log {log_file})")
            return 0
        print("daemon failed to start; check log", file=sys.stderr)
        return 1
    print("usage: muse daemon [start|stop|status|restart]")
    return 2


def _terminate_daemon() -> None:
    """Best-effort SIGTERM to muse daemon processes (fallback stop path)."""
    import signal
    import subprocess as _sp
    try:
        out = _sp.run(["pgrep", "-f", "muse.daemon.main"], capture_output=True,
                      text=True, timeout=5).stdout.split()
        for pid in out:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())