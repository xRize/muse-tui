# muse

Linux/macOS-native terminal music client: a background **daemon** owning
playback, library and analysis, with **CLI** and **TUI** clients over a
Unix-socket JSON API.

Implements the core of the muse spec (see conversation): playback with gapless
advance and loudness normalization, **Smart Shuffle** (BPM/key/energy scoring,
no repeats), **AutoMix** (beat-aligned crossfade planning + `atempo`
transition via ffmpeg), offline **analysis** (BPM / key / LUFS / beat grid /
sections), **yt-dlp** downloads with legal notices, lyrics + cover art
fetching/caching, macOS media keys / Linux MPRIS integration, and an Apple
Music provider scaffold that honestly reports DRM limitations instead of
circumventing them.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e .[dev]
# optional desktop integration:
.venv/bin/pip install -e .[macos]   # macOS media keys (pyobjc)
.venv/bin/pip install -e .[linux]   # Linux MPRIS (dbus-next)
```

Requires `ffmpeg` (decoding/analysis) and optionally `yt-dlp` (downloads) on
your PATH.

## Quickstart

```bash
muse daemon start                  # background daemon (IPC socket + media keys)
muse import ~/Music/mine           # import files into the library
muse analyze --all                 # offline analysis (BPM/key/LUFS/beat grid)
muse search "creed"                # search local library
muse play 3                        # play track 3 (or: muse play "creed")
muse queue add 7                   # enqueue
muse next                          # advance (Smart Shuffle when queue is empty)
muse volume 75                     # percent volume
muse automix on && muse automix length 8
muse automix-preview 1 2           # dry-run transition plan (no audio)
muse radio 5 --length 10           # build a Smart Shuffle sequence
muse lyrics && muse cover          # now-playing extras
muse status                        # now playing
muse legal                         # YouTube / Apple notices
muse tui                           # full-screen interface
```

`--json` on any command emits raw JSON for scripting.

## Architecture

```
muse/daemon     IPC socket server + command registry (single source of truth)
muse/audio      ffmpeg-decoder voices -> sounddevice stereo mixer
                (fade state machine = gapless advance / AutoMix crossfade)
muse/analysis   numpy DSP: onset autocorrelation BPM, KS-profile key,
                R128-style loudness, beat grid, sections; tiered worker
muse/smart      Smart Shuffle scorer + AutoMix transition planner
muse/providers  local | youtube (yt-dlp) | apple (MusicKit scaffold)
                | lyrics (LRCLIB) | covers (Deezer -> generated fallback)
```

State: SQLite at `~/.local/share/muse/database.db`; config
`~/.config/muse/config.toml`; analysis cache `~/.cache/muse/analysis`.

## Offline / CI mode

Set `MUSE_OFFLINE=1` — no network calls (search, downloads, lyrics, covers all
degrade gracefully). Synthetic test signals (`tests/conftest.py`,
`muse/analysis/synth.py`) make the whole test suite deterministic and
network-free: `muse analyze` and AutoMix planning are validated against
click-tracks at known BPM and chord pads in known keys.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

## Known prototype limits

- Apple Music playback is intentionally unimplemented (DRM); provider syncs
  metadata and reports why playback can't happen.
- Smart Shuffle pool is library-local; artist/genre embeddings are future work.
- MPRIS is registered as a minimal prototype (play/pause/next only).
- `atempo` time-stretch is applied during AutoMix, limited to ±4% adjustments.