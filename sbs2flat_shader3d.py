#!/usr/bin/env python3
"""
sbs2d_shader3d.py  -  2D->3D depth anaglyph transcoder for the SBS2D mount.

Runs Pearce's depth shader (pink/magenta) on the GPU via moderngl, turning flat 2D
into green/magenta depth-anaglyph. The mount invokes this (instead of ffmpeg) for
sources mapped to depth3d mode.

Pipeline:  ffmpeg decode (raw RGB) -> GPU shader per frame -> ffmpeg encode + audio.

Invoked as:
    python sbs2d_shader3d.py --src "X:\\movie.mkv" --dst "C:\\...\\hash.part"
        --ffmpeg "C:\\...\\ffmpeg.exe" [--width 1280] [--strength 0.15]
        [--encoder qsv|nvenc|software] [--maxrate 10] [--quality 21]
"""

import argparse, subprocess, sys, os

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

try:
    import numpy as np
    import moderngl
except ImportError:
    sys.stderr.write("depth3d needs: pip install moderngl numpy\n")
    sys.exit(3)

VERT = """
#version 330
in vec2 in_pos;
out vec2 u;
void main(){ u = in_pos*0.5+0.5; gl_Position = vec4(in_pos,0.0,1.0); }
"""
# Pearce's exact depth-anaglyph shader (with the working strength scale 0.02).
FRAG = """
#version 330
uniform sampler2D uTex;
uniform vec2  uTexSize;
uniform float uStrength;
uniform float uGhost;      // crosstalk cancellation strength (0.0-0.8)
in vec2 u;
out vec4 fragColor;
void main(){
    float inB = step(0.0,u.x)*step(u.x,1.0)*step(0.0,u.y)*step(u.y,1.0);
    float l  = dot(texture(uTex,u).rgb,                          vec3(.299,.587,.114));
    float lR = dot(texture(uTex,u+vec2(4.0/uTexSize.x,0.0)).rgb, vec3(.299,.587,.114));
    float lL = dot(texture(uTex,u-vec2(4.0/uTexSize.x,0.0)).rgb, vec3(.299,.587,.114));
    float smL = (l+lR+lL)/3.0;
    float dist = distance(u, vec2(0.5));
    float z = ((0.35-dist)*0.8 + (smL-0.5)*0.25) * (uStrength * 0.02);
    vec2 uL = vec2(clamp(u.x - z, 0.001, 0.999), u.y);
    vec2 uR = vec2(clamp(u.x + z, 0.001, 0.999), u.y);
    vec3 cL = texture(uTex, uL).rgb;   // left eye  -> magenta (R+B)
    vec3 cR = texture(uTex, uR).rgb;   // right eye -> green
    // Magenta channels from the left eye; green from the right eye.
    float magR = 0.7*cL.r + 0.3*cL.b;
    float magB = 0.3*cL.r + 0.7*cL.b;
    float grn  = cR.g;
    // GHOST/CROSSTALK CANCELLATION: subtract the other eye's luminance contribution so
    // the green (right) doesn't bleed through the magenta lens and vice versa. This lets
    // you push depth higher without green ghosting.
    float lLum = dot(cL, vec3(.299,.587,.114));   // left-eye brightness
    float rLum = dot(cR, vec3(.299,.587,.114));   // right-eye brightness
    magR = clamp(magR - uGhost * rLum, 0.0, 1.0);
    magB = clamp(magB - uGhost * rLum, 0.0, 1.0);
    grn  = clamp(grn  - uGhost * lLum, 0.0, 1.0);
    fragColor = mix(vec4(0.0,0.0,0.0,1.0), vec4(magR, grn*0.95, magB, 1.0), inB);
}
"""


def _get_ffprobe_path(ffmpeg_path):
    """Safely extracts the directory from the ffmpeg path and points to ffprobe."""
    dirname = os.path.dirname(ffmpeg_path)
    if dirname:
        # If a directory path exists, join it cleanly with ffprobe.exe
        return os.path.join(dirname, "ffprobe.exe")
    return "ffprobe"


