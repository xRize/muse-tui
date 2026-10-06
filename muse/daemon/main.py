"""muse daemon entrypoint: owns engine + DB + IPC + media keys, long-running."""
from __future__ import annotations

import logging
import os
import signal
import sys
import time

from muse import paths
from muse.daemon import ipc
from muse.daemon.commands import enable_shared_registry
from muse.daemon.mpris import MediaKeys

log = logging.getLogger("muse.daemon")


def setup_logging() -> None:
    level = os.environ.get("MUSE_LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def main() -> int:
    setup_logging()
    socket_file = os.environ.get("MUSE_SOCKET")

    # singleton guard: probe the exact socket we intend to own
    probe = ipc.daemon_running(socket_file) if socket_file else ipc.daemon_running()
    if probe:
        print("daemon already running", file=sys.stderr)
        return 1

    cmds = enable_shared_registry()
    cmds.audio_engine()      # start engine now (not lazily on first play)
    cmds.db()
    # first-run: write default config.toml if absent (spec §2)
    try:
        cfg = paths.config_path()
        if not cfg.exists():
            cfg.write_text(paths.DEFAULT_CONFIG)
    except OSError as e:
        log.warning("config bootstrap failed: %s", e)
    ipc.start_ipc(socket_file)

    media = MediaKeys(
        on_toggle=cmds.cmd_toggle,
        on_next=cmds.cmd_next,
        on_prev=cmds.cmd_prev,
    )

    stop = False

    def _sig(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    log.info("muse daemon ready")
    try:
        while not stop:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        media.stop()
        if cmds.engine:
            cmds.engine.stop()
        cmds.database.close() if cmds.database else None
        log.info("daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())