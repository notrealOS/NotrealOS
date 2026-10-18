#!/usr/bin/env python3
"""Run an app unchanged and save its window as a PNG after a delay.

Usage (inside a compositor, see shoot.sh): snap.py OUT.png DELAY_S SCRIPT [ARGS...]
Optional: SNAP_ACTION="python expression" runs on the window just before the shot (w = window),
SNAP_PRELOAD=demo/<app>.py runs first and fills the app with made-up data.
"""
import os, runpy, sys

out, delay, script, args = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4:]

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import GLib, Graphene, Gtk


def windows():
    return [w for w in Gtk.Window.list_toplevels() if w.get_visible() and w.get_width() > 100]


def act():
    ws = windows()
    if ws and os.environ.get("SNAP_ACTION"):
        w = ws[0]
        try:
            eval(os.environ["SNAP_ACTION"], {"w": w, "Gtk": Gtk, "GLib": GLib})
        except Exception as e:
            print("SNAP_ACTION failed:", e, file=sys.stderr)
    return False


def shot():
    ws = windows()
    if not ws:
        print("no window", file=sys.stderr)
        os._exit(2)
    w = ws[0]
    width, height = w.get_width(), w.get_height()
    k = float(os.environ.get("SNAP_SCALE", "2"))      # 2x = the phone's real pixel density
    snap = Gtk.Snapshot()
    snap.scale(k, k)
    Gtk.WidgetPaintable.new(w).snapshot(snap, width, height)
    node = snap.to_node()
    w.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, width * k, height * k)).save_to_png(out)
    print("saved", out, width, height)
    os._exit(0)


def maximize():
    for w in windows():
        w.maximize()
    return not windows()


GLib.timeout_add(300, maximize)
GLib.timeout_add(int(float(os.environ.get("SNAP_ACTION_AT") or delay - 0.8) * 1000), act)
GLib.timeout_add(int(delay * 1000), shot)
GLib.timeout_add(int(delay * 1000) + 20000, lambda: os._exit(3))
sys.argv = [script] + args
sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
if os.environ.get("SNAP_PRELOAD"):     # demo data: swaps network/keyring calls for fake ones
    runpy.run_path(os.environ["SNAP_PRELOAD"], run_name="snap_preload")
runpy.run_path(script, run_name="__main__")