def _probe_wh(ff, src):
    ffprobe = _get_ffprobe_path(ff)
    pr = subprocess.run([ffprobe, "-v", "error",
                         "-select_streams", "v:0", "-show_entries", "stream=width,height",
                         "-of", "csv=p=0", src], capture_output=True, text=True, creationflags=NO_WINDOW)
    try:
        w, h = map(int, pr.stdout.strip().split(",")[:2]); return w, h
    except Exception:
        return 1920, 1080


def _probe_fps(ff, src):
    ffprobe = _get_ffprobe_path(ff)
    pr = subprocess.run([ffprobe, "-v", "error",
                         "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                         "-of", "csv=p=0", src], capture_output=True, text=True, creationflags=NO_WINDOW)
    try:
        num, den = pr.stdout.strip().split("/"); return max(1.0, float(num)/float(den))
    except Exception:
        return 24.0


import atexit

_CHILDREN = []

def _kill_children(*_a):
    for p in _CHILDREN:
        try:
            p.kill()
        except Exception:
            pass

atexit.register(_kill_children)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--strength", type=float, default=0.30)
    ap.add_argument("--ghost", type=float, default=0.3, help="crosstalk cancel 0.0-0.8")
    ap.add_argument("--encoder", default="qsv")
    ap.add_argument("--maxrate", type=int, default=10)
    ap.add_argument("--quality", type=int, default=21)
    a = ap.parse_args()
    ff = a.ffmpeg

    sw, sh = _probe_wh(ff, a.src)
    fps = _probe_fps(ff, a.src)
    W = a.width
    H = int(round(W * sh / sw / 2) * 2)
    frame_bytes = W * H * 3

    # Decoder: raw RGB frames at WxH
    dec = subprocess.Popen(
        [ff, "-nostdin", "-loglevel", "error", "-i", a.src,
         "-an", "-vf", f"scale={W}:{H},format=rgb24",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, creationflags=NO_WINDOW)
    _CHILDREN.append(dec)

    # GPU
    ctx = moderngl.create_standalone_context()
    prog = ctx.program(vertex_shader=VERT, fragment_shader=FRAG)
    quad = ctx.buffer(np.array([-1,-1, 1,-1, -1,1, 1,1], dtype='f4').tobytes())
    vao = ctx.simple_vertex_array(prog, quad, 'in_pos')
    tex = ctx.texture((W, H), 3, alignment=1); tex.repeat_x = False; tex.repeat_y = False
    fbo = ctx.framebuffer(color_attachments=[ctx.texture((W, H), 3, alignment=1)])
    prog['uTexSize'].value = (float(W), float(H))
    prog['uStrength'].value = a.strength * 3.8
    prog['uGhost'].value = a.ghost

    # Encoder: takes raw RGB video from us + audio from the ORIGINAL source, writes .part
    if a.encoder == "nvenc":
        venc = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", str(a.quality)]
    elif a.encoder == "qsv":
        venc = ["-c:v", "h264_qsv", "-global_quality", str(a.quality)]
    else:
        venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(a.quality)]
    venc += ["-maxrate", f"{a.maxrate}M", "-bufsize", f"{a.maxrate*2}M"]

    enc = subprocess.Popen(
        [ff, "-nostdin", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", f"{fps:.4f}", "-i", "-",
         "-i", a.src,
         "-map", "0:v:0", "-map", "1:a?",
         "-c:a", "aac", "-b:a", "256k", "-ac", "2", "-sn"]
        + venc + ["-max_muxing_queue_size", "4096", "-f", "matroska", a.dst],
        stdin=subprocess.PIPE, creationflags=NO_WINDOW)
    _CHILDREN.append(enc)

    try:
        while True:
            raw = dec.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            tex.write(raw); tex.use(0); fbo.use()
            vao.render(moderngl.TRIANGLE_STRIP)
            out = fbo.read(components=3, alignment=1)
            enc.stdin.write(out)      # write straight through (no flip needed: quad maps 1:1)
        enc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    dec.wait()
    rc = enc.wait()
    _kill_children()
    sys.exit(0 if rc == 0 else 1)


if __name__ == "__main__":
    main()
