#!/usr/bin/env bash
# muse installer — venv + editable install + optional global `muse` symlink.
# Usage: ./scripts/install.sh [--no-dev] [--no-symlink] [--bin-dir DIR] [--help]
set -euo pipefail

NO_DEV=0
NO_SYMLINK=0
BIN_DIR="${HOME}/.local/bin"

usage() {
  sed -n '2,5p' "$0"
  cat <<'EOF'
Options:
  --no-dev          skip dev/test dependencies (pytest, ruff)
  --no-symlink      do not symlink `muse` into a PATH dir
  --bin-dir DIR     symlink target directory (default: ~/.local/bin)
  --help            show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --no-dev) NO_DEV=1; shift;;
    --no-symlink) NO_SYMLINK=1; shift;;
    --bin-dir) BIN_DIR="$2"; shift 2;;
    --help|-h) usage; exit 0;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"

banner() {
  cat <<'ART'
     __  __        _
 /|/ __/ /__ __(_)__  _______  __ _
/ |/ _/ (/ // //_)/ |/ / __/ |/ /
|/__/ \__, //_/  (_)___/\__/|___/
     |___/
ART
}

banner

# ---- system dependencies ----------------------------------------------------
command -v python3 >/dev/null 2>&1 || {
  echo "error: python3 not found on PATH" >&2
  echo "  macOS:  brew install python" >&2
  echo "  Debian: sudo apt install python3 python3-venv" >&2
  exit 1
}

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "error: Python >= 3.10 required, found $(python3 --version 2>&1)" >&2
  exit 1
fi
echo "python:  $(python3 --version 2>&1)"

if command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg:  $(ffmpeg -version 2>/dev/null | head -1)"
else
  echo "error: ffmpeg not found on PATH (required for playback + analysis)" >&2
  echo "  macOS:  brew install ffmpeg" >&2
  echo "  Debian: sudo apt install ffmpeg" >&2
  exit 1
fi

if command -v yt-dlp >/dev/null 2>&1; then
  echo "yt-dlp:  $(command -v yt-dlp) (optional: downloads enabled)"
else
  echo "yt-dlp:  not found (optional — install with: pip install yt-dlp)"
fi

# ---- virtualenv + install ---------------------------------------------------
if [ ! -x "$PY" ]; then
  echo "creating virtualenv at $VENV"
  python3 -m venv "$VENV"
fi
"$PY" -m pip install --quiet --upgrade pip setuptools wheel
case "$(uname -s)" in
  Darwin) DEP_EXTRAS="macos";;
  Linux)  DEP_EXTRAS="linux";;
  *)      DEP_EXTRAS="";;
esac
if [ "$NO_DEV" -eq 1 ]; then
  EXTRAS="$DEP_EXTRAS"
elif [ -n "$DEP_EXTRAS" ]; then
  EXTRAS="dev,$DEP_EXTRAS"
else
  EXTRAS="dev"
fi
echo "installing: pip install -e '.[$EXTRAS]'"
"$PY" -m pip install --quiet -e ".[$EXTRAS]"

# ---- smoke check ------------------------------------------------------------
# Console script (NOT `python -m muse` — no __main__.py, and -m from this
# repo root would hit the muse/ directory namespace shadow).
VERSION_OUT="$("$VENV/bin/muse" --version 2>&1)" \
  && echo "smoke:   $VERSION_OUT" \
  || { echo "install smoke check failed: $VERSION_OUT" >&2; exit 1; }

# ---- global symlink ---------------------------------------------------------
if [ "$NO_SYMLINK" -eq 0 ]; then
  mkdir -p "$BIN_DIR"
  ln -sf "$VENV/bin/muse" "$BIN_DIR/muse"
  echo "symlink: $BIN_DIR/muse -> $VENV/bin/muse"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note:     $BIN_DIR is not on your PATH — add it to your shell rc:" \
       ; echo "          echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc" ;;
  esac
fi

cat <<'NEXT'

muse installed. Next steps:
  muse daemon start          # background daemon (IPC + media keys)
  muse scan ~/Music --no-analyze   # import files, no analysis yet
  muse scan ~/Music          # import + BPM/key analysis in one go
  muse analyze --all         # analyze everything still pending
  muse tui                   # full-screen interface
NEXT