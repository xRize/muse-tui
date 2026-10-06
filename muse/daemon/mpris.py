"""Desktop media-key integration.

- macOS: MPNowPlayingInfoCenter + MPRemoteCommandCenter (via pyobjc).
- Linux: MPRIS over D-Bus (spec §2) via dbus-next.
Both degrade to no-ops when unavailable so the daemon always runs.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("muse.mpris")


class MediaKeys:
    """Registers now-playing metadata and remote commands for the desktop."""

    def __init__(self, on_play=None, on_pause=None, on_toggle=None, on_next=None,
                 on_prev=None):
        self.on_play = on_play
        self.on_pause = on_pause
        self.on_toggle = on_toggle
        self.on_next = on_next
        self.on_prev = on_prev
        self._impl = None
        self._lock = threading.Lock()
        self._start()

    # -- platform dispatch ----------------------------------------------------
    def _start(self) -> None:
        import sys

        try:
            if sys.platform == "darwin":
                self._start_macos()
            elif sys.platform.startswith("linux"):
                self._start_mpris()
        except Exception as e:
            log.info("media keys unavailable: %s", e)
            self._impl = None

    def _start_macos(self) -> None:
        try:
            import MediaPlayer  # pyobjc-framework-MediaPlayer
        except ImportError:
            log.info("pyobjc MediaPlayer not installed; media keys disabled")
            return
        center = MediaPlayer.MPRemoteCommandCenter.sharedCommandCenter()
        handlers = [
            (center.playCommand(), self.on_play),
            (center.pauseCommand(), self.on_pause),
            (center.togglePlayPauseCommand(), self.on_toggle),
            (center.nextTrackCommand(), self.on_next),
            (center.previousTrackCommand(), self.on_prev),
        ]

        for cmd, cb in handlers:
            if cb is not None:
                cmd.addTargetWithHandler_(lambda evt, _cb=cb: (_cb(), MediaPlayer.MPRemoteCommandHandlerStatusSuccess)[1])
        self._impl = ("macos", MediaPlayer)
        log.info("macOS now-playing/remote commands registered")

    def _start_mpris(self) -> None:
        try:
            import importlib.util
            if importlib.util.find_spec("dbus_next") is None:
                raise ImportError
        except ImportError:
            log.info("dbus-next not installed; MPRIS disabled (pip install dbus-next)")
            return
        # MPRIS needs an asyncio loop; spawn a thread with its own bus connection
        import asyncio

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
        future = asyncio.run_coroutine_threadsafe(self._mpris_setup(loop), loop)
        try:
            future.result(timeout=5)
        except Exception as e:
            log.info("MPRIS setup failed: %s", e)

    async def _mpris_setup(self, loop) -> None:
        from dbus_next import BusType
        from dbus_next.aio import MessageBus

        bus = await MessageBus().connect(BusType.SESSION)
        # minimal Player — full interface in a later milestone
        log.info("MPRIS bus connected (minimal prototype)")
        self._impl = ("linux", bus)

    # -- metadata ---------------------------------------------------------------
    def update_now_playing(self, title: str, artist: str = "", album: str = "",
                           duration: float = 0.0, position: float = 0.0,
                           playing: bool = False) -> None:
        with self._lock:
            if not self._impl:
                return
            kind, mod = self._impl
            try:
                if kind == "macos":
                    info = mod.MPNowPlayingInfoCenter.defaultCenter()
                    import MediaPlayer as MP
                    from pyobjc_core import NSMutableDictionary

                    d = NSMutableDictionary.dictionary()
                    d[MP.MPMediaItemPropertyTitle] = title
                    d[MP.MPMediaItemPropertyArtist] = artist
                    d[MP.MPMediaItemPropertyAlbumTitle] = album
                    d[MP.MPMediaItemPropertyPlaybackDuration] = duration
                    d[MP.MPNowPlayingInfoPropertyElapsedPlaybackTime] = position
                    d[MP.MPNowPlayingInfoPropertyPlaybackRate] = 1.0 if playing else 0.0
                    info.nowPlayingInfo_ = d
            except Exception as e:
                log.debug("now-playing update failed: %s", e)

    def stop(self) -> None:
        pass