"""Legal notices surfaced in the app and the `muse legal` command (spec §6)."""
from __future__ import annotations

YOUTUBE_NOTICE = """\
YouTube downloads (muse get)
  Downloads are performed by yt-dlp (MIT-licensed third-party tool).
  muse neither bundles nor enables DRM circumvention; DRM-protected streams
  are skipped by yt-dlp itself.
  You are responsible for complying with YouTube's Terms of Service and
  applicable copyright law. Downloads are suitable for personal, archival or
  licensed use only. Do not redistribute downloaded media.
"""

APPLE_NOTICE = """\
Apple Music (muse)
  Apple Music catalogue content is DRM-protected. muse does not decrypt,
  extract or otherwise circumvent DRM; Apple tracks are synced/queued as
  metadata only and cannot be played as local audio. Any MusicKit integration
  uses Apple's official APIs and requires the user's own developer token and
  an Apple Music subscription for playback; Apple's developer terms apply.
"""

LICENSE_NOTICE = """\
License
  muse code: MIT. Third-party runtime dependencies keep their own licenses
  (numpy/sounddevice/mutagen/yt-dlp: permissive; audio decoding is performed
  by the user's installed ffmpeg binary).
"""


def full_text() -> str:
    return YOUTUBE_NOTICE + "\n" + APPLE_NOTICE + "\n" + LICENSE_NOTICE