# SBS2Flat

**Watch your 3D movies as normal 2D in Plex, Jellyfin, or Emby — no pre-conversion needed.**

If you have a library of 3D movies (side-by-side or over-under) but watch on a
normal 2D TV, your media server just shows a squished double-image. The usual "fix"
is to re-encode your whole library to 2D ahead of time — hours of work and double
the disk space.

SBS2Flat does it **live**. It creates a virtual folder where your 3D movies appear
as ordinary 2D files, converted on the fly as you play them. Point your media server
at that folder — or just open it and play a file directly — and everything looks
normal. It can also serve the same movies as **anaglyph** (for cheap green/magenta
glasses) or pass them through as raw 3D.

> This solves a request people have been making for over a decade. As far as I know,
> nothing else does live SBS/OU→2D as a drop-in layer for a media server.

## What it does

* **2D mode** — crops one eye and un-squishes it to a normal flat picture.
* **Anaglyph mode** — merges both eyes into green/magenta 3D, watchable on any screen with cheap glasses (keeps full depth).
* **3D passthrough** — serves the original file unchanged, for real 3D displays.
* **Auto-detects** side-by-side vs over-under, and half vs full, per file.
* **Cleans up names** (strips `\_HSBS`, `3D`, release junk) so cover art matches.
* **Hides junk** (disc images / VR clips that can't be flattened and jam scans).
* **Collapses** redundant "movie-folder-inside-a-movie-folder" nesting.
* Works for **movies and TV shows**.

It reads your existing folders as-is — local disk, NAS, or an already-mounted
cloud/rclone drive. It never moves, copies, or modifies your originals.

## Requirements

* **Windows** (uses WinFsp for the virtual filesystem)
* [**WinFsp**](https://winfsp.dev) — install this first (make sure to set the development option to install to hard drive)
* [**ffmpeg**](https://ffmpeg.org) — for the live conversion. **Hardware encoding is strongly recommended** (NVIDIA NVENC or Intel QuickSync); software encoding works but is slower.
* **Python 3.8+**
* On first install, `pip install winfspy` may need the **"Desktop development with C++"** build tools (from Visual Studio Build Tools). Install those, then reopen your terminal.

## Setup

1. Install **WinFsp** and **ffmpeg**.
2. Install the Python dependencies:

```
   pip install -r requirements.txt
   ```

3. Copy `config.example.yaml` to `config.yaml` and edit it:

   * Set `output` to a **folder** like `C:\\SBS2Flat` (a folder path is the most
reliable; a drive letter can work too).
   * List your 3D folders under `libraries`, each with a `mode` (`2d`, `anaglyph`, or `3d`).
   * **Set `encoder` to match your hardware**: `nvenc` (NVIDIA), `qsv` (Intel), or
`software`. **If the wrong one is set, ffmpeg won't start and nothing plays.**
   * If nothing plays even so, set the full `ffmpeg:` path explicitly.
4. Check everything is ready:

```
   python sbs2flat.py --check
   ```

5. Start it:

```
   python sbs2flat.py
   ```

## Watching

You have two easy options:

* **Through a media server** — in Plex / Jellyfin / Emby, add a library pointing at
your `output` location (it's a normal local folder, so no network share is needed
when the server is on the same PC). **Turn off deep media analysis / chapter-image
/ trickplay extraction** on that library — those probe every file during scanning
and trigger conversions. Filename-based scanning works great.
* **Straight from the folder** — open the `output` folder in Windows or a player like
VLC and just play a file. No media server required.

## Run automatically at startup (hidden)

To have SBS2Flat launch quietly in the background when you log in:

1. Make sure `run\_hidden.vbs` points at the folder containing `sbs2flat.py`
(edit the `folder` line inside it if needed).
2. Double-click **`install\_startup.bat`**. It adds a hidden-launch shortcut to your
Startup folder. That's it — it now starts on login with no command window.

To remove it, delete `SBS2Flat.lnk` from your Startup folder (the installer prints
the exact path).

## Notes on performance

Conversion happens live, so the first few seconds of a movie take a moment while it
starts encoding. Hardware encoding keeps this smooth. Seeking far ahead of what's
been converted will pause while it catches up. This is normal for on-the-fly
conversion — the trade for not pre-converting your whole library.

**Anaglyph mode is heavier** than 2D (it processes both eyes plus a per-pixel blend).
On slower machines (e.g. Intel iGPU) 1080p anaglyph may stutter. If so, set
`anaglyph_height: 720` (or `540`) in your config — it encodes much faster and looks
the same through colored glasses. Strong GPUs (NVIDIA) can leave it at `0` for full
resolution. `gpu_decode: true` also offloads decoding to the GPU (NVIDIA CUDA or
Intel QuickSync) to free up the CPU.

## Troubleshooting

* **Nothing plays / spins forever** → wrong `encoder` for your hardware, or ffmpeg
not found. Try `encoder: software`, and set the full `ffmpeg:` path.
* **Anaglyph looks flat** → If a specific file is still off, its layout may be unusual.
* **Library scan stalls** → turn off chapter-image / trickplay / real-time monitoring
on that library; make sure `hide\_junk: true` so ISOs/VR don't jam the scan.
* **Missing cover art** → keep `clean\_names: true`. A few oddly-named files may still
need a manual "Identify/Match" in your media server.

## License

MIT


