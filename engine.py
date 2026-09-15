#!/usr/bin/env python3
r"""
sbs2d_winfsp.py  -  Windows (WinFsp) real-time SBS-3D -> 2D virtual mount for Plex.

Presents a read-only 2D view of one or more SBS 3D libraries under a single
drive. Nothing is pre-converted and nothing permanent is written: a title is
transcoded on playback into a temp scratch cache that self-deletes shortly after
playback stops (ref-counted).

    rclone mounts (FTP box etc.)  ->  sbs2d_winfsp.py  ->  drive Z:  ->  Plex  ->  PS5

Multiple sources appear as top-level folders in Z::

    python sbs2d_winfsp.py Z: --map Movies=R:\3DMovies --map TV=S:\3DShows --encoder qsv

Requires:
    pip install winfspy          (WinFsp is already installed if rclone mounts)
    ffmpeg + ffprobe on PATH

NOTE: WinFsp is Windows-only, so this can't be exercised in a Linux sandbox. It
targets the winfspy API (modeled on winfspy's memfs example). If the first run
throws, paste the traceback -- it's a quick fix.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import PureWindowsPath

try:
    from winfspy import (
        FileSystem,
        BaseFileSystemOperations,
        FILE_ATTRIBUTE,
        NTStatusObjectNameNotFound,
        NTStatusEndOfFile,
        NTStatusAccessDenied,
        NTStatusMediaWriteProtected,
        NTStatusNotADirectory,
    )
    from winfspy.plumbing.win32_filetime import filetime_now
    from winfspy.plumbing.security_descriptor import SecurityDescriptor
except ImportError:
    sys.exit("Missing dependency: pip install winfspy  (WinFsp must be installed)")

VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".wmv", ".webm"}
# Disc images and other non-croppable containers: hidden from listings entirely so
# scanners (Jellyfin) never try to parse them and hang. The mount cannot SBS-crop a
# disc image anyway, so hiding them loses no working content.
HIDE_EXT = {".iso", ".img", ".bin", ".nrg", ".mdf"}

_VR_RE = re.compile(r"(?:^|[_\.\- ])vr(?:[_\.\- 0-9]|$)", re.I)

def _is_hidden(name):
    if not HIDE_JUNK:
        return False
    if os.path.splitext(name)[1].lower() in HIDE_EXT:
        return True
    # Hide VR videos (180/360, etc.) -- not croppable, not matchable for artwork.
    if _VR_RE.search(os.path.splitext(name)[0]):
        return True
    return False

# --- Clean display names so a media server (Emby/Jellyfin) can match artwork. ---
# The real source files carry 3D tags (_3D, _HSBS, _FSBS, _Full_SBS, _3DFF, _LRF, and
# release-group junk) that break metadata matching. We present a CLEAN name to the
# client while keeping the real path internally for cropping. Directories are never
# renamed. Collisions (two sources cleaning to the same name) get a numeric suffix so
# neither disappears.
_CLEAN_TAGS = [
    r"3dff", r"3d", r"h?sbs", r"full[_\.\- ]?sbs", r"half[_\.\- ]?sbs",
    r"h?ou", r"full[_\.\- ]?ou", r"half[_\.\- ]?ou", r"over[_\.\- ]?under",
    r"top[_\.\- ]?bottom", r"tab", r"lrf", r"left[_\.\- ]?only", r"right[_\.\- ]?only", r"mvc",
]
_CLEAN_TAG_RE = re.compile(r"[_\.\- ]+(?:" + "|".join(_CLEAN_TAGS) + r")(?=[_\.\- ]|$)", re.I)

def _desep(s):
    # Convert . and _ separators to spaces. Dashes are LEFT ALONE (reverted) -- the
    # dash-to-space change caused nested-folder problems and did more harm than good.
    return re.sub(r"[._]+", " ", s)

def _clean_folder_name(name):
    if not CLEAN_NAMES:
        return name
    # Folders have no extension. Strip 3D tags, convert separators (incl dashes) to
    # spaces, wrap a bare year in parens. "the-mummy-returns" -> "the mummy returns".
    s = _CLEAN_TAG_RE.sub("", name)
    # bare year -> (year)
    m = re.search(r"^(.*?)[_\.\- ]((?:19|20)\d{2})(?:[_\.\- ]|$)", s)
    if m and "(" not in name:
        title = re.sub(r"\s+", " ", _desep(m.group(1))).strip()
        return f"{title} ({m.group(2)})"
    # keep existing (YYYY); just desep the rest
    return re.sub(r"\s+", " ", _desep(s)).strip()

def _clean_display_name(name):
    if not CLEAN_NAMES:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    # TV episode -> keep through SxxExx
    mtv = re.search(r"^(.*?[Ss]\d{1,2}[Ee]\d{1,3})", stem)
    if mtv:
        base = re.sub(r"\s+", " ", _desep(mtv.group(1))).strip()
        return base + ("." + ext if ext else "")
    # (YYYY) in parens
    m = re.search(r"^(.*?\((?:19|20)\d{2}\))", stem)
    if m:
        mm = re.match(r"^(.*?)(\((?:19|20)\d{2}\))$", m.group(1))
        title, year = mm.groups()
        title = re.sub(r"\s+", " ", _desep(_CLEAN_TAG_RE.sub("", title))).strip()
        return f"{title} {year}" + ("." + ext if ext else "")
    # bare year
    m2 = re.search(r"^(.*?)[_\.\- ]((?:19|20)\d{2})(?:[_\.\- ]|$)", stem)
    if m2:
        title, year = m2.groups()
        title = re.sub(r"\s+", " ", _desep(_CLEAN_TAG_RE.sub("", title))).strip()
        return f"{title} ({year})" + ("." + ext if ext else "")
    # no year -> strip tags + trailing junk
    s = re.sub(r"[_\.\- ]+$", "", _CLEAN_TAG_RE.sub("", stem))
    return re.sub(r"\s+", " ", _desep(s)).strip() + ("." + ext if ext else "")

DEFAULT_SBS_RE = re.compile(
    r"(?:^|[\W_])(?:h[-_.]?sbs|half[-_.]?sbs|full[-_.]?sbs|sbs|3d)(?:$|[\W_])", re.I
)
_EPOCH_AS_FILETIME = 11644473600


def unix_to_filetime(t):
    try:
        return int((t + _EPOCH_AS_FILETIME) * 10_000_000)
    except Exception:
        return filetime_now()


# --------------------------------------------------------------------------- #
# ffmpeg / probe
# --------------------------------------------------------------------------- #
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FFMPEG = "ffmpeg"   # overridden by --ffmpeg in main()


def _ff_capture(args, timeout=90):
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, creationflags=NO_WINDOW)
        return p.stdout, p.stderr.decode("utf-8", "replace")
    except Exception:
        return b"", ""


def _ssim_axis(src, axis, t):
    # Compare the two halves along an axis. High SSIM = the eyes match = that's the
    # 3D split axis.  axis "h" = side-by-side, "v" = over/under.
    if axis == "h":
        a, b = "crop=iw/2:ih:0:0", "crop=iw/2:ih:iw/2:0"
    else:
        a, b = "crop=iw:ih/2:0:0", "crop=iw:ih/2:0:ih/2"
    fc = (f"[0:v]{a},scale=320:180,setsar=1[a];"
          f"[0:v]{b},scale=320:180,setsar=1[b];[a][b]ssim")
    _, err = _ff_capture([FFMPEG, "-nostdin", "-ss", str(t), "-i", src,
                          "-an", "-frames:v", "3", "-filter_complex", fc, "-f", "null", "-"])
    vals = re.findall(r"All:([0-9.]+)", err)
    return max((float(v) for v in vals), default=0.0)


def _half_luma(src, crop, t):
    # Mean brightness of a cropped region (scale to 1x1 averages it into one pixel).
    out, _ = _ff_capture([FFMPEG, "-nostdin", "-ss", str(t), "-i", src, "-an",
                          "-frames:v", "1", "-vf", f"{crop},format=gray,scale=1:1",
                          "-f", "rawvideo", "-"])
    return out[0] if out else 0


def detect_layout(src, name):
    """Return (axis, half): axis 'sbs'|'ou', half 0|1. Looks at the video itself."""
    n = name.lower()
    # 1) Trust an explicit filename tag first -- these files are clearly labelled,
    #    and it's more reliable than pixel-guessing (which mis-detected some).
    if re.search(r"(?:^|[\W_])(?:hou|fou|ou|tab|tb|over[-_ ]?under|top[-_ ]?bottom)(?:$|[\W_])", n):
        axis = "ou"
    elif re.search(r"(?:^|[\W_])(?:hsbs|fsbs|sbs|side[-_ ]?by[-_ ]?side|half[-_ ]?sbs|full[-_ ]?sbs|lrf)(?:$|[\W_])", n):
        axis = "sbs"
    else:
        # 2) No tag in the name -> fall back to SSIM pixel analysis.
        sh = sv = 0.0
        for t in (90, 30, 5):
            sh, sv = _ssim_axis(src, "h", t), _ssim_axis(src, "v", t)
            if max(sh, sv) > 0:
                break
        if max(sh, sv) == 0:
            axis = "sbs"                        # last-resort default
        else:
            axis = "sbs" if sh >= sv else "ou"
    if axis == "sbs":
        ca, cb = "crop=iw/2:ih:0:0", "crop=iw/2:ih:iw/2:0"
    else:
        ca, cb = "crop=iw:ih/2:0:0", "crop=iw:ih/2:0:ih/2"
    n = name.lower()
    if "right_only" in n or "right-only" in n:
        return axis, 1
    if "left_only" in n or "left-only" in n:
        return axis, 0
    la, lb = _half_luma(src, ca, 90), _half_luma(src, cb, 90)   # pick the non-black eye
    # only override to the brighter half if the difference is clear (avoids noise)
    if abs(la - lb) < 8:
        return axis, 0
    return axis, (0 if la >= lb else 1)


_WH_CACHE = {}

def _probe_wh(src):
    """Return (width, height) of the source, or (0,0). Cached, with a SHORT timeout so
    a slow/network source can never hang the mount (falls back to 0,0 = default aspect)."""
    if src in _WH_CACHE:
        return _WH_CACHE[src]
    wh = (0, 0)
    try:
        p = subprocess.run([FFMPEG, "-nostdin", "-i", src, "-hide_banner"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=8, creationflags=NO_WINDOW)
        m = re.search(r"(\d{2,5})x(\d{2,5})", p.stderr.decode("utf-8", "replace"))
        if m:
            wh = (int(m.group(1)), int(m.group(2)))
    except Exception:
        wh = (0, 0)   # timeout or error -> proceed with default aspect (never hang)
    _WH_CACHE[src] = wh
    return wh


def _is_full(axis, w, h):
    """Decide if an SBS/OU source is FULL (each eye already full-size) vs HALF.
    Full-SBS -> the whole frame is ~2x as wide as a normal 16:9 (each eye 16:9-ish).
    Full-OU  -> the whole frame is ~2x as tall.
    """
    if not w or not h:
        return False
    ar = w / h
    if axis == "sbs":
        # a single 16:9 eye side-by-side full = ~3.55 aspect; half-SBS ~1.78.
        return ar > 2.3
    else:
        # full-OU stacks two 16:9 -> ~0.89; half-OU ~1.78.
        return ar < 1.2


def _codec_hint(src):
    """Guess the source video codec from the filename (cheap, no probe)."""
    n = os.path.basename(src).lower()
    if re.search(r"(hevc|h\.?265|x265)", n):
        return "hevc"
    if re.search(r"(av1)", n):
        return "av1"
    if re.search(r"(vp9)", n):
        return "vp9"
    if re.search(r"(h\.?264|x264|avc)", n):
        return "h264"
    return ""


def _hwaccel_args(cfg, src=None, force_cpu_decode=False):
    """Return -hwaccel args for GPU decoding, matching encoder + source codec, or [].
    Explicitly selecting the hardware decoder (e.g. hevc_qsv) makes HW HEVC/AV1 decode
    actually engage -- a bare -hwaccel often silently fails on HEVC and falls back to CPU."""
    if force_cpu_decode or not getattr(cfg, "hwdec", True):
        return []
    codec = _codec_hint(src) if src else ""
    if cfg.encoder == "nvenc":
        # -hwaccel cuda auto-downloads frames to system memory for the CPU filters.
        # (Don't force -c:v <dec>: it can leave frames in GPU format and break the
        #  crop/blend filters with a pixel-format error.)
        return ["-hwaccel", "cuda"]
    if cfg.encoder == "qsv":
        # -hwaccel qsv auto-downloads frames for the CPU filters. Forcing the explicit
        # decoder (hevc_qsv) leaves frames in 'qsv' pixel format and breaks the crop/
        # blend with "Impossible to convert between the formats", so we don't.
        return ["-hwaccel", "qsv"]
    return []


def _use_hwdec(cfg, src=None, force_cpu_decode=False):
    return bool(_hwaccel_args(cfg, src, force_cpu_decode))


def build_ffmpeg_cmd(src, dst, cfg, layout, anaglyph=False, force_cpu_decode=False):
    axis, half = layout
    # Crop one eye, then present it at the correct 2D display aspect. We do NOT trust
    # half/full tags (files are mislabeled). Instead: the intended 2D image aspect is
    # the source display-aspect halved (SBS) or doubled (OU), since both eyes show the
    # same scene. setdar makes the (square-pixel) frame display at that ratio, then we
    # scale to a concrete square-pixel raster so every player shows the true shape.
    # Empirically correct un-squish (works for both half- and full-SBS without tags):
    #   SBS: crop keeps original height -> force 1920 wide x that height.
    #        HSBS 1920x800 eye=960x800 -> 1920x800.  FSBS 3840x800 eye=1920x800 -> 1920x800.
    #   OU:  crop keeps original width -> stretch height back to full (2x crop height).
    if anaglyph:
        # Green/magenta anaglyph: keep FULL 3D depth (both eyes), rendered as
        # green/magenta color so it plays on any normal screen with cheap glasses.
        L = float(getattr(cfg, "anaglyph_ghost", 0.3))   # crosstalk-cancel strength
        # Determine full vs half from the FILENAME (instant -- avoids a network probe
        # that stalls startup on remote/seedbox files). Default to half (most common).
        _n = os.path.basename(src).lower()
        full = bool(re.search(r"(?:^|[^a-z])(full|fsbs|f-?sbs|fou|f-?ou)(?:[^a-z]|$)", _n))
        # Anaglyph is heavy (two eyes + per-pixel blend). Cap height for performance,
        # preserve the true aspect (no 16:9 stretch), and un-squish with ffmpeg
        # expressions so no absolute dimensions (and no probe) are needed.
        capH = int(getattr(cfg, "anaglyph_height", 0) or cfg.target_height or 1080)
        if axis == "ou":
            lcrop = "crop=iw:ih/2:0:0"
            rcrop = "crop=iw:ih/2:0:ih/2"
            unsq = "" if full else "scale=iw:ih*2,"   # half-OU: un-squish height
        else:
            lcrop = "crop=iw/2:ih:0:0"
            rcrop = "crop=iw/2:ih:iw/2:0"
            unsq = "" if full else "scale=iw*2:ih,"   # half-SBS: un-squish width
        # cap height (width auto, preserves aspect), then gbrp for the RGB blend.
        sc = f"{unsq}scale=-2:{capH}:flags=bilinear,format=gbrp,setsar=1"
        # Green/magenta with ghost reduction (TOP=left eye, BOTTOM=right eye):
        #   c0 Green = right.g - left.g*L
        #   c1 Blue  = left.b  - right.b*L
        #   c2 Red   = left.r  - right.r*L
        blend = (f"blend=c0_expr='max(BOTTOM-TOP*{L},0)':"
                 f"c1_expr='max(TOP-BOTTOM*{L},0)':"
                 f"c2_expr='max(TOP-BOTTOM*{L},0)'")
        vf = (f"[0:v]{lcrop},{sc}[L];"
              f"[0:v]{rcrop},{sc}[R];"
              f"[L][R]{blend},format=yuv420p,setsar=1")
    else:
        if axis == "ou":                           # over/under -> crop top or bottom
            crop = "crop=iw:ih/2:0:0" if half == 0 else "crop=iw:ih/2:0:ih/2"
            scale = "scale=iw:ih*2:flags=lanczos"  # keep width, un-squish height to full
        else:                                      # side-by-side -> crop left or right
            crop = "crop=iw/2:ih:0:0" if half == 0 else "crop=iw/2:ih:iw/2:0"
            scale = "scale=1920:ih:flags=lanczos"  # force full width, keep (original) height
        vf = f"setsar=1,{crop},{scale},setsar=1"
    maps = ["-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-map_chapters", "0"]
    if cfg.subs:
        maps += ["-map", "0:s?", "-c:s", "copy"]   # only safe for MKV text/bitmap subs
    else:
        maps += ["-sn"]                            # drop subs (they break stream-copy from MP4)
    audio = ["-c:a", "copy"] if cfg.copy_audio else ["-c:a", "aac", "-b:a", "256k", "-ac", "2"]
    if anaglyph:
        # anaglyph vf is a filter_complex graph; label its output and map it.
        fc = vf + "[vout]"
        vmap = ["-map", "[vout]"]
        # audio maps from input 0 (video came from the complex graph)
        amap = ["-map", "0:a?"]
        hw = _hwaccel_args(cfg, src, force_cpu_decode)
        base = ([cfg.ffmpeg, "-nostdin", "-loglevel", "error", "-y"] + hw + ["-i", src,
                 "-filter_complex", fc] + vmap + amap
                + (["-c:a", "copy"] if cfg.copy_audio
                   else ["-c:a", "aac", "-b:a", "256k", "-ac", "2"])
                + ["-sn"])
    else:
        hw = _hwaccel_args(cfg, src, force_cpu_decode)
        base = ([cfg.ffmpeg, "-nostdin", "-loglevel", "error", "-y"] + hw + ["-i", src]
                + maps + audio + ["-vf", vf])
    if cfg.encoder == "qsv":
        v = ["-c:v", "h264_qsv", "-global_quality", str(cfg.quality),
             "-maxrate", f"{cfg.maxrate}M", "-bufsize", f"{cfg.maxrate*2}M"]
    elif cfg.encoder == "nvenc":
        v = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", str(cfg.quality),
             "-maxrate", f"{cfg.maxrate}M", "-bufsize", f"{cfg.maxrate*2}M"]
    else:
        v = ["-c:v", "libx264", "-preset", cfg.x264_preset, "-crf", str(cfg.quality),
             "-maxrate", f"{cfg.maxrate}M", "-bufsize", f"{cfg.maxrate*2}M"]
    # Force the pixel aspect into the encoded bitstream (not just the container),
    # so a player reading the live-generated stream can't mis-guess the shape on
    # non-16:9 titles. -sar 1 = square pixels baked into the video header itself.
    return base + v + ["-sar", "1", "-max_muxing_queue_size", "4096",
                       "-f", "matroska", dst]


def probe(src):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", src],
            timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        info = json.loads(out)
    except Exception:
        return 0.0, 384_000
    dur = float(info.get("format", {}).get("duration", 0) or 0)
    abps = 384_000
    for s in info.get("streams", []):
        if s.get("codec_type") == "audio" and s.get("bit_rate"):
            abps = int(s["bit_rate"])
            break
    return dur, abps


# --------------------------------------------------------------------------- #
# Ephemeral transcode entry
# --------------------------------------------------------------------------- #
class Entry:
    def __init__(self, src, cache_base, cfg, sema, anaglyph=False):
        self.src, self.cfg, self.sema = src, cfg, sema
        self.anaglyph = anaglyph
        self._force_cpu_decode = False   # set True after a GPU-decode failure
        st = os.stat(src)
        mode = "ag" if anaglyph else "2d"
        key = hashlib.sha1(f"{src}:{st.st_size}:{int(st.st_mtime)}:{mode}".encode()).hexdigest()
        self.part = os.path.join(cache_base, key + ".part")
        self.final = os.path.join(cache_base, key + ".mkv")
        self.lock = threading.Lock()
        self.proc = None
        self.finished = False
        self.final_size = None
        self.refs = 0
        self.zero_since = time.time()
        self.last_read = time.time()
        self._slot = False
        self.failed = False            # ffmpeg errored on this file -> don't hang, fail fast
        self._killed = False           # set when idle-killed (not a real failure)
        self._logf = None
        self.log = os.path.join(cache_base, key + ".log")
        self.layout_file = os.path.join(cache_base, key + ".layout")
        self._layout = None
        # Size is estimated from the SOURCE file size (a metadata-only os.stat) so
        # that directory listings NEVER read file content. Running ffprobe here
        # made Plex scans crawl over FTP (end-of-file seeks per episode). The tail
        # is zero-padded on read and the real duration lives in the MKV header, so
        # over-estimating is safe.
        self.estimate = st.st_size * 2 + 64 * 1024 * 1024
        if cfg.keep_cache and os.path.exists(self.final):
            self.final_size = os.path.getsize(self.final)
            self.finished = True

    def acquire(self):
        with self.lock:
            self.refs += 1

    def release_ref(self):
        with self.lock:
            self.refs = max(0, self.refs - 1)
            if self.refs == 0:
                self.zero_since = time.time()

    def kill_if_read_idle(self, timeout):
        # A transcode nobody is reading from (e.g. Plex just analyzing headers)
        # is aborted quickly so a library scan can't spawn full-movie encodes.
        with self.lock:
            if self.proc and not self.finished and time.time() - self.last_read > timeout:
                self._killed = True
                try:
                    self.proc.terminate()   # _watch() releases the slot + clears proc
                except Exception:
                    pass

    def _resolve_layout(self):
        if self._layout:
            return self._layout
        try:
            with open(self.layout_file) as f:
                axis, half = f.read().strip().split(",")
                self._layout = (axis, int(half))
                return self._layout
        except Exception:
            pass
        self._layout = detect_layout(self.src, os.path.basename(self.src))
        try:
            with open(self.layout_file, "w") as f:
                f.write(f"{self._layout[0]},{self._layout[1]}")
        except OSError:
            pass
        return self._layout

    def ensure_running(self):
        with self.lock:
            if self.finished or self.failed or self.proc is not None:
                return
            if not self.sema.acquire(timeout=self.cfg.slot_timeout):
                return
            self._slot = True
            for p in (self.part, self.final):
                try:
                    os.remove(p)
                except OSError:
                    pass
            layout = self._resolve_layout()
            cmd = build_ffmpeg_cmd(self.src, self.part, self.cfg, layout, anaglyph=self.anaglyph, force_cpu_decode=self._force_cpu_decode)
            line = f"START [{layout[0]} half={layout[1]}] " + " ".join(cmd)
            if self.cfg.verbose:
                sys.stderr.write(line + "\n")
            try:
                with open(os.path.join(self.cfg.cache, "commands.log"), "a",
                          encoding="utf-8") as clog:
                    clog.write(line + "\n\n")
            except OSError:
                pass
            try:
                self._logf = open(self.log, "wb")
            except OSError:
                self._logf = None
            self.proc = subprocess.Popen(
                cmd, stderr=self._logf,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        rc = self.proc.wait()
        try:
            if self._logf:
                self._logf.close()
        except Exception:
            pass
        self._release_slot()
        with self.lock:
            if self._killed:                       # idle-killed on purpose -> allow restart
                self._killed = False
                self.proc = None
                return
            produced = os.path.getsize(self.part) if os.path.exists(self.part) else 0
            if rc == 0 and produced > 0:
                try:
                    os.replace(self.part, self.final)
                    self.final_size, self.finished = produced, True
                except OSError:
                    pass
            else:                                  # ffmpeg errored
                if _use_hwdec(self.cfg, self.src) and not self._force_cpu_decode:
                    # GPU decode may not support this file's codec -> retry on CPU once.
                    self._force_cpu_decode = True
                    sys.stderr.write(f"[sbs2d] GPU decode failed on {os.path.basename(self.src)}, "
                                     f"retrying with CPU decode...\n")
                    self.proc = None
                    # clear partial output so the retry starts clean
                    try:
                        os.remove(self.part)
                    except OSError:
                        pass
                    return                         # leaves proc None + not failed -> will restart
                self.failed = True
                sys.stderr.write(f"[sbs2d] ffmpeg failed (rc={rc}) on {self.src}\n"
                                 f"        log: {self.log}\n")
            self.proc = None

    def _release_slot(self):
        if self._slot:
            self._slot = False
            try:
                self.sema.release()
            except ValueError:
                pass

    def available(self):
        if self.finished:
            return self.final_size
        try:
            return os.path.getsize(self.part)
        except OSError:
            return 0

    def evict(self):
        with self.lock:
            if self.proc:
                self.proc.terminate()
                self.proc = None
            self.finished = False
            self.final_size = None
        for p in (self.part, self.final):
            try:
                os.remove(p)
            except OSError:
                pass

    def read(self, length, offset):
        if offset >= self.estimate:
            return b""
        self.last_read = time.time()
        self.ensure_running()
        if self.failed:
            return b""
        target = offset + length
        deadline = time.time() + self.cfg.read_timeout
        while True:
            self.last_read = time.time()
            if self.finished or self.available() >= target:
                break
            if self.failed:
                break
            if self.proc is None:
                self.ensure_running()
                if self.failed:
                    break
            if time.time() > deadline:
                break
            time.sleep(0.05)
        path = self.final if self.finished else self.part
        real = self.final_size if self.finished else self.available()
        buf = b""
        if offset < real:
            try:
                with open(path, "rb") as f:
                    f.seek(offset)
                    buf = f.read(min(length, real - offset))
            except OSError:
                buf = b""
        if self.finished and len(buf) < length and offset + len(buf) < self.estimate:
            pad = min(length - len(buf), self.estimate - (offset + len(buf)))
            buf += b"\x00" * pad
        return buf


# --------------------------------------------------------------------------- #
# A resolved node
# --------------------------------------------------------------------------- #
class Node:
    def __init__(self, win_path, src, sd, cfg, entry_getter, virtual=False, union_dirs=None):
        self.win_path = win_path
        self.src = src
        self.security_descriptor = sd
        self.union_dirs = union_dirs
        if union_dirs is not None or virtual or src is None:
            self.is_dir = True
            self.is_sbs = False
            self.attributes = FILE_ATTRIBUTE.FILE_ATTRIBUTE_DIRECTORY
            self.size = 0
            self.times = filetime_now()
            return
        st = os.stat(src)
        self.is_dir = os.path.isdir(src)
        self.times = unix_to_filetime(st.st_mtime)
        name = os.path.basename(src)
        detected = cfg.all or bool(DEFAULT_SBS_RE.search(name))
        if cfg.sbs_re:
            detected = bool(cfg.sbs_re.search(name)) or cfg.all
        # Passthrough labels serve RAW (3D, no crop). Derive the top-level label from
        # the win_path and skip cropping if it's a passthrough label.
        _pass = _ana = False
        try:
            _wp = str(win_path).strip("\\")
            _lbl = _wp.split("\\", 1)[0] if _wp else ""
            _pass = _lbl in getattr(cfg, "passthrough_labels", set())
            _ana = _lbl in getattr(cfg, "anaglyph_labels", set())
        except Exception:
            _pass = _ana = False
        is_video = (not self.is_dir and os.path.splitext(src)[1].lower() in VIDEO_EXT)
        # passthrough -> serve raw (never transcode). anaglyph -> always transcode.
        # otherwise -> transcode only if detected as 3D.
        self.is_sbs = is_video and not _pass and (_ana or detected)
        if self.is_dir:
            self.attributes = FILE_ATTRIBUTE.FILE_ATTRIBUTE_DIRECTORY
            self.size = 0
        else:
            self.attributes = FILE_ATTRIBUTE.FILE_ATTRIBUTE_ARCHIVE
            # Cheap size estimate for SBS files: no Entry object, no hashing, no locks.
            # (A DLNA server probes hundreds of files; building an Entry each time was
            # the bottleneck.) Matches Entry.estimate exactly so playback stays consistent.
            self.size = (st.st_size * 2 + 64 * 1024 * 1024) if self.is_sbs else st.st_size

    def get_file_info(self):
        return {
            "file_attributes": self.attributes,
            "allocation_size": (self.size + 4095) & ~4095,
            "file_size": self.size,
            "creation_time": self.times,
            "last_access_time": self.times,
            "last_write_time": self.times,
            "change_time": self.times,
            "index_number": 0,
        }


class Opened:
    def __init__(self, node):
        self.node = node
        self.entry = None
        self.fh = None


# --------------------------------------------------------------------------- #
# WinFsp operations
# --------------------------------------------------------------------------- #
class SBS2DOperations(BaseFileSystemOperations):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.mounts = cfg.mounts          # {label: realpath}
        os.makedirs(cfg.cache, exist_ok=True)
        self._sd = SecurityDescriptor.from_string("O:BAG:BAD:P(A;;FA;;;WD)")
        self.entries = {}
        self._dircache = {}
        self._nodecache = {}
        self._namemap = {}   # (parent_win_path -> {clean_name: real_basename})
        self.emap_lock = threading.Lock()
        self.sema = threading.BoundedSemaphore(cfg.max_transcodes)
        threading.Thread(target=self._reaper, daemon=True).start()

    def _parts(self, file_name):
        return [x for x in PureWindowsPath(file_name).parts if x not in ("\\", "/")]

    def _entry(self, win_path, src):
        # Anaglyph if the top-level label is an anaglyph label.
        parts = self._parts(win_path)
        lbl = parts[0] if parts else ""
        ana = lbl in getattr(self.cfg, "anaglyph_labels", set())
        with self.emap_lock:
            e = self.entries.get(win_path)
            if e is None:
                e = Entry(src, self.cfg.cache, self.cfg, self.sema, anaglyph=ana)
                self.entries[win_path] = e
            return e

    def _node_cached(self, file_name, make):
        import time as _t
        v = self._nodecache.get(file_name)
        if v and _t.time() - v[0] < self.cfg.dir_cache_secs:
            return v[1]
        n = make()
        self._nodecache[file_name] = (_t.time(), n)
        return n

    def _node(self, file_name):
        parts = self._parts(file_name)
        if not parts:                                   # virtual root
            return Node(str(file_name), None, self._sd, self.cfg, self._entry, virtual=True)
        label = parts[0]
        bases = self.mounts.get(label)
        if bases is None:
            raise NTStatusObjectNameNotFound()
        if len(parts) == 1:                             # a label folder
            if len(bases) == 1:
                return Node(str(file_name), bases[0], self._sd, self.cfg, self._entry)
            return Node(str(file_name), None, self._sd, self.cfg,
                        self._entry, union_dirs=bases)
        # Walk the path component by component, translating each cleaned display name
        # back to its real name via the per-directory namemap. This handles cleaned
        # FOLDER names in the middle of the path, not just the leaf file.
        for base in bases:                              # first drive that resolves wins
            real = base
            win_accum = "\\" + label
            ok = True
            for comp in parts[1:]:
                # collapse redundant nested wrapper folders before looking inside
                if os.path.isdir(real):
                    real = self._collapse_dir(real)
                # try literal first (fast path: name wasn't changed)
                cand = os.path.join(real, comp)
                if os.path.exists(cand):
                    real = cand
                    win_accum = win_accum + "\\" + comp
                    continue
                # else translate via this directory's namemap
                cmap = self._namemap.get(win_accum)
                if cmap is None:
                    try:
                        self._dir_children(self._node(win_accum))
                        cmap = self._namemap.get(win_accum)
                    except Exception:
                        cmap = None
                real_comp = cmap.get(comp) if cmap else None
                if real_comp is None:
                    ok = False
                    break
                real = os.path.join(real, real_comp)
                win_accum = win_accum + "\\" + comp
                if not os.path.exists(real):
                    ok = False
                    break
            if ok and os.path.exists(real):
                return Node(str(file_name), real, self._sd, self.cfg, self._entry)
        raise NTStatusObjectNameNotFound()

    def _node_parent(self, parts):
        # build a Node for the parent directory of parts (for map population)
        pwin = "\\" + "\\".join(parts[:-1])
        return self._node(pwin)

    def _reaper(self):
        while True:
            time.sleep(1)
            now = time.time()
            with self.emap_lock:
                snapshot = list(self.entries.items())
            for _, e in snapshot:                       # abort idle (scan) transcodes
                e.kill_if_read_idle(self.cfg.idle_kill)
            with self.emap_lock:
                dead = [k for k, e in self.entries.items()
                        if (not self.cfg.keep_cache) and e.refs == 0
                        and now - e.zero_since > self.cfg.evict_grace]
                for k in dead:
                    self.entries.pop(k).evict()
                total = sum((e.available() or 0) for e in self.entries.values())
                if total > self.cfg.cache_cap * (1024 ** 3):
                    for e in sorted((e for e in self.entries.values() if e.refs == 0),
                                    key=lambda e: e.zero_since):
                        e.evict()

    def get_volume_info(self):
        try:
            du = shutil.disk_usage(self.cfg.cache)
            return {"total_size": du.total, "free_size": du.free, "volume_label": "SBS2D"}
        except Exception:
            return {"total_size": 1 << 44, "free_size": 1 << 43, "volume_label": "SBS2D"}

    def set_volume_label(self, volume_label):
        raise NTStatusMediaWriteProtected()

    def get_security_by_name(self, file_name):
        node = self._node(file_name)
        return (node.attributes, node.security_descriptor.handle,
                node.security_descriptor.size)

    def open(self, file_name, create_options, granted_access):
        node = self._node(file_name)
        op = Opened(node)
        if node.is_sbs:
            op.entry = self._entry(node.win_path, node.src)
            op.entry.acquire()
        return op

    def close(self, file_context):
        if file_context.entry:
            file_context.entry.release_ref()
        if file_context.fh is not None:
            try:
                file_context.fh.close()
            except OSError:
                pass
            file_context.fh = None

    def cleanup(self, file_context, file_name, flags):
        return

    def get_file_info(self, file_context):
        return file_context.node.get_file_info()

    def get_security(self, file_context):
        sd = file_context.node.security_descriptor
        return sd.handle, sd.size

    def _dircache_get(self, key):
        import time as _t
        v = self._dircache.get(key)
        if v and _t.time() - v[0] < self.cfg.dir_cache_secs:
            return v[1]
        return None

    def _dircache_put(self, key, val):
        import time as _t
        self._dircache[key] = (_t.time(), val)

    @staticmethod
    def _collapse_dir(path):
        if not COLLAPSE:
            return path
        # If 'path' contains exactly ONE entry and it's a directory (a redundant nested
        # wrapper like "The Mummy (1999)\\The-mummy-1999\\"), descend into it. Repeat
        # a few levels deep just in case. Returns the real dir whose files we should show.
        for _ in range(4):
            try:
                entries = [e for e in os.listdir(path) if not _is_hidden(e)]
            except OSError:
                return path
            if len(entries) == 1 and os.path.isdir(os.path.join(path, entries[0])):
                path = os.path.join(path, entries[0])
                continue
            break
        return path

    def _dir_children(self, node):
        # returns list of (name, real_src_or_None); None means a label under root
        _ck = "dc:" + str(node.win_path)
        _cv = self._dircache_get(_ck)
        if _cv is not None:
            return _cv
        if not self._parts(node.win_path):               # virtual root -> labels
            return [(lbl, None) for lbl in sorted(self.mounts)]
        cmap = {}                                        # clean_name -> real_basename (this dir)
        used = set()
        def _present(realname, realpath):
            isdir = os.path.isdir(realpath)
            if isdir:
                disp = _clean_folder_name(realname)   # clean folder names too (dashes/tags)
            else:
                disp = _clean_display_name(realname)
            if disp != realname:
                # collision handling: if this clean name is taken, add a suffix.
                if disp in used:
                    b, d, e = disp.rpartition(".")
                    if not d or isdir:
                        b, e = disp, ""
                    i = 2
                    cand = f"{b} ({i}){('.'+e) if e else ''}"
                    while cand in used:
                        i += 1
                        cand = f"{b} ({i}){('.'+e) if e else ''}"
                    disp = cand
                cmap[disp] = realname
            used.add(disp)
            return disp
        if node.union_dirs:                              # merged label -> union drives
            seen, out = set(), []
            for base0 in node.union_dirs:
                base = self._collapse_dir(base0) if os.path.isdir(base0) else base0
                if not os.path.isdir(base):              # source (seedbox) offline -> skip it
                    continue
                try:
                    names = sorted(os.listdir(base))
                except OSError:
                    continue
                for n in names:
                    if n in seen:
                        continue
                    if _is_hidden(n):                    # skip .iso etc.
                        continue
                    seen.add(n)
                    disp = _present(n, os.path.join(base, n))
                    out.append((disp, os.path.join(base, n)))
            self._namemap[str(node.win_path)] = cmap
            self._dircache_put(_ck, out)
            return out
        try:
            _src = self._collapse_dir(node.src) if (node.src and os.path.isdir(node.src)) else node.src
            out = []
            for n in sorted(os.listdir(_src)):
                if _is_hidden(n):
                    continue
                disp = _present(n, os.path.join(_src, n))
                out.append((disp, os.path.join(_src, n)))
            self._namemap[str(node.win_path)] = cmap
            self._dircache_put(_ck, out)
            return out
        except OSError:
            return []

    def read_directory(self, file_context, marker):
        node = file_context.node
        if not node.is_dir:
            raise NTStatusNotADirectory()
        entries = []
        win = PureWindowsPath(node.win_path)
        if self._parts(node.win_path):                    # not the virtual root
            entries.append({"file_name": ".", **node.get_file_info()})
            entries.append({"file_name": "..", **node.get_file_info()})
        for name, src in self._dir_children(node):
            try:
                child = (self._node(str(win / name)) if src is None
                         else Node(str(win / name), src, self._sd, self.cfg, self._entry))
            except OSError:
                continue
            entries.append({"file_name": name, **child.get_file_info()})
        if marker is not None:
            names = [e["file_name"] for e in entries]
            if marker in names:
                entries = entries[names.index(marker) + 1:]
        return entries

    def get_dir_info_by_name(self, file_context, file_name):
        node = file_context.node
        child = self._node(str(PureWindowsPath(node.win_path) / file_name))
        return {"file_name": file_name, **child.get_file_info()}

    def read(self, file_context, offset, length):
        node = file_context.node
        if node.is_sbs:
            if offset >= node.size:
                raise NTStatusEndOfFile()
            data = file_context.entry.read(length, offset)
            if not data:
                raise NTStatusEndOfFile()
            return data
        if file_context.fh is None:
            file_context.fh = open(node.src, "rb")
        file_context.fh.seek(offset)
        data = file_context.fh.read(length)
        if not data:
            raise NTStatusEndOfFile()
        return data

    def create(self, *a, **k): raise NTStatusMediaWriteProtected()
    def overwrite(self, *a, **k): raise NTStatusMediaWriteProtected()
    def write(self, *a, **k): raise NTStatusMediaWriteProtected()
    def flush(self, *a, **k): return
    def set_basic_info(self, *a, **k): raise NTStatusMediaWriteProtected()
    def set_file_size(self, *a, **k): raise NTStatusMediaWriteProtected()
    def set_security(self, *a, **k): raise NTStatusMediaWriteProtected()
    def rename(self, *a, **k): raise NTStatusMediaWriteProtected()
    def can_delete(self, *a, **k): raise NTStatusAccessDenied()



# --------------------------------------------------------------------------- #
#  Public entry point: run(cfg_dict) -- called by sbs2flat.py
# --------------------------------------------------------------------------- #
import types as _types

# Feature toggles (set from config in run()). Default all ON to match prior behavior.
CLEAN_NAMES = True
HIDE_JUNK = True
COLLAPSE = True


def run(c):
    """c is the dict produced by config_loader.load_config()."""
    global FFMPEG, CLEAN_NAMES, HIDE_JUNK, COLLAPSE

    cfg = _types.SimpleNamespace()
    cfg.mountpoint     = c["output"]
    cfg.map            = list(c.get("map", []))
    cfg.passthrough    = list(c.get("passthrough", []))
    cfg.anaglyph       = list(c.get("anaglyph", []))
    cfg.cache          = os.path.join(tempfile.gettempdir(), "sbs2flat")
    cfg.encoder        = c.get("encoder", "qsv")
    cfg.ffmpeg         = c.get("ffmpeg", "ffmpeg")
    cfg.sbs_mode       = "full"
    cfg.target_height  = int(c.get("target_height", 1080))
    cfg.all            = True          # treat tagged/detected files as 3D
    cfg.sbs_regex      = None
    cfg.maxrate        = int(c.get("maxrate", 10))
    cfg.quality        = int(c.get("quality", 21))
    cfg.x264_preset    = c.get("x264_preset", "veryfast")
    cfg.max_transcodes = int(c.get("max_transcodes", 2))
    cfg.read_timeout   = int(c.get("read_timeout", 45))
    cfg.subs           = False
    cfg.copy_audio     = False
    cfg.slot_timeout   = int(c.get("slot_timeout", 60))
    cfg.idle_kill      = int(c.get("idle_kill", 8))
    cfg.evict_grace    = int(c.get("evict_grace", 60))
    cfg.dir_cache_secs = int(c.get("dir_cache_secs", 300))
    cfg.cache_cap      = int(c.get("cache_cap", 20))
    cfg.keep_cache     = False
    cfg.verbose        = bool(c.get("verbose", False))

    cfg.anaglyph_ghost = float(c.get("anaglyph_ghost", 0.3))
    cfg.hwdec = bool(c.get("gpu_decode", True))
    cfg.anaglyph_height = int(c.get("anaglyph_height", 0) or 0)
    CLEAN_NAMES = bool(c.get("clean_names", True))
    HIDE_JUNK   = bool(c.get("hide_junk", True))
    COLLAPSE    = bool(c.get("collapse_folders", True))

    if not cfg.map and not cfg.passthrough and not cfg.anaglyph:
        raise SystemExit("No source folders configured. Add some under 'libraries' in config.yaml.")

    # Ensure the mount point can be created. For a folder path like C:\SBS2Flat,
    # WinFsp needs the PARENT to exist but the mount folder itself to NOT exist.
    mp = cfg.mountpoint
    if not (len(mp) == 2 and mp[1] == ":"):        # not a bare drive letter
        parent = os.path.dirname(mp.rstrip("\\/"))
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                raise SystemExit(f"Could not create output parent folder {parent}: {e}")
        # if the mount folder itself exists from a previous run, remove the empty dir
        if os.path.isdir(mp):
            try:
                os.rmdir(mp)                       # only succeeds if empty (safe)
            except OSError:
                pass

    cfg.mounts = {}
    cfg.passthrough_labels = set()
    cfg.anaglyph_labels = set()

    def _register(specs, kind):
        for m in specs:
            if "=" not in m:
                raise SystemExit(f"Bad source (need LABEL=PATH): {m}")
            label, path = m.split("=", 1)
            path = path.rstrip("\\/")
            if kind == "pass":
                cfg.passthrough_labels.add(label)
            elif kind == "ana":
                cfg.anaglyph_labels.add(label)
            if not os.path.isdir(path):
                sys.stderr.write(f"WARNING: source not available, skipping for now: {path}\n")
            cfg.mounts.setdefault(label, []).append(os.path.abspath(path))

    _register(cfg.map, "map")
    _register(cfg.passthrough, "pass")
    _register(cfg.anaglyph, "ana")
    cfg.sbs_re = None
    FFMPEG = cfg.ffmpeg

    # Wait for source folders to be ready (handles SBS2Flat starting before an
    # rclone / NAS mount has finished coming up at login). Waits up to wait_for_sources
    # seconds; proceeds with whatever is ready after that so one offline source can't
    # block everything forever.
    wait_secs = int(c.get("wait_for_sources", 120))
    all_paths = [p for paths in cfg.mounts.values() for p in paths]
    if wait_secs > 0 and all_paths:
        import time as _t
        deadline = _t.time() + wait_secs
        while _t.time() < deadline:
            ready = [p for p in all_paths if os.path.isdir(p)]
            if len(ready) == len(all_paths):
                break
            missing = len(all_paths) - len(ready)
            print(f"[SBS2Flat] Waiting for {missing} source folder(s) to become "
                  f"available (e.g. rclone mount still starting)...")
            _t.sleep(5)
        still_missing = [p for p in all_paths if not os.path.isdir(p)]
        if still_missing:
            print(f"[SBS2Flat] Proceeding; {len(still_missing)} source(s) not ready yet "
                  f"(they'll appear once available).")

    ops = SBS2DOperations(cfg)
    fs = FileSystem(
        cfg.mountpoint, ops,
        sector_size=512, sectors_per_allocation_unit=1,
        volume_creation_time=filetime_now(), volume_serial_number=0,
        file_info_timeout=1000, case_sensitive_search=False,
        case_preserved_names=True, unicode_on_disk=True,
        persistent_acls=False, post_cleanup_when_modified_only=True,
        um_file_context_is_user_context2=True, file_system_name="SBS2Flat",
    )
    print("[SBS2Flat] Sources:")
    for lbl, paths in cfg.mounts.items():
        mode = ("anaglyph" if lbl in cfg.anaglyph_labels else
                "raw-3D" if lbl in cfg.passthrough_labels else "2D")
        for path in paths:
            print(f"   {cfg.mountpoint}\\{lbl}  [{mode}]  <-  {path}")
    print(f"[SBS2Flat] Mounted at {cfg.mountpoint} (encoder={cfg.encoder}).")
    print("[SBS2Flat] Point your media server here. Press Ctrl+C to stop.")
    fs.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        fs.stop()
        print("\n[SBS2Flat] Stopped.")
