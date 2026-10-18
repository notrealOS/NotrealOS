#!/usr/bin/env python3
"""sleepd: suspend the phone soon after the screen goes off (10-12 battery fix).

gsd-power only suspends after 5 min without input and tries once; the screen was usually
turned off with the power key and back on within 5 min, so the phone almost never slept
(~17 %/h). Here: once the screen has been off for DELAY s, ask logind to suspend; retry
every RETRY s while it stays off (a sleep lock from music/calls/alarms blocks it, or a
wake without the screen). Not while charging. gsd's own timeout stays as a fallback.
"""
import time
from gi.repository import Gio, GLib

DELAY = 60
RETRY = 60
POLL = 5
BAT = "/sys/class/power_supply/qcom-battmgr-bat/status"

session = Gio.bus_get_sync(Gio.BusType.SESSION)
system = Gio.bus_get_sync(Gio.BusType.SYSTEM)
off_since = None
next_try = 0.0


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
    try:
        return open(BAT).read().strip() in ("Charging", "Full")
    except OSError:
        return False


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


def suspend():
    b = blocked()
    if b:
        log("skip, sleep blocked by", b)
        return
    try:
        system.call_sync("org.freedesktop.login1", "/org/freedesktop/login1",
                         "org.freedesktop.login1.Manager", "Suspend", GLib.Variant("(b)", (False,)),
                         None, Gio.DBusCallFlags.NONE, 5000, None)
        log("suspend requested")
    except GLib.Error as e:
        log("suspend refused:", e.message)


def tick():
    global off_since, next_try
    now = time.monotonic()
    if screen_on():
        off_since = None
    elif off_since is None:
        off_since = now
        next_try = now + DELAY
    elif now >= next_try:
        next_try = now + RETRY
        if not charging():
            suspend()
    return True


log("start, delay", DELAY)
GLib.timeout_add_seconds(POLL, tick)
GLib.MainLoop().run()
