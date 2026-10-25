#!/usr/bin/env python3
"""nrbatteryd: battery logger for notreal.battery (10-23).

Every STEP s, and whenever the screen or the focused app changes, writes one JSON line to
~/.local/share/nrbattery/YYYY-MM-DD.jsonl closing the segment since the previous line:
  t, dt (wall s), slept (s spent suspended), pct, mah (charge used, from charge_counter;
  negative = charged), ma (current now, + = charging), mv, chg, scr (screen on), app,
  bat/skin/cpu temps (deg C), load, top (top 3 CPU processes in the segment, % of one core).
The app attributes each segment's mAh to screen-on app / screen-off awake / asleep.
Suspend shows up as wall time passing while CLOCK_MONOTONIC stands still.
"""
import json
import os
import subprocess
import sys
import time
from gi.repository import Gio, GLib

sys.path.insert(0, os.path.expanduser("~/saver"))
from autosaver import AutoSaver  # noqa: E402 - battery saver on at 20 %, off when plugged in (10-24)

STEP = 60
POLL = 5
BAT = "/sys/class/power_supply/qcom-battmgr-bat/"
USB = "/sys/class/power_supply/qcom-battmgr-usb/online"
OUT = os.path.expanduser("~/.local/share/nrbattery")
WATCH = os.path.expanduser("~/overview-widget/toplevel-watch")
TZ = "/sys/class/thermal/"
HZ = os.sysconf("SC_CLK_TCK")
KEEP_DAYS = 60

session = Gio.bus_get_sync(Gio.BusType.SESSION)
zones = {}
for z in sorted(os.listdir(TZ)):
    try:
        zones[z] = open(TZ + z + "/type").read().strip()
    except OSError:
        pass
CPU_ZONES = [z for z, t in zones.items() if t.startswith(("cpu", "cpuss"))]
SKIN_ZONE = next((z for z, t in zones.items() if t == "sdm-skin-thermal"), None)

state = {"app": None, "scr": None, "wall": time.time(), "mono": time.monotonic(),
         "cc": None, "procs": {}}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def rd(path, conv=int):
    try:
        return conv(open(path).read().strip())
    except (OSError, ValueError):
        return None


def screen_on():
    try:
        r = session.call_sync("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                              "org.freedesktop.DBus.Properties", "Get",
                              GLib.Variant("(ss)", ("org.gnome.Mutter.DisplayConfig", "PowerSaveMode")),
                              GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 2000, None)
        return r.unpack()[0] == 0
    except GLib.Error:
        return state["scr"] if state["scr"] is not None else True


def temp(z):
    v = rd(TZ + z + "/temp") if z else None
    return None if v is None else round(v / 1000, 1)


def procs():
    """pid -> (name, cpu ticks)"""
    out = {}
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            s = open(f"/proc/{p}/stat").read()
        except OSError:
            continue
        i = s.rfind(")")
        f = s[i + 2:].split()
        name = s[s.find("(") + 1:i]
        if name.startswith("python"):   # "python3" says nothing: use the script (voiced, chat, ...)
            try:
                args = open(f"/proc/{p}/cmdline", "rb").read().split(b"\0")
                script = next((a for a in args[1:] if a and not a.startswith(b"-")), b"")
                name = os.path.basename(script.decode(errors="replace")).removesuffix(".py") or name
            except OSError:
                pass
        out[p] = (name, int(f[11]) + int(f[12]))
    return out


def top(old, new, secs):
    use = {}
    for p, (name, t) in new.items():
        d = t - old.get(p, (name, t))[1] if p in old else 0
        if d > 0:
            use[name] = use.get(name, 0) + d
    best = sorted(use.items(), key=lambda kv: -kv[1])[:3]
    return [[n, round(100 * d / HZ / max(secs, 1))] for n, d in best]


def write(rec):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, time.strftime("%Y-%m-%d", time.localtime(rec["t"])) + ".jsonl"), "a") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def prune():
    cutoff = time.time() - KEEP_DAYS * 86400
    for n in os.listdir(OUT) if os.path.isdir(OUT) else []:
        p = os.path.join(OUT, n)
        if n.endswith(".jsonl") and os.path.getmtime(p) < cutoff:
            os.remove(p)


def sample():
    """Close the segment since the last sample (the state it describes is the old one)."""
    now, mono = time.time(), time.monotonic()
    dt = now - state["wall"]
    slept = max(0.0, dt - (mono - state["mono"]))
    cc = rd(BAT + "charge_counter")
    pr = procs()
    rec = {"t": round(now), "dt": round(dt), "slept": round(slept),
           "pct": rd(BAT + "capacity"),
           "mah": round((state["cc"] - cc) / 1000, 2) if cc is not None and state["cc"] is not None else None,
           "ma": round((rd(BAT + "current_now") or 0) / 1000),
           "mv": round((rd(BAT + "voltage_now") or 0) / 1000),
           "chg": rd(USB) == 1, "scr": state["scr"], "app": state["app"] if state["scr"] else None,
           "bat": round((rd(BAT + "temp") or 0) / 10, 1), "skin": temp(SKIN_ZONE),
           "cpu": max((temp(z) or 0) for z in CPU_ZONES) if CPU_ZONES else None,
           "load": round(os.getloadavg()[0], 2),
           "top": top(state["procs"], pr, mono - state["mono"])}
    if dt >= 1:
        write(rec)
        try:
            auto.step(rec["pct"], rec["chg"])
        except OSError as e:
            log("autosaver:", e)
    state.update(wall=now, mono=mono, cc=cc, procs=pr)


def tick():
    on = screen_on()
    if on != state["scr"]:
        if state["scr"] is not None:
            sample()
        state["scr"] = on
    # long sleeps: close the segment right after resume so it isn't blurred with awake time
    if time.time() - state["wall"] >= STEP or (time.time() - state["wall"]) - (time.monotonic() - state["mono"]) > 10:
        sample()
    return True


def on_watch(src, cond):
    line = src.readline()
    if not line:
        log("toplevel-watch ended, restarting")
        GLib.timeout_add_seconds(5, start_watch)
        return False
    parts = line.split()
    if parts and parts[0] == "active":
        app = parts[1] if len(parts) > 1 and parts[1] != "none" else "overview"
        if app != state["app"]:
            if state["scr"]:
                sample()
            state["app"] = app
    return True


def start_watch():
    p = subprocess.Popen([WATCH], stdout=subprocess.PIPE, text=True, bufsize=1)
    ch = GLib.IOChannel.unix_new(p.stdout.fileno())
    GLib.io_add_watch(ch, GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP, lambda c, cond: on_watch(p.stdout, cond))
    return False


auto = AutoSaver()
state["cc"] = rd(BAT + "charge_counter")
state["procs"] = procs()
state["scr"] = screen_on()
prune()
start_watch()
GLib.timeout_add_seconds(POLL, tick)
GLib.timeout_add_seconds(6 * 3600, lambda: prune() or True)
log("started")
GLib.MainLoop().run()
