"""
config_loader.py  -  reads config.yaml and turns it into the settings the
SBS2Flat mount engine uses. Keeps the friendly config separate from the engine
internals so the file the user edits stays simple.
"""

import os
import sys
import shutil

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency: PyYAML.  Install it with:  pip install pyyaml")


class ConfigError(Exception):
    pass


def _find_ffmpeg(value):
    if value and value != "auto":
        if os.path.isfile(value):
            return value
        raise ConfigError(f"ffmpeg not found at: {value}")
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise ConfigError(
        "Could not find ffmpeg automatically. Install it and either add it to your "
        "PATH, or set  ffmpeg: \"C:\\\\path\\\\to\\\\ffmpeg.exe\"  in config.yaml"
    )


def _pick_encoder(value):
    value = (value or "auto").lower()
    if value in ("qsv", "nvenc", "software"):
        return value
    if value == "auto":
        # Simple heuristic: prefer QSV, then nvenc, else software. The engine will
        # fall back to software if the chosen HW encoder fails at runtime.
        return "qsv"
    raise ConfigError(f"Unknown encoder: {value} (use auto, qsv, nvenc, or software)")


def load_config(path="config.yaml"):
    if not os.path.isfile(path):
        raise ConfigError(
            f"Config file not found: {path}\n"
            "Copy config.example.yaml to config.yaml and edit it."
        )
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    out = {}
    out["output"] = raw.get("output")
    if not out["output"]:
        raise ConfigError("config.yaml: 'output' is required (drive letter or folder).")

    libs = raw.get("libraries") or []
    if not libs:
        raise ConfigError("config.yaml: add at least one entry under 'libraries'.")

    maps, passthrough, anaglyph = [], [], []
    for i, lib in enumerate(libs, 1):
        label = lib.get("label")
        src = lib.get("path")
        mode = (lib.get("mode") or "2d").lower()
        if not label or not src:
            raise ConfigError(f"libraries[{i}]: each entry needs a 'label' and a 'path'.")
        if not os.path.isdir(src):
            sys.stderr.write(f"WARNING: source folder not found (will retry when available): {src}\n")
        spec = f"{label}={src}"
        if mode == "2d":
            maps.append(spec)
        elif mode == "3d":
            passthrough.append(spec)
        elif mode == "anaglyph":
            anaglyph.append(spec)
        else:
            raise ConfigError(f"libraries[{i}]: unknown mode '{mode}' (use 2d, 3d, or anaglyph).")

    out["map"] = maps
    out["passthrough"] = passthrough
    out["anaglyph"] = anaglyph
    out["ffmpeg"] = _find_ffmpeg(raw.get("ffmpeg"))
    out["encoder"] = _pick_encoder(raw.get("encoder"))
    out["target_height"] = int(raw.get("target_height", 1080))
    out["quality"] = int(raw.get("quality", 21))
    out["max_transcodes"] = int(raw.get("max_transcodes", 2))
    out["clean_names"] = bool(raw.get("clean_names", True))
    out["hide_junk"] = bool(raw.get("hide_junk", True))
    out["collapse_folders"] = bool(raw.get("collapse_folders", True))
    out["wait_for_sources"] = int(raw.get("wait_for_sources", 120))
    out["anaglyph_ghost"] = float(raw.get("anaglyph_ghost", 0.3))
    out["gpu_decode"] = bool(raw.get("gpu_decode", True))
    out["anaglyph_height"] = int(raw.get("anaglyph_height", 0) or 0)
    return out


if __name__ == "__main__":
    # Quick self-test: print what the config resolves to.
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")
    import json
    print(json.dumps(cfg, indent=2))
