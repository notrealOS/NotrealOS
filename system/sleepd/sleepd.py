#!/usr/bin/env python3
"""sleepd: suspend the phone soon after the screen goes off (10-12 battery fix).

gsd-power only suspends after 5 min without input and tries once; the screen was usually
turned off with the power key and back on within 5 min, so the phone almost never slept
(~17 %/h). Here: once the screen has been off for DELAY s, ask logind to suspend; retry
every RETRY s while it stays off (a sleep lock from music/calls/alarms blocks it, or a
wake without the screen). Not while charging. gsd's own timeout stays as a fallback.
"""
import os
import subprocess
import time
from gi.repository import Gio, GLib

DELAY = 60
RETRY = 60
POLL = 5
BAT = "/sys/class/power_supply/qcom-battmgr-bat/status"
USB = "/sys/class/power_supply/qcom-battmgr-usb/online"
KMSG = os.path.expanduser("~/crashlog/kmsg.log")
# dead USB controller = every suspend fails (-22) until reset (10-20 overnight death)
USB_DEAD = ("HC died", "xhci-hcd.1.auto: PM: failed to suspend")
NAG = 1800  # log "still awake" this often while the screen is off and we can't sleep

session = Gio.bus_get_sync(Gio.BusType.SESSION)
system = Gio.bus_get_sync(Gio.BusType.SYSTEM)
off_since = None
next_try = 0.0
last_why = None
last_nag = 0.0
pending = False
kmsg_pos = os.path.getsize(KMSG) if os.path.exists(KMSG) else 0
reset_proc = None


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def screen_on():
    try:
        r = session.call_sync("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                              "org.freedesktop.DBus.Properties", "Get",
                              GLib.Variant("(ss)", ("org.gnome.Mutter.DisplayConfig", "PowerSaveMode")),
                              GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 2000, None)
        return r.unpack()[0] == 0
    except GLib.Error:
        return True  # can't tell (phoc restarting): stay awake


def charging():
    # 10-20: battery status can stick at "Charging" after a USB/xhci crash; also need a live charger
    try:
        if open(USB).read().strip() != "1":
            return False
        return open(BAT).read().strip() in ("Charging", "Full")
    except OSError:
        return False


def why(reason):
    """Log skip reasons once per change instead of silently skipping (10-20 overnight drain)."""
    global last_why
    if reason != last_why:
        last_why = reason
        if reason:
            log("skip,", reason)


def blocked():
    """logind lets the session owner override its own block inhibitors, so check them here."""
    try:
        r = system.call_sync("org.freedesktop.login1", "/org/freedesktop/login1",
                             "org.freedesktop.login1.Manager", "ListInhibitors", None,
                             GLib.VariantType("(a(ssssuu))"), Gio.DBusCallFlags.NONE, 2000, None)
    except GLib.Error:
        return True
    for what, who, why, mode, uid, pid in r.unpack()[0]:
        if mode == "block" and "sleep" in what.split(":"):
            return f"{who}: {why}"
    return None


def usb_dead():
    """New kernel lines since last look mention a dead xhci? (crashlog's kmsg.log, new file per boot)"""
    global kmsg_pos
    try:
        size = os.path.getsize(KMSG)
        if size < kmsg_pos:
            kmsg_pos = 0
        with open(KMSG, errors="replace") as f:
            f.seek(kmsg_pos)
            new = f.read()
            kmsg_pos = f.tell()
    except OSError:
        return False
    return any(m in new for m in USB_DEAD)


def usb_reset():
    """sudo usbreset (~/usbreset, passwordless): mode flip, then rebind + flip."""
    global reset_proc
    if reset_proc and reset_proc.poll() is None:
        return
    log("USB controller dead, running usbreset")
    reset_proc = subprocess.Popen(["sudo", "-n", "/usr/local/sbin/usbreset"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def suspended(src, res):
    global pending
    pending = False
    try:
        src.call_finish(res)
    except GLib.Error as e:
        log("suspend refused:", e.message)


def suspend():
    # async: a hung logind call must never stall the retry loop again (10-20)
    global pending
    if reset_proc and reset_proc.poll() is None:
        why("usbreset running")
        return
    b = blocked()
    if b:
        why(f"sleep blocked by {b}")
        return
    if pending:
        why("previous Suspend call still pending")
        return
    why(None)
    pending = True
    system.call("org.freedesktop.login1", "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager", "Suspend", GLib.Variant("(b)", (False,)),
                None, Gio.DBusCallFlags.NONE, 30000, None, suspended)
    log("suspend requested")


def tick():
    global off_since, next_try, last_why, last_nag
    now = time.monotonic()
    if usb_dead():
        usb_reset()
    if screen_on():
        off_since = None
        last_why = None
    elif off_since is None:
        off_since = now
        next_try = now + DELAY
        last_nag = now
    elif now >= next_try:
        next_try = now + RETRY
        if charging():
            why("charging")
        else:
            suspend()
        if now - last_nag >= NAG:
            last_nag = now
            log(f"screen off {int((now - off_since) / 60)} min, still awake ({last_why or 'suspend keeps failing'})")
    return True


log("start, delay", DELAY)
GLib.timeout_add_seconds(POLL, tick)
GLib.MainLoop().run()
