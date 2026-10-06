"""XDG-based file layout (spec §2): config, data, cache."""
from __future__ import annotations

import os
from pathlib import Path


def _xdg(env_var: str, default: Path) -> Path:
    base = os.environ.get(env_var)
    if base:
        return Path(base).expanduser()
    return Path.home() / default


def config_dir() -> Path:
    d = _xdg("MUSE_CONFIG_DIR", Path(".config/muse"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_dir() -> Path:
    d = _xdg("MUSE_DATA_DIR", Path(".local/share/muse"))
    (d / "covers").mkdir(parents=True, exist_ok=True)
    (d / "lyrics").mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    d = _xdg("MUSE_CACHE_DIR", Path(".cache/muse"))
    (d / "analysis").mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "config.toml"


def db_path() -> Path:
    return data_dir() / "database.db"


def covers_dir() -> Path:
    return data_dir() / "covers"


def lyrics_dir() -> Path:
    return data_dir() / "lyrics"


def analysis_dir() -> Path:
    return cache_dir() / "analysis"


def download_dir() -> Path:
    override = os.environ.get("MUSE_DOWNLOAD_DIR")
    if override:
        d = Path(override).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        return d
    from tomllib import TOMLDecodeError
    try:
        import tomllib
        with open(config_path(), "rb") as f:
            cfg = tomllib.load(f)
        raw = cfg.get("download", {}).get("path", "~/Music/muse")
    except (FileNotFoundError, TOMLDecodeError):
        raw = "~/Music/muse"
    d = Path(raw).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def library_dir() -> Path:
    """Managed library copy target for imports (XDG data, inside backups)."""
    d = data_dir() / "library"
    d.mkdir(parents=True, exist_ok=True)
    return d


def offline() -> bool:
    return os.environ.get("MUSE_OFFLINE", "0") not in ("", "0", "false", "no")


def read_config() -> dict:
    """Parse ~/.config/muse/config.toml; {} on absence or parse errors."""
    try:
        import tomllib
        with open(config_path(), "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


DEFAULT_CONFIG = """\
[audio]
backend = "sounddevice"
volume = 0.8
gapless = true
replaygain = "album"   # "track" | "album" | "off"

[download]
path = "~/Music/muse"
yt_dlp_opts = ["--extract-audio", "--audio-format", "mp3"]

[automix]
enabled = true
transition_length = 8  # seconds
beat_match = true
harmonic_match = true

[shuffle]
smart = true
pool_size = 50
"""