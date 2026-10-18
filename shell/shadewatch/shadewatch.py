#!/usr/bin/env python3
"""Debug logger for "the notification shade won't come down" (10-14).

Every touch that starts on the top bar is logged with how far it was dragged down, the
active app, and the state of our overlays (crtd, widget). After a real pull-down a
screenshot is taken so we can see whether Phosh's drawer actually opened.
Log + shots: $XDG_RUNTIME_DIR/shadewatch/
"""
import fcntl, os, struct, subprocess, time

TOUCH_DEVICE = "/dev/input/by-path/platform-a94000.spi-event"
TOP = 0.04          # touch starting above this fraction of the height = top bar
PULL = 0.10         # dragged down at least this much = a real pull
KEEP_SHOTS = 15
EVENT = struct.Struct("llHHi")
EV_KEY, EV_ABS, BTN_TOUCH, ABS_MT_POSITION_Y = 1, 3, 0x14a, 0x36
EVIOCGABS_Y = 0x80184541 + ABS_MT_POSITION_Y - 1   # EVIOCGABS(ABS_MT_POSITION_Y)

RUN = os.environ.get("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
OUT = os.path.join(RUN, "shadewatch")
os.makedirs(OUT, exist_ok=True)
LOG = open(os.path.join(OUT, "shadewatch.log"), "a", buffering=1)


def log(*a):
    LOG.write(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a) + "\n")


def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception as e:
        return "ERR %s" % e


def last_line(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 300))
            return f.read().decode(errors="replace").strip().splitlines()[-1]
    except Exception:
        return "-"


def state():
    act = sh("wlrctl", "toplevel", "list", "state:active").split("\n")[0] or "none"
    return "app=[%s] crtd=[%s] widget=[%s]" % (
        act, last_line(os.path.join(RUN, "crtd.log")), last_line(os.path.join(RUN, "overview-widget.log")))


def screenshot():
    shots = sorted(f for f in os.listdir(OUT) if f.endswith(".png"))
    for old in shots[:-KEEP_SHOTS + 1]:
        os.unlink(os.path.join(OUT, old))
    path = os.path.join(OUT, time.strftime("%H%M%S.png"))
    r = sh("gdbus", "call", "--session", "-d", "org.gnome.Shell.Screenshot",
           "-o", "/org/gnome/Shell/Screenshot", "-m", "org.gnome.Shell.Screenshot.Screenshot",
           "false", "false", path)
    return path if r.startswith("(true") else "shot failed: " + r


def main():
    fd = os.open(TOUCH_DEVICE, os.O_RDONLY)
    absinfo = bytearray(24)
    fcntl.ioctl(fd, EVIOCGABS_Y, absinfo)
    _v, ymin, ymax = struct.unpack_from("iii", absinfo)
    span = ymax - ymin
    log("start, y range", ymin, ymax)
    start_y = last_y = None
    t0 = 0.0
    while True:
        data = os.read(fd, EVENT.size * 64)
        for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
            s, us, etype, code, value = EVENT.unpack_from(data, off)
            if etype == EV_ABS and code == ABS_MT_POSITION_Y:
                last_y = value
                if start_y is None:
                    start_y, t0 = value, s + us / 1e6
            elif etype == EV_KEY and code == BTN_TOUCH and not value:
                if start_y is not None and start_y - ymin < span * TOP:
                    down = (last_y - start_y) / span
                    dur = s + us / 1e6 - t0
                    msg = "top touch from=%.3f down=%.2f %.2fs %s" % (
                        (start_y - ymin) / span, down, dur, state())
                    if down >= PULL:
                        time.sleep(0.8)   # let the drawer finish opening
                        msg += " shot=" + screenshot()
                    log(msg)
                start_y = last_y = None


if __name__ == "__main__":
    main()
