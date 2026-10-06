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

    sp = mk("queue", help="list/add/remove/clear queue")
    sp.add_argument("action", nargs="?", default="list")
    sp.add_argument("ref", nargs="?")

    sp = mk("search", help="search providers (local by default)")
    sp.add_argument("query")
    sp.add_argument("--provider", default="local", choices=["local", "youtube", "all"])

    sp = mk("library", help="list library")
    sp.add_argument("--artist")
    sp.add_argument("--album")

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

    sp = mk("get", help="download from YouTube via yt-dlp")
    sp.add_argument("url")

    sp = mk("lyrics")
    sp.add_argument("ref", nargs="?")
    sp = mk("cover")
    sp.add_argument("ref", nargs="?")

    sp = mk("daemon", help="start/stop the background daemon")
    sp.add_argument("action", nargs="?", default="start")

    mk("tui", help="launch the interactive TUI")
    mk("legal", help="YouTube/Apple download+DRM notices")

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
        resp = _call("queue", {"action": args.action, "ref": args.ref}, js)
    elif c == "search":
        resp = _call("search", {"query": args.query, "provider": args.provider}, js)
    elif c == "library":
        resp = _call("library", {"artist": args.artist, "album": args.album}, js)
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
    elif c in ("pause", "resume", "toggle", "stop", "prev", "status"):
        resp = _call(c, {}, js)
    elif c == "automix":
        resp = _call("automix", {"action": args.action, "value": args.value}, js)
    elif c == "automix-preview":
        resp = _call("automix_preview", {"ref_a": args.a, "ref_b": args.b}, js)
    elif c == "radio":
        resp = _call("radio", {"ref": args.ref, "length": args.length,
                               "enqueue": not args.no_enqueue}, js)
    elif c == "get":
        resp = _call("get", {"url": args.url}, js)
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
    if c in ("library",):
        for r in resp.get("tracks", []):
            dur = _fmt_seconds(r.get("duration"))
            print(f"{r['id']:5d}. {r.get('artist') or '?'} — {r['title']} "
                  f"[{r.get('album') or '-'}] {dur} {r.get('provider')}")
        return
    if c == "queue":
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
    if c == "lyrics":
        print(resp.get("lyrics") or "(no lyrics found)")
        return
    if c == "cover":
        path = resp.get("cover")
        print(path or "(no cover)")
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


def _daemon(action: str, as_json: bool) -> int:
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
    if action == "stop":
        resp = _call("stop")
        ok = not _fail(resp)
        # best-effort: kill by socket peer? prototype: signal via pid file
        print("stopped playback; full daemon shutdown: kill the process manually")
        return 0 if ok else 1
    print("usage: muse daemon [start|stop]")
    return 2


if __name__ == "__main__":
    sys.exit(main())