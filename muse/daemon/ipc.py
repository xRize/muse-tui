"""Unix-domain JSON-RPC socket server for the daemon (spec §2)."""
from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import threading

log = logging.getLogger("muse.ipc")

SOCKET_NAME = "muse.sock"


def socket_path() -> str:
    import tempfile
    return os.path.join(tempfile.gettempdir(), SOCKET_NAME)


class JSONRPCServer(socketserver.StreamRequestHandler):
    """One request per connection: read JSON line, dispatch, reply JSON line."""

    def handle(self) -> None:
        try:
            line = self.rfile.readline()
            if not line:
                return
            req = json.loads(line.decode())
            method = req.get("method", "")
            args = req.get("args", {}) or {}
            from muse.daemon.commands import handle
            resp = handle(method, args)
            resp.setdefault("ok", "error" not in resp)
        except Exception as e:
            log.exception("ipc error")
            resp = {"ok": False, "error": str(e)}
        try:
            self.wfile.write(json.dumps(resp).encode() + b"\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


class IPCServer(socketserver.ThreadingUnixStreamServer):
    allow_reuse_address = True
    daemon_threads = True


def start_ipc(socket_file: str | None = None) -> IPCServer:
    path = socket_file or socket_path()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    srv = IPCServer((path), JSONRPCServer)
    t = threading.Thread(target=srv.serve_forever, name="muse-ipc", daemon=True)
    t.start()
    log.info("ipc listening on %s", path)
    return srv


def request(method: str, args: dict | None = None, timeout: float = 10.0,
            path: str | None = None) -> dict:
    """Client-side call. Returns {'ok': bool, ...reply}."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path or socket_path())
            payload = json.dumps({"method": method, "args": args or {}}).encode() + b"\n"
            s.sendall(payload)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        if not buf:
            return {"ok": False, "error": "empty response"}
        resp = json.loads(buf.decode())
        resp.setdefault("ok", "error" not in resp)
        return resp
    except (TimeoutError, FileNotFoundError, ConnectionRefusedError, OSError):
        return {"ok": False, "error": "daemon not running (start with: muse daemon start)"}


def daemon_running(path: str | None = None) -> bool:
    return request("version", timeout=2.0, path=path).get("ok", False)