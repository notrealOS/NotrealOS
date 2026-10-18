"""Camera services (PipeWire + WirePlumber) health, shared by notarealCamera and nrdoctor."""
import os
import subprocess
import sys
import time


def log(*a):
    print(time.strftime("%H:%M:%S"), "camsvc:", *a, flush=True)


def pids(name):
    r = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split()]


def healthy():
    return len(pids("pipewire")) == 1 and len(pids("wireplumber")) == 1


def restart_camera_services():
    """Restart PipeWire + WirePlumber and make sure exactly ONE of each runs.
    10-16: a WirePlumber that ignored SIGTERM survived a restart; two WirePlumbers then fought
    over the cameras and camss refused to start ("Failed to start media pipeline: -32")."""

    for name in ("wireplumber", "pipewire"):
        for pid in pids(name):
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    end = time.time() + 3
    while time.time() < end and (pids("wireplumber") or pids("pipewire")):
        time.sleep(0.2)
    for name in ("wireplumber", "pipewire"):
        for pid in pids(name):          # stuck in camera teardown: force it
            try:
                os.kill(pid, 9)
            except OSError:
                pass
    time.sleep(0.5)
    subprocess.run(["rc-service", "--user", "pipewire", "restart"], capture_output=True, timeout=20)
    time.sleep(1.5)
    subprocess.run(["rc-service", "--user", "wireplumber", "restart"], capture_output=True, timeout=20)
    time.sleep(3)
    wp = pids("wireplumber")
    for pid in sorted(wp)[:-1]:         # keep only the newest
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    log("camera services restarted: pipewire", pids("pipewire"), "wireplumber", pids("wireplumber"))
    return healthy()


if __name__ == "__main__":
    sys.exit(0 if restart_camera_services() else 1)
