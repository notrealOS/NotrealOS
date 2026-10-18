#!/usr/bin/env python3
"""Lock-screen background for the active theme -> org.gnome.desktop.screensaver picture-uri.
Phosh draws that picture behind the lock screen and follows the setting live.
  micro/alien/nasa: the notarealShell drawn sheet, rendered offscreen at 1080x2400 (540x1200 @2x)
  other themes: ~/theme/lock/<name>.jpg|png if present (vapor = the original picture)"""
import os
import subprocess
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gio, Graphene, Gsk, Gtk  # noqa: E402

sys.path.insert(0, os.path.expanduser("~/.local/share/phantomshell"))
import notareal_theme as theme  # noqa: E402

LOCK = os.path.expanduser("~/theme/lock")
W, H, SCALE = 540, 1200, 2


def render_drawn(out, method="_micro"):
    import phantomshell
    Gtk.init()
    bg = phantomshell.VaporBackground()
    bg.t = 9.0                       # a pleasant moment of the slow tick ring / orbit marker
    snap = Gtk.Snapshot()
    snap.scale(SCALE, SCALE)
    getattr(bg, method)(snap, W, H)
    node = snap.to_node()
    r = Gsk.CairoRenderer()
    r.realize(None)
    tex = r.render_texture(node, Graphene.Rect().init(0, 0, W * SCALE, H * SCALE))
    r.unrealize()
    tex.save_to_png(out)


def main():
    name = theme.active()
    pic = None
    for ext in ("png", "jpg"):
        p = os.path.join(LOCK, f"{name}.{ext}")
        if os.path.exists(p):
            pic = p
    import phantomshell
    drawn = phantomshell.DRAWN_WALLPAPERS.get(theme.value("wallpaper"))
    if drawn:                       # micro / alien / nasa: the shell's drawn sheet
        pic = os.path.join(LOCK, f"{name}.png")
        render_drawn(pic, drawn)
    if not pic:
        print(f"lock: no picture for '{name}', left as is")
        return
    s = Gio.Settings.new("org.gnome.desktop.screensaver")
    s.set_string("picture-uri", "file://" + pic)
    s.set_string("picture-options", "zoom")
    Gio.Settings.sync()
    print(f"lock: {pic}")


if __name__ == "__main__":
    main()
