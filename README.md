# muse

Linux/macOS-native terminal music client: a background **daemon** owns
playback, library and analysis; **CLI** and **TUI** clients talk to it over a
Unix-socket JSON API.

Core features: gapless playback with loudness normalization, **Smart Shuffle**
(BPM/key/energy scoring, no repeats), **AutoMix** (beat-aligned crossfade
planning + `atempo` via ffmpeg, band-limited and vocal-aware modes), offline
**analysis** (BPM / key / LUFS / beat grid / sections + Tier-2 **vocal-region
detection**), **Generate Queue** (transition-scored similar tracks), **yt-dlp**
downloads with legal notices, lyrics + cover art fetching/caching, macOS media
keys / Linux MPRIS integration, and an Apple Music provider scaffold that
honestly reports DRM limitations instead of circumventing them.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e .[dev]
# optional desktop integration:
.venv/bin/pip install -e .[macos]   # macOS media keys (pyobjc)
.venv/bin/pip install -e .[linux]   # Linux MPRIS (dbus-next)
```

Requires `ffmpeg` (decoding/analysis) and optionally `yt-dlp` (downloads) on
your PATH.

Optional — put `muse` on your global PATH (a symlink; repeat after moving the
project):

```bash
ln -sf "$(pwd)/.venv/bin/muse" /opt/homebrew/bin/muse   # macOS (Homebrew)
```

## Quickstart

```bash
# daemon + library
muse daemon start                  # background daemon (IPC socket + media keys)
muse import ~/Music/mine           # import files into the library (managed copies)
muse analyze --all                 # offline analysis: BPM/key/LUFS/beat grid
muse search "creed"                # search the local library
muse library --kind artists        # group view (artists|albums)

# playback
muse play 3                        # play track 3 (or: muse play "creed")
muse seek 30                       # jump to 0:30 (+10 / -5 = relative seek)
muse next                          # advance (Smart Shuffle when queue is empty)
muse next --plain                  # ... without Smart Shuffle
muse prev                          # restart track, or previous if <3s in
muse volume 75                     # percent volume

# queue + smart modes
muse queue add 7                   # enqueue
muse shuffle --keep-first          # shuffle the queue, playing track first
muse genq                          # Generate Queue (see below)
muse radio 5 --length 10           # Smart Shuffle sequence seeded by track 5
muse discover 5 --variety 0.8      # exploration shuffle (--enqueue to queue)

# AutoMix
muse automix on && muse automix length 8
muse automix bandpass off          # band-limited crossfade filter (default on)
muse automix vocal on              # vocal-aware transitions (default off)
muse automix-preview 1 2           # dry-run transition plan (no audio)

# info + extras
muse status                        # now playing
muse history --limit 10            # recently played
muse stats                         # library/play-count summary + top tracks
muse lyrics && muse cover          # now-playing extras
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

## YouTube downloads (TUI + CLI)

Download single videos or whole playlists into the library — a YouTube
Music-like flow in the terminal, built on yt-dlp.

Files land as `Artist - Song.mp3`: noise like "(Official Video)", "[4K]" or
"- Remastered 2011" is stripped from titles (kept: "(Live ...)",
"(feat. ...)"), and the search ranker prefers plain song/audio uploads over
music videos. Each download grabs cover art (video thumbnail as a sidecar
`.jpg` + embedded tags) and auto-fetches lyrics in the background.

**TUI** (`muse tui`, tab 5 / Downloads tab):

1. Paste a URL, `yt:<video-id>` or `ytpl:<playlist-id>` in the Downloads box
   and press Enter — `/playlist` URLs are detected automatically.
2. Or type search terms: the top 5 YouTube results populate the list
   (song-shaped hits first, tagged `song` vs `video`); Enter downloads the
   highlighted hit.
3. Jobs run in the daemon (they survive TUI exit); the Jobs pane updates live
   with progress lines, imported track ids and real yt-dlp errors. Enter on a
   finished job plays the first imported track.

**CLI:**

```bash
muse get "https://www.youtube.com/watch?v=…"      # single video (+ audio)
muse get "ytpl:PLxx" --playlist --workers 8       # whole playlist
muse get --watch "https://…"                      # poll until jobs finish
muse get "search:radiohead creep" --name          # resolve the top hit (label only)
muse get "search:radiohead creep" --watch         # download the top hit
muse ytsearch "radiohead creep" --limit 3         # ranked candidates (default 5)
muse downloads                                    # job status/progress/errors
```

`search:<query>` (also `ytsearch:<query>`) refs resolve inline to the top
ranked result; `--name` prints the resolved `Artist — Song` label; `ytsearch`
lists ranked candidates with `song`/`video` tags and copy-pasteable
`muse get <url>` hints.

Downloads land in `download.path` (default `~/Music/muse`;
`MUSE_DOWNLOAD_DIR` overrides per process). Completed downloads are
auto-imported (tags via mutagen) and immediately playable/searchable;
analysis runs on demand via `muse analyze --all`. The legal notice
(`muse legal`) prints with every download: personal/archival use only —
respect YouTube ToS and copyright; muse does not process DRM-protected
streams.

## Library: import & integrity

`muse import` copies each source file into muse's managed library dir
(`~/.local/share/muse/library/<Artist>/<stem> [<hash>].<ext>` — readable
original name + content-hash suffix) and references the copy, so the library
keeps working when the original file moves or is deleted. Re-importing the
same content is idempotent (same hash → same copy → same track row). Genre
tags are preserved, shown in queue/library listings, and drive Generate
Queue's genre bonus.

On every daemon start an integrity check deletes library entries whose file
is missing (metadata-only rows — e.g. un-downloaded YouTube refs — are kept;
queue/history/playlist entries cascade). `analyze --all` runs the same check
first, so dead rows can never clog the analysis batch.

## Generate Queue

With a track playing (or any track as the seed), Generate Queue ranks every
analyzed library track by BPM/key/energy transition score **plus a genre-tag
bonus** and queues the best matches (default 5) to play next. Matches below
the score gate are dropped, so a quiet library yields a short queue rather
than junk.

```bash
muse genq                # seed = now playing; queue top matches at the head
muse genq 5 --length 8   # seed by track id or search query
muse genq --no-enqueue   # preview scores without queueing
```

In the TUI, press `G` on the Queue tab to generate from the playing track.
Generated picks show their transition scores; only analyzed tracks are
candidates (run `muse analyze --all` first).

## TUI

`muse tui` is a tabbed interface (Search / Queue / Library / Playlists /
Downloads, keys 1–5) with a now-playing bar, transport keys, and live status
from the daemon; it falls back to in-process command handling when no daemon
runs. In [kitty](https://sw.kovidgoyal.net/kitty/), the now-playing bar shows
the track's cover art as a real image (Kitty Graphics Protocol); every other
terminal shows the same bar text-only.

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
muse/providers  local | youtube (yt-dlp downloader + playlist fetch;
                song-first search ranking + title cleanup)
                | apple (MusicKit scaffold)
                | lyrics (LRCLIB) | covers (video thumbs / Deezer ->
                generated fallback)
```

State: SQLite at `~/.local/share/muse/database.db` (managed import copies
under `~/.local/share/muse/library`); config `~/.config/muse/config.toml`;
analysis cache `~/.cache/muse/analysis`. Honors `MUSE_SOCKET` to relocate the
IPC socket (daemon and clients both).

### Vocal detection (Tier-2)

The analysis worker runs a vocal pass on the last ~90s of audio by default:
~1s segments of 25th-percentile spectra (transient-robust — drums and clicks
fall below the quantile and vanish), scored by midband/treble energy share,
spectral spread, and prominent-partial count in the 300–3400 Hz band. Produces
`vocal_regions` plus `vocal_intro_sec` / `vocal_end_sec`, which the AutoMix
planner uses in vocal mode to prefer mixing into instrumental windows of the
incoming track (avoiding mid-song lyrical collisions).

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
- Generate Queue candidates are scored only when analysis exists; unanalyzed
  tracks are skipped (run `muse analyze --all` to widen the pool).
- MPRIS is registered as a minimal prototype (play/pause/next only).
- `atempo` time-stretch is applied during AutoMix, limited to ±4% adjustments.
- Band-limited crossfade filters are one-pole block-form DSP; block boundary
  decomposition differs inherently from single-block processing (documented in
  `tests/test_features_wave2.py`).