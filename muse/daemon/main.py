"""muse daemon entrypoint: owns engine + DB + IPC + media keys, long-running."""
from __future__ import annotations

import logging
import os
import signal
import sys
import time

from muse import paths
from muse.daemon import commands as commands_mod
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


def apply_config(cmds) -> None:
    """Seed runtime settings from config.toml (spec §2 file layout).

    DB settings win only as defaults: config.toml values are applied on every
    daemon start so editing the file takes effect after `daemon restart`.
    """
    try:
        cfg = paths.read_config()
    except Exception:
        cfg = {}
    db = cmds.db()
    audio_cfg = cfg.get("audio", {}) or {}
    if "volume" in audio_cfg:
        try:
            v = float(audio_cfg["volume"])
            db.set_setting("volume", f"{min(1.0, max(0.0, v if v <= 1 else v / 100)):g}")
        except (TypeError, ValueError):
            pass
    am = cfg.get("automix", {}) or {}
    if "enabled" in am:
        db.set_setting("automix_enabled", "1" if am["enabled"] else "0")
    if "transition_length" in am:
        try:
            db.set_setting("automix_transition_length",
                           str(max(2.0, min(16.0, float(am["transition_length"])))))
        except (TypeError, ValueError):
            pass
    if "beat_match" in am:
        db.set_setting("automix_beat_match", "1" if am["beat_match"] else "0")
    if "harmonic_match" in am:
        db.set_setting("automix_harmonic_match", "1" if am["harmonic_match"] else "0")
    sh = cfg.get("shuffle", {}) or {}
    if "smart" in sh:
        db.set_setting("shuffle_smart", "1" if sh["smart"] else "0")


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
    apply_config(cmds)
    ipc.start_ipc(socket_file)

    media = MediaKeys(
        on_toggle=cmds.cmd_toggle,
        on_next=cmds.cmd_next,
        on_prev=cmds.cmd_prev,
    )
    cmds.daemon = type("DaemonCtx", (), {"media": media})()

    stop = False

    def _sig(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    log.info("muse daemon ready")
    try:
        while not stop and not commands_mod.exit_requested():
            time.sleep(0.5)
            try:
                cmds.service_tick()
            except Exception:
                log.exception("service tick failed")
    except KeyboardInterrupt:
        pass
    finally:
        media.stop()
        if cmds.engine:
            cmds.engine.stop()
        if cmds.database:
            cmds.database.close()
        log.info("daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())