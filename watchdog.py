#!/usr/bin/env python3
"""
watchdog.py  -  optional helper that keeps the SBS2Flat mount alive.

Media servers (Emby/Jellyfin/Plex) can PURGE a library if its folder goes empty or
missing -- and most have no "don't remove missing items" toggle. The reliable
protection is: never let the mount stay down. This watchdog checks that the mount is
actually SERVING content and, if not, cleans up any stale mountpoint and restarts it.

It is OPTIONAL. Use it if your source is a cloud/rclone/NAS mount that can drop, or if
the machine reboots unattended. Configure the few values below, then run this INSTEAD
of launching SBS2Flat directly -- the watchdog launches and babysits it.

    python watchdog.py

Tip: on Windows, run it at logon as a hidden, admin scheduled task if your restart
command needs admin (e.g. controlling a service).
"""

import os, sys, time, shutil, subprocess

# ------------------------- CONFIG (edit these) -------------------------
# The mount's OUTPUT location (what you set as `output:` in config.yaml).
MOUNT_OUT   = r"C:\SBS2Flat"
# A subfolder that ALWAYS has files when the mount is healthy (a library label).
HEALTH_DIR  = r"C:\SBS2Flat\Movies"
# The command that STARTS the mount. Either the run command, or a service control.
#  - direct:  [sys.executable, r"C:\path\to\sbs2flat\sbs2flat.py"]
#  - service: ["sc", "start", "SBS2Flat"]   (or nssm, etc.)
START_CMD   = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sbs2flat.py")]
# Optional command to STOP the mount before a restart (leave [] if not needed).
STOP_CMD    = []
CHECK_EVERY = 10      # seconds between health checks
GRACE       = 90      # seconds to allow a restart to come up (mount waits for sources)
# ----------------------------------------------------------------------

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(os.path.dirname(MOUNT_OUT) or ".", "watchdog.log"),
                  "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def healthy():
    try:
        if not os.path.isdir(HEALTH_DIR):
            return False
        with os.scandir(HEALTH_DIR) as it:
            for _ in it:
                return True
        return False
    except OSError:
        return False


def _run(cmd):
    if not cmd:
        return
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=NO_WINDOW)
    except Exception as e:
        log(f"command failed {cmd}: {e}")


def recover():
    log("Recovering mount...")
    _run(STOP_CMD)
    time.sleep(3)
    # clean orphaned ffmpeg
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/f", "/im", "ffmpeg.exe"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=NO_WINDOW)
        except Exception:
            pass
    # remove a leftover mount folder that would block a fresh mount
    for _ in range(5):
        if not os.path.exists(MOUNT_OUT):
            break
        try:
            if os.name == "nt":
                subprocess.run(["powershell", "-NoProfile", "-Command",
                                f"Remove-Item -Recurse -Force '{MOUNT_OUT}' -ErrorAction SilentlyContinue"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=NO_WINDOW)
            else:
                shutil.rmtree(MOUNT_OUT, ignore_errors=True)
        except Exception:
            pass
        time.sleep(1)
    _run(START_CMD) if False else None
    # START_CMD may be a long-running process (direct launch) -> Popen, not run
    try:
        subprocess.Popen(START_CMD, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=NO_WINDOW)
    except Exception as e:
        log(f"start failed: {e}")


def main():
    log("SBS2Flat watchdog started.")
    if not healthy():
        recover()
        w = 0
        while w < GRACE and not healthy():
            time.sleep(3); w += 3
    while True:
        time.sleep(CHECK_EVERY)
        if healthy():
            continue
        # confirm it's really down, not a momentary blip
        down = True
        for _ in range(2):
            time.sleep(3)
            if healthy():
                down = False; break
        if not down:
            continue
        log("Mount down -> recovering.")
        recover()
        w = 0
        while w < GRACE:
            time.sleep(3); w += 3
            if healthy():
                break
        log("Recovered." if healthy() else "Still down; will retry next cycle.")


if __name__ == "__main__":
    main()
