#!/usr/bin/env python3
"""ka5idle: let the FiiO KA5 USB-suspend while nothing plays (10-17 battery fix).

The KA5 drew ~150 mA idle because it never suspended: it has no remote wakeup, so usbhid
can't suspend it while anything holds its HID buttons open, and the kernel's console kbd
handler always does (phoc too). So while the KA5 sink is suspended by PulseAudio this
unbinds the HID interface (sudo /usr/local/bin/ka5hid off) and the KA5 autosuspends
2 s later; when the sink plays again it binds it back (buttons work normally via phoc).
Trade-off: the KA5's buttons do nothing while it is idle.
notarealPlayer's HI-FI mode opens the KA5's ALSA device directly (PulseAudio's sink stays
suspended), so an open KA5 PCM counts as playing too (checked every 2 s, 10-17).
Log: ~/ka5idle/ka5idle.log
"""
import glob
import os
import subprocess
import time

from gi.repository import GLib, GLibUnix

HERE = os.path.dirname(os.path.abspath(__file__))
SINK = "usb-FiiO_FiiO_KA5"


def log(*a):
    with open(os.path.join(HERE, "ka5idle.log"), "a") as f:
        print(time.strftime("%m-%d %H:%M:%S"), *a, file=f)


def hid_iface():
    """sysfs path of the KA5's HID interface, or None when it isn't plugged in."""
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(d + "/idVendor") as f, open(d + "/idProduct") as g:
                if (f.read().strip(), g.read().strip()) != ("2972", "0055"):
                    continue
        except OSError:
            continue
        for i in glob.glob("%s/%s:*" % (d, os.path.basename(d))):
            try:
                with open(i + "/bInterfaceClass") as f:
                    if f.read().strip() == "03":
                        return i
            except OSError:
                pass
    return None


class Ka5Idle:
    def __init__(self):
        self.playing = None
        # pactl subscribe tells us about sink state changes; the 10 s poll covers replugs
        # (the HID rebinds itself on plug-in) and a pulseaudio restart (the pipe dies with it)
        self.sub = None
        self.start_subscribe()
        GLib.timeout_add_seconds(10, self.tick)
        GLib.timeout_add_seconds(2, self.alsa_tick)
        self.update()

    def start_subscribe(self):
        try:
            self.sub = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE)
        except OSError as e:
            log("pactl subscribe failed:", e)
            return
        os.set_blocking(self.sub.stdout.fileno(), False)
        GLibUnix.fd_add_full(GLib.PRIORITY_DEFAULT, self.sub.stdout.fileno(), GLib.IOCondition.IN | GLib.IOCondition.HUP, self.on_sub)

    def on_sub(self, fd, cond):
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            return GLib.SOURCE_CONTINUE
        if not data:
            self.sub.wait()
            self.sub = None
            return GLib.SOURCE_REMOVE
        if b"sink" in data:
            self.update()
        return GLib.SOURCE_CONTINUE

    def tick(self):
        if self.sub is None:
            self.start_subscribe()
        self.update()
        return GLib.SOURCE_CONTINUE

    def alsa_open(self):
        """True while something (HI-FI) holds a KA5 playback PCM open directly."""
        for st in glob.glob("/proc/asound/KA5/pcm*p/sub*/status"):
            try:
                with open(st) as f:
                    if f.read().strip() != "closed":
                        return True
            except OSError:
                pass
        return False

    def alsa_tick(self):
        if self.playing is not None and self.alsa_open() != self.playing:
            self.update()
        return GLib.SOURCE_CONTINUE

    def sink_playing(self):
        try:
            out = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            return True   # unsure: leave the buttons alone
        for line in out.splitlines():
            f = line.split("\t")
            if len(f) >= 5 and SINK in f[1]:
                return f[4] in ("RUNNING", "IDLE")
        return False

    def update(self):
        iface = hid_iface()
        if iface is None:
            self.playing = None
            return
        playing = self.alsa_open() or self.sink_playing()
        bound = os.path.exists(iface + "/driver")
        if playing != bound:
            r = subprocess.run(["sudo", "-n", "/usr/local/bin/ka5hid", "on" if playing else "off"],
                               capture_output=True, text=True)
            log("buttons", "on" if playing else "off", "" if r.returncode == 0 else "FAILED: " + r.stderr.strip())
        if playing != self.playing:
            log("playing" if playing else "idle")
            self.playing = playing


if __name__ == "__main__":
    log("start v2")
    Ka5Idle()
    GLib.MainLoop().run()
