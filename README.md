# muse

Linux/macOS-native terminal music client: a background **daemon** owning
playback, library and analysis, with **CLI** and **TUI** clients over a
Unix-socket JSON API.

Implements the core of the muse spec (see conversation): playback with gapless
advance and loudness normalization, **Smart Shuffle** (BPM/key/energy scoring,
no repeats), **AutoMix** (beat-aligned crossfade planning + `atempo`
transition via ffmpeg, with band-limited and vocal-aware modes), offline
**analysis** (BPM / key / LUFS / beat grid / sections + Tier-2 **vocal-region
detection**), **yt-dlp** downloads with legal notices, lyrics + cover art
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

Optional — make `muse` available on your global PATH (the symlink keeps
working from any directory; repeat after moving the project):

```bash
ln -sf "$(pwd)/.venv/bin/muse" /opt/homebrew/bin/muse   # macOS (Homebrew)
```

## Quickstart

```bash
muse daemon start                  # background daemon (IPC socket + media keys)
muse import ~/Music/mine           # import files into the library
muse analyze --all                 # offline analysis (BPM/key/LUFS/beat grid)
muse search "creed"                # search local library
muse play 3                        # play track 3 (or: muse play "creed")
muse seek 30                       # jump to 0:30 (+10 / -5 = relative seek)
muse next                          # advance (Smart Shuffle when queue is empty)
muse next --plain                  # advance without Smart Shuffle
muse prev                          # restart track, or previous track if <3s in
muse queue add 7                   # enqueue
muse shuffle                       # shuffle the queue (alias: queue shuffle)
muse shuffle --keep-first          # ... keeping the playing track first
muse volume 75                     # percent volume
muse automix on && muse automix length 8
muse automix bandpass off          # band-limited crossfade filter (default on)
muse automix vocal on              # vocal-aware transitions (default off)
muse automix-preview 1 2           # dry-run transition plan (no audio)
muse radio 5 --length 10           # build a Smart Shuffle sequence
muse discover                      # exploration shuffle across the library
muse discover 5 --variety 0.8      # ... seeded, wider picks, --enqueue to queue
muse history --limit 10            # recently played
muse stats                         # library/play-count summary + top tracks
muse library --kind artists        # group view (artists|albums)
muse lyrics && muse cover          # now-playing extras
muse status                        # now playing
muse daemon status|stop|restart    # daemon lifecycle
muse completions bash              # shell completions (bash|zsh|fish)
muse legal                         # YouTube / Apple notices
muse tui                           # full-screen interface
```

Shell completions (pipe to your rc file or eval in-session):

```bash
muse completions bash  > ~/.muse-completion.bash && source ~/.muse-completion.bash
muse completions zsh   > ~/.zfunc/_muse
muse completions fish  > ~/.config/fish/completions/muse.fish
```

`--json` on any command emits raw JSON for scripting.

## Download interface (TUI + CLI)

Download whole playlists or single videos from YouTube into the library —
a YouTube-like flow in the terminal, built on yt-dlp.

**TUI** (`muse tui`, tab 5 / Downloads tab):

1. Paste a URL, `yt:<video-id>` or `ytpl:<playlist-id>` in the Downloads box
   and press Enter — the job starts immediately (`--playlist` force-flag not
   needed; `/playlist` URLs are detected automatically).
2. Or type search terms — YouTube results populate the list; Enter downloads
   the highlighted hit (a YouTube "YouTube Music"-style flow).
3. Jobs run in the daemon (survive TUI exit); the Jobs pane updates live with
   progress lines, imported track ids and real yt-dlp errors on failure.
   Enter on a finished job plays the first imported track.

**CLI equivalents:**

```bash
muse get "https://www.youtube.com/watch?v=…"      # single video (+ audio)
muse get "ytpl:PLxx" --playlist --workers 8       # whole playlist
muse get --watch "https://…"                      # poll until jobs finish
muse downloads                                    # job status/progress/errors
```

Completed downloads are auto-imported (tags via mutagen), queued for optional
analysis by the normal tiered worker, and immediately playable/searchable.
Downloads land in `download.path` (default `~/Music/muse`); override
per-process with the `MUSE_DOWNLOAD_DIR` env var.

Downloads always print the legal notice (see `muse legal`): for
personal/archival use only — respect YouTube ToS and copyright; muse does not
process DRM-protected streams.

## Architecture

```
muse/daemon     IPC socket server + command registry (single source of truth);
                0.5s service tick: gapless auto-advance and live AutoMix
                crossfades (beat anchors, tempo scaling, band-limited filters)
muse/audio      ffmpeg-decoder voices -> sounddevice stereo mixer
                (fade state machine = gapless advance / AutoMix crossfade)
muse/analysis   numpy DSP: onset autocorrelation BPM, KS-profile key,
                R128-style loudness, beat grid, sections; tiered worker
                vocals.py: Tier-2 vocal-region detection (quantile spectra)
muse/smart      Smart Shuffle scorer + AutoMix transition planner
muse/providers  local | youtube (yt-dlp downloader + playlist fetch)
                | apple (MusicKit scaffold)
                | lyrics (LRCLIB) | covers (Deezer -> generated fallback)
```

State: SQLite at `~/.local/share/muse/database.db`; config
`~/.config/muse/config.toml`; analysis cache `~/.cache/muse/analysis`.
Honors `MUSE_SOCKET` to relocate the IPC socket (daemon and clients both).

### Vocal detection (Tier-2)

The analysis worker runs a vocal pass on the last ~90s of audio by default:
~1s segments of 25th-percentile spectra (transient-robust — drums and clicks
fall below the quantile and vanish), scored by midband/treble energy share,
spectral spread, and prominent-partial count in the 300–3400 Hz band. Produces
`vocal_regions` plus `vocal_intro_sec` / `vocal_end_sec`, which the AutoMix
planner uses in vocal mode to prefer mixing into instrumental windows of the
incoming track (avoiding mid-song lyrical collisions).

### TUI

`muse tui` is a tabbed interface (Search / Queue / Library / Playlists /
Downloads, keys 1–5) with a now-playing header, transport keys, and live
status from the daemon.

## Offline / CI mode

Set `MUSE_OFFLINE=1` — no network calls (search, downloads, lyrics, covers all
degrade gracefully). Synthetic test signals (`tests/conftest.py`,
`muse/analysis/synth.py`) make the whole test suite deterministic and
network-free: `muse analyze`, the vocal detector, and AutoMix planning are
validated against click-tracks at known BPM, harmonic-vocal surrogates, and
chord pads in known keys.

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
- Band-limited crossfade filters are one-pole block-form DSP; block boundary
  decomposition differs inherently from single-block processing (documented in
  `tests/test_features_wave2.py`).