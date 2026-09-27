# Changelog

## Unreleased — 2D-to-3D depth mode, reliability, and watch-while-scanning

### New: 2D-to-3D depth mode (`depth3d`)
- Turns **flat 2D content** into green/magenta 3D by faking depth from the image with
  a GPU shader (green/magenta anaglyph you watch with cheap glasses). Great for a
  cloud/Real-Debrid mount of ordinary 2D movies.
- Runs the shader on the GPU (via moderngl), not ffmpeg — light and fast.
- Tunable: `depth_strength` (amount of depth) and `depth_ghost` (crosstalk/green-
  ghosting cancellation). Defaults 0.30 / 0.3.
- Requires `pip install moderngl numpy`.

### New: watch while a library scan runs
- Playback now takes priority over scan probes: when you start a movie and all
  transcode slots are busy scanning, the mount evicts a scan-probe to give playback
  its slot immediately. The scan keeps running on the remaining slot(s).

### New: library-purge protection (last-known-good listings)
- If a source folder temporarily drops (cloud/NAS/rclone blip), the mount keeps
  showing the last-known file listing instead of an empty folder — so a media server
  scan can't see files "disappear" and purge your library. Persisted across restarts.
- **Also recommended:** turn OFF scheduled library scans in your media server so a
  scan never runs while the mount is momentarily down.

### New: optional watchdog helper (`watchdog.py`)
- Keeps the mount alive: detects if it's down/empty/wedged, cleans up a stale
  mountpoint, and restarts it. Optional — configure the paths/commands at the top.

### Fixes
- Killing a transcode now kills its whole process tree, so 2D-to-3D transcodes can't
  orphan child ffmpeg processes (which previously could pile up during a scan).

---


## Unreleased — anaglyph, performance, and reliability improvements

### Anaglyph (green/magenta) mode
- **Fixed heavy green colour cast.** The blend was running in YUV; it now
  converts to planar RGB (gbrp) before blending so the green/magenta channel
  math is correct.
- **Ported the original shader's ghost-reduction** (crosstalk cancellation) with
  a configurable strength via `anaglyph_ghost` (0.0–0.6, default 0.3). Higher =
  less ghosting.
- **Fixed the aspect ratio** so cinemascope (2.40:1) and other non-16:9 films no
  longer come out stretched tall. Full-vs-half SBS/OU is detected from the
  filename (no slow probe), and the un-squish preserves the true shape.
- **Big performance win:** each eye is downscaled *before* the per-pixel blend
  (the expensive step), so anaglyph encodes far faster.
- **`anaglyph_height`** setting to cap anaglyph output height for weaker machines
  (e.g. `360` on an Intel iGPU) — looks the same through glasses, encodes much
  faster. `0` = same as `target_height`.

### Performance
- **GPU decoding** (`gpu_decode`, default on) offloads source decoding to the GPU
  — NVIDIA CUDA or Intel QuickSync — matching the encoder. Falls back to CPU
  automatically if the GPU can't decode a given file, so nothing breaks.
- Faster directory listings and lighter per-file overhead.

### Library handling
- **Clean display names:** strips 3D tags (`_HSBS`, `_3D`, `Full_SBS`, release
  junk) and normalises separators so media servers match cover art.
- **Hides non-croppable junk:** disc images (`.iso`, etc.) and VR clips are hidden
  from listings so they can't jam a library scan.
- **Collapses redundant nested folders** (a movie folder whose only child is
  another folder) so titles resolve cleanly.
- **Three modes per source:** `2d` (crop to flat), `anaglyph` (green/magenta), and
  `3d` (raw passthrough) — the same source can be exposed in more than one.

### Startup
- **Waits for source folders** (`wait_for_sources`, default 120s) before mounting,
  so it can start before an rclone/NAS mount has finished coming up at login.

### Docs
- Config comments now explain the anaglyph/performance settings, including what to
  set on weaker Intel machines.
