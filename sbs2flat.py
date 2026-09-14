#!/usr/bin/env python3
"""
SBS2Flat  -  watch your 3D (side-by-side / over-under) movies as normal 2D
             (or anaglyph) in Plex, Jellyfin, or Emby -- converted live, with
             no need to pre-convert your whole library.

Usage:
    python sbs2flat.py                 # uses config.yaml in this folder
    python sbs2flat.py my_config.yaml  # use a specific config file
    python sbs2flat.py --check         # check dependencies and config, then exit

Point your media server at the 'output' location from your config.
"""

import os
import sys
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))


def _err(msg):
    print("\n[SBS2Flat] " + msg + "\n", file=sys.stderr)


def check_dependencies(cfg=None):
    """Return a list of problems (empty list = all good)."""
    problems = []

    # Python version
    if sys.version_info < (3, 8):
        problems.append(f"Python 3.8+ required (you have {sys.version.split()[0]}).")

    # PyYAML
    try:
        import yaml  # noqa
    except ImportError:
        problems.append("PyYAML is not installed.  Run:  pip install pyyaml")

    # winfspy (only needed on Windows to actually mount)
    if os.name == "nt":
        try:
            import winfspy  # noqa
        except ImportError:
            problems.append(
                "winfspy is not installed.  Run:  pip install winfspy\n"
                "   (and install WinFsp itself from https://winfsp.dev )"
            )

    # ffmpeg
    if cfg is not None:
        ff = cfg.get("ffmpeg")
        if not ff or not (os.path.isfile(ff) or shutil.which(ff)):
            problems.append("ffmpeg not found. Install it or set its path in config.yaml.")

    return problems


def main():
    args = [a for a in sys.argv[1:]]
    check_only = "--check" in args
    args = [a for a in args if a != "--check"]
    config_path = args[0] if args else os.path.join(HERE, "config.yaml")

    # Basic dependency check first (before importing engine, which needs winfspy).
    base_problems = check_dependencies()
    yaml_missing = any("PyYAML" in p for p in base_problems)
    if yaml_missing:
        for p in base_problems:
            _err(p)
        sys.exit(1)

    # Load config
    try:
        from config_loader import load_config, ConfigError
        cfg = load_config(config_path)
    except ConfigError as e:
        _err(str(e))
        sys.exit(1)

    problems = check_dependencies(cfg)
    if problems:
        _err("Setup needs attention:")
        for p in problems:
            print("  - " + p, file=sys.stderr)
        if check_only:
            sys.exit(1)
        # winfspy/ffmpeg problems are fatal for actually running
        fatal = [p for p in problems if "winfspy" in p or "ffmpeg" in p]
        if fatal:
            sys.exit(1)

    print("[SBS2Flat] Configuration looks good.")
    print(f"[SBS2Flat] Output location: {cfg['output']}")
    n = len(cfg["map"]) + len(cfg["passthrough"]) + len(cfg["anaglyph"])
    print(f"[SBS2Flat] {n} source folder(s) configured "
          f"({len(cfg['map'])} 2D, {len(cfg['anaglyph'])} anaglyph, {len(cfg['passthrough'])} raw-3D).")

    if check_only:
        print("[SBS2Flat] --check passed. Everything is ready.")
        return

    # Hand off to the engine (the mount). Imported here so --check works without winfspy.
    try:
        import engine
    except ImportError:
        _err("engine.py not found next to sbs2flat.py.")
        sys.exit(1)

    print("[SBS2Flat] Starting the virtual 2D library... (press Ctrl+C to stop)")
    engine.run(cfg)


if __name__ == "__main__":
    main()
