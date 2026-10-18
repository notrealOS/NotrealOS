#!/usr/bin/env python3
"""notarealShell (formerly PhantomShell) for Phosh: home-screen wallpaper + a search pop-up.

10-12: the terminal is gone (old version: phantomshell.py.before-slim). What is left:
  home mode     BOTTOM layer behind all windows: the vapor wallpaper (what you see with no app open)
  summoned mode OVERLAY layer (left-edge swipe up / tap the wallpaper / widget): a search box
                (Enter = Google search in Firefox; app chips while typing; "remind 10m ...",
                "timer 5m", "note ..." still work), then the reminders and the latest notes,
                each with a delete button on the LEFT (user is left-handed).
  APPS panel    for the joystick's "all apps" slot (ShowApps).
Reminders are FIRED by ~/alarm/alarmd (it can wake the phone from sleep); voiced writes them.
Colors and font come from ~/theme/theme.css (notareal_theme).

Session D-Bus API (org.notarealinux.PhantomShell, /org/notarealinux/PhantomShell):
  Summon(), Dismiss(), Toggle(), ShowApps(), Open(query) -> s; signal SummonedChanged(b)
"""

import datetime
import fcntl
import json
import math
import os
import random
import re
import struct
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402
import notareal_theme as theme  # noqa: E402  ~/theme/theme.css

# gtk4-layer-shell is already loaded; launched apps must not inherit the preload
# (Firefox crashes with it, and every app would load it needlessly)
os.environ.pop("LD_PRELOAD", None)

APP_ID = "org.notarealinux.PhantomShell"
OBJ_PATH = "/org/notarealinux/PhantomShell"
IFACE_XML = """
<node><interface name="org.notarealinux.PhantomShell">
  <method name="Summon"/><method name="Dismiss"/><method name="Toggle"/><method name="ShowApps"/>
  <method name="Open"><arg name="query" type="s" direction="in"/><arg name="result" type="s" direction="out"/></method>
  <signal name="SummonedChanged"><arg name="summoned" type="b"/></signal>
</interface></node>
"""
# The overview widget (~/overview-widget) covers the top 236 px whenever no app window is
# active, which includes every time this shell is summoned: start the panel below it.
TOP_CLEARANCE = 250

# Phosh opens the overview with a swipe up from the bottom edge; it doesn't tell us,
# so while summoned we watch the touchscreen for that swipe and step aside.
TOUCH_DEVICE = "/dev/input/by-path/platform-a94000.spi-event"
EDGE_START = 0.97   # a touch starting below this fraction of the height is the home bar
EDGE_SWIPE = 0.08   # and must travel up this fraction of the height
EV_KEY, EV_ABS, BTN_TOUCH, ABS_MT_POSITION_Y = 0x01, 0x03, 0x14a, 0x36
EVENT = struct.Struct("llHHi")
EVIOCGABS_Y = (2 << 30) | (24 << 16) | (ord("E") << 8) | (0x40 + ABS_MT_POSITION_Y)

DATA_DIR = os.path.expanduser("~/.local/share/phantomshell")
NOTES_FILE = os.path.join(DATA_DIR, "notes.json")
REMINDERS_FILE = os.path.join(DATA_DIR, "reminders.json")
NOTES_SHOWN = 5

CSS = """
window.phantom { background: var(--nr-bg); color: var(--nr-fg-bright); }

/* soft Y2K window: navy body, lavender frame, gradient title bar, stacked layer behind */
.retro { background: alpha(var(--nr-bg), 0.74); border: 2px solid var(--nr-accent); border-radius: 7px;
         box-shadow: 0 5px 0 alpha(var(--nr-glow), 0.55); }
.ghost { background: alpha(var(--nr-violet), 0.45); border: 2px solid var(--nr-violet); border-radius: 7px; }
.titlebar { background: var(--nr-gradient); border-radius: 5px 5px 0 0; padding: 3px 6px; }
.title { color: var(--nr-bg); font-size: 9pt; font-weight: bold; letter-spacing: 1px; }
button.tb-btn { min-width: 18px; min-height: 18px; padding: 0; border-radius: 3px; border: none;
                box-shadow: none; background: var(--nr-bg); color: var(--nr-white); font-size: 8pt; }
button.tb-btn:active { background: var(--nr-surface); }
.retro-body { padding: 8px 10px; }
.rule { background: alpha(var(--nr-accent), 0.35); min-height: 1px; margin: 4px 0; }

entry.web { background: alpha(var(--nr-bg-deep), 0.9); color: var(--nr-white); box-shadow: none;
            border: 2px solid var(--nr-accent); border-radius: 5px; font-size: 10pt;
            min-height: 44px; caret-color: var(--nr-primary); }
entry.web:focus-within { border-color: var(--nr-primary); }
.status { font-size: 8.5pt; color: var(--nr-mint); }
.section { font-size: 8pt; font-weight: bold; letter-spacing: 3px; color: var(--nr-pink); margin-top: 4px; }
.empty { font-size: 8.5pt; color: alpha(var(--nr-accent), 0.6); }
.item-when { font-size: 8.5pt; color: var(--nr-primary); }
.item-text { font-size: 9.5pt; color: var(--nr-fg); }
button.del { min-width: 36px; min-height: 36px; padding: 0; border-radius: 5px; border: none; box-shadow: none;
             background: alpha(var(--nr-pink), 0.12); color: var(--nr-pink); font-size: 10pt; }
button.del:active { background: alpha(var(--nr-pink), 0.35); }

button.chip { background: var(--nr-mint); color: var(--nr-bg); border: none; border-radius: 4px;
              box-shadow: 0 3px 0 alpha(var(--nr-mint), 0.35);
              font-size: 8.5pt; font-weight: bold; padding: 3px 9px; min-height: 28px; }

button.app-tile { background: linear-gradient(135deg, var(--nr-mint), var(--nr-violet)); border: none; border-radius: 11px;
                  box-shadow: 0 3px 0 alpha(var(--nr-glow), 0.6); padding: 4px;
                  transition: transform 140ms ease-out; }
button.app-tile:active { transform: scale(0.9) translate(0, 3px); box-shadow: 0 1px 0 alpha(var(--nr-glow), 0.6); }
.keycap { background: var(--nr-bg); border-radius: 8px; padding: 5px 4px; }
.app-tile-name { font-size: 6.5pt; font-weight: bold; color: var(--nr-fg-bright); }
flowboxchild { padding: 0; background: none; }
entry.search { background: alpha(var(--nr-bg-deep), 0.9); color: var(--nr-white); box-shadow: none;
               border: 2px solid var(--nr-accent); border-radius: 5px; font-size: 9pt; }

.warning-title { font-weight: bold; font-size: 7pt; color: var(--nr-white); }
.warning { font-size: 5.5pt; color: var(--nr-fg-bright); }
.kanji { font-size: 10pt; letter-spacing: 10px; color: var(--nr-white); }

/* animations */
@keyframes appear { from { opacity: 0; transform: translate(0, 40px) scale(0.95); }
                    to { opacity: 1; transform: none; } }
.appear { animation: appear 340ms cubic-bezier(0.2, 0.9, 0.3, 1.12); }
@keyframes pop { 0% { opacity: 0; transform: scale(0.6); } 70% { transform: scale(1.06); }
                 100% { opacity: 1; transform: none; } }
button.chip { animation: pop 220ms ease-out; }
@keyframes drift { 0% { transform: translate(0, 0); } 50% { transform: translate(4px, 3px); }
                   100% { transform: translate(0, 0); } }
window.summoned .ghost { animation: drift 5s ease-in-out infinite; }
"""


class Apps:
    """Installed apps from .desktop files, matched by name."""

    def __init__(self):
        self.reload()

    def reload(self):
        apps = [a for a in Gio.AppInfo.get_all() if a.should_show()]
        self.apps = sorted(apps, key=lambda a: a.get_display_name().lower())

    def match(self, query, limit=6):
        q = query.lower().strip()
        if not q:
            return []
        scored = []
        for a in self.apps:
            name = a.get_display_name().lower()
            if name.startswith(q):
                score = 0
            elif any(w.startswith(q) for w in re.split(r"[\s\-_.]+", name)):
                score = 1
            elif q in name:
                score = 2
            elif q in (a.get_id() or "").lower():
                score = 3
            else:
                continue
            scored.append((score, len(name), a))
        scored.sort(key=lambda s: (s[0], s[1]))
        return [a for _, _, a in scored[:limit]]


def once(func):
    """Wrap a callback for GLib.idle_add/timeout_add so it runs exactly once.

    grab_focus() returns True on success, which GLib reads as "call me again":
    two such idles (prompt + terminal) then fight over focus forever."""
    def run(*args):
        func(*args)
        return GLib.SOURCE_REMOVE
    return run


def launch_context():
    ctx = Gdk.Display.get_default().get_app_launch_context()
    ctx.unsetenv("LD_PRELOAD")
    return ctx


def rgba(color, alpha=1.0):
    c = Gdk.RGBA()
    c.parse(color)
    c.alpha = alpha
    return c


def rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


def point(x, y):
    return Graphene.Point().init(x, y)


def stops(*pairs):
    out = []
    for offset, color in pairs:
        stop = Gsk.ColorStop()
        stop.offset = offset
        stop.color = rgba(color)
        out.append(stop)
    return out


class VaporBackground(Gtk.Widget):
    """Wallpaper picture, twinkling sparkles and a rotating wireframe triangle."""

    __gtype_name__ = "VaporBackground"

    def __init__(self):
        super().__init__(hexpand=True, vexpand=True)
        self.t = 0.0
        self.tick_id = 0
        self.last = None
        rnd = random.Random(7)
        self.sparkles = [(rnd.random(), rnd.random() * 0.42, rnd.uniform(0.6, 2.2), rnd.uniform(0, 6.28))
                         for _ in range(8)]
        self._textures = {}

    @property
    def texture(self):
        """Wallpaper picture for the theme: background-<wallpaper>.jpg (e.g. vaporlight),
        else the original background.jpg."""
        name = theme.value("wallpaper")
        if name not in self._textures:
            d = os.path.expanduser("~/.local/share/phantomshell")
            path = os.path.join(d, f"background-{name}.jpg")
            try:
                self._textures[name] = Gdk.Texture.new_from_filename(
                    path if os.path.exists(path) else os.path.join(d, "background.jpg"))
            except GLib.Error:
                self._textures[name] = None
        return self._textures[name]

    def start(self):
        if not self.tick_id:
            self.last = None
            self.tick_id = self.add_tick_callback(self._tick)

    def stop(self):
        if self.tick_id:
            self.remove_tick_callback(self.tick_id)
            self.tick_id = 0

    def _tick(self, widget, clock):
        # the sparkles and the triangle move slowly: 30 fps looks the same as the
        # panel's 120 and costs a quarter of the CPU/GPU
        now = clock.get_frame_time() / 1e6
        if self.last is not None and now - self.last < 1 / 30:
            return GLib.SOURCE_CONTINUE
        if self.last is not None:
            self.t += now - self.last
        self.last = now
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        drawn = DRAWN_WALLPAPERS.get(theme.value("wallpaper"))
        if drawn:
            getattr(self, drawn)(snap, w, h)
            return
        # wallpaper, scaled to cover the screen
        if self.texture:
            tw, th = self.texture.get_width(), self.texture.get_height()
            scale = max(w / tw, h / th)
            dw, dh = tw * scale, th * scale
            snap.append_texture(self.texture, rect((w - dw) / 2, (h - dh) / 2, dw, dh))
        else:
            snap.append_color(rgba(theme.hex("bg")), rect(0, 0, w, h))
        snap.append_color(rgba(theme.hex("bg"), 0.18), rect(0, 0, w, h))  # soften for legibility
        # sparkles
        for sx, sy, speed, phase in self.sparkles:
            a = 0.25 + 0.6 * abs(math.sin(self.t * speed + phase))
            x, y = sx * w, sy * h
            col = rgba(theme.hex("white"), a)
            snap.append_color(col, rect(x - 1, y - 5, 2, 10))
            snap.append_color(col, rect(x - 5, y - 1, 10, 2))
        # floating wireframe triangle
        tri = Gsk.PathBuilder.new()
        tx, ty, tr = w * 0.82, h * 0.075, w * 0.06
        rot = self.t * 0.6
        for j in range(4):
            ang = rot + j * 2 * math.pi / 3
            (tri.move_to if j == 0 else tri.line_to)(tx + tr * math.cos(ang), ty + tr * math.sin(ang))
        snap.append_stroke(tri.to_path(), Gsk.Stroke.new(2.0), rgba(theme.hex("mint"), 0.9))


# --- micro theme wallpaper: a micrographics / blueprint sheet, drawn (no picture) -----------

    def _label(self, snap, text, x, y, size=7, alpha=0.8, bold=False, color="fg", font="JetBrains Mono",
               glow=0):
        layout = self.create_pango_layout(text)
        fd = Pango.FontDescription.from_string(f"{font} {'Bold ' if bold else ''}{size}")
        layout.set_font_description(fd)
        snap.save()
        snap.translate(point(x, y))
        if glow:                      # CRT bloom: a blurred copy under the text
            snap.push_blur(glow)
            snap.append_layout(layout, theme.color(color, min(1.0, alpha * 1.1)))
            snap.pop()
        snap.append_layout(layout, theme.color(color, alpha))
        snap.restore()

    @staticmethod
    def _line(snap, x1, y1, x2, y2, alpha=0.8, width=1.0, color="fg"):
        pb = Gsk.PathBuilder.new()
        pb.move_to(x1, y1)
        pb.line_to(x2, y2)
        snap.append_stroke(pb.to_path(), Gsk.Stroke.new(width), theme.color(color, alpha))

    @staticmethod
    def _circle(snap, x, y, r, alpha=0.8, width=1.0, fill=False, color="fg"):
        pb = Gsk.PathBuilder.new()
        pb.add_circle(point(x, y), r)
        if fill:
            snap.append_fill(pb.to_path(), Gsk.FillRule.WINDING, theme.color(color, alpha))
        else:
            snap.append_stroke(pb.to_path(), Gsk.Stroke.new(width), theme.color(color, alpha))

    def _plus(self, snap, x, y, s, alpha=0.8, cross=False):
        if cross:
            self._line(snap, x - s * 0.7, y - s * 0.7, x + s * 0.7, y + s * 0.7, alpha, 1.4)
            self._line(snap, x - s * 0.7, y + s * 0.7, x + s * 0.7, y - s * 0.7, alpha, 1.4)
        else:
            self._line(snap, x - s, y, x + s, y, alpha, 1.4)
            self._line(snap, x, y - s, x, y + s, alpha, 1.4)

    def _micro(self, snap, w, h):
        t = self.t
        snap.append_color(theme.color("bg"), rect(0, 0, w, h))
        # paper grid, every 24 px, a stronger line every 5th
        for i in range(0, int(w) + 1, 24):
            snap.append_color(theme.color("fg", 0.07 if i % 120 == 0 else 0.035), rect(i, 0, 1, h))
        for j in range(0, int(h) + 1, 24):
            snap.append_color(theme.color("fg", 0.07 if j % 120 == 0 else 0.035), rect(0, j, w, 1))
        m = 18                                           # margin
        # left ruler (left-handed: the measuring edge sits on the left)
        self._line(snap, m, m + 40, m, h - m - 40, 0.55)
        for k, y in enumerate(range(int(m + 40), int(h - m - 40), 12)):
            long = k % 5 == 0
            self._line(snap, m, y, m + (12 if long else 6), y, 0.55)
            if k % 10 == 0:
                self._label(snap, f"{k:03d}", m + 15, y - 6, 5.5, 0.5)
        # corner registration marks
        for cx, cy in ((m, m), (w - m, m), (m, h - m), (w - m, h - m)):
            self._plus(snap, cx, cy, 7, 0.7)
        # header block
        self._label(snap, "NOTAREAL//SYS", 52, m + 22, 15, 0.92, bold=True)
        self._label(snap, "MICROGRAPH SHEET  REV 10-12  ·  OBJ-48", 54, m + 46, 6.5, 0.6)
        self._line(snap, 52, m + 62, w - m - 8, m + 62, 0.8)
        self._line(snap, 52, m + 66, w * 0.45, m + 66, 0.35)
        # main instrument: crosshair circle with a slowly turning tick ring
        cx, cy, r = w * 0.60, h * 0.42, min(w, h) * 0.27
        self._circle(snap, cx, cy, r, 0.85, 1.2)
        self._circle(snap, cx, cy, r * 0.62, 0.45, 1.0)
        self._circle(snap, cx, cy, r * 0.08, 0.9, fill=True)
        self._line(snap, cx - r * 1.25, cy, cx + r * 1.25, cy, 0.5)
        self._line(snap, cx, cy - r * 1.25, cx, cy + r * 1.25, 0.5)
        rot = t * 0.08
        for k in range(72):
            a = rot + k * math.tau / 72
            r1 = r * (0.86 if k % 6 else 0.78)
            self._line(snap, cx + r1 * math.cos(a), cy + r1 * math.sin(a),
                       cx + r * 0.97 * math.cos(a), cy + r * 0.97 * math.sin(a), 0.6 if k % 6 else 0.9)
        # orbiting marker on the inner ring + its leader line and readout
        a = t * 0.25
        mx, my = cx + r * 0.62 * math.cos(a), cy + r * 0.62 * math.sin(a)
        self._circle(snap, mx, my, 4.5, 0.95, fill=True, color="hot-pink")
        self._line(snap, cx + r * 1.05, cy - r * 0.95, w - m - 8, cy - r * 0.95, 0.6)
        self._circle(snap, cx + r * 1.05, cy - r * 0.95, 3, 0.8)
        self._label(snap, f"dX {r * 0.62 * math.cos(a):+07.2f}", w - m - 110, cy - r * 0.95 + 6, 6, 0.7)
        self._label(snap, f"dY {r * 0.62 * math.sin(a):+07.2f}", w - m - 110, cy - r * 0.95 + 18, 6, 0.7)
        self._label(snap, "A_", cx - r * 1.2, cy + r * 0.95, 22, 0.9, bold=True)
        # plus-mark matrix (one is a cross), like the OFFLINE sheet
        gx, gy = 56, h * 0.70
        for row in range(3):
            for col in range(4):
                self._plus(snap, gx + col * 26, gy + row * 26, 8, 0.75, cross=(row, col) == (1, 2))
        self._label(snap, "RELAY LINK\nOFFLINE", gx - 6, gy + 70, 6, 0.6)
        self._label(snap, "POWER: --\nSTATE: IDLE", gx + 70, gy + 70, 6, 0.6)
        # nodes on a rail
        ry = h * 0.82
        self._line(snap, 52, ry, w - m - 8, ry, 0.7)
        for k, x in enumerate((70, 140, 230, w * 0.62, w * 0.80)):
            self._circle(snap, x, ry, 6 if k % 2 else 4, 0.85, 1.2)
        self._line(snap, 140, ry, 110, ry + 30, 0.6)
        self._circle(snap, 110, ry + 30, 4, 0.8)
        # olive block with a few dashes (the one solid shape)
        snap.append_color(theme.color("cyan", 0.85), rect(w * 0.62, h * 0.70, w * 0.22, 10))
        for k in range(8):
            snap.append_color(theme.color("fg", 0.8), rect(w * 0.62 + k * 12, h * 0.70 + 18, 7, 3))
        # footer scale bar
        fy = h - m - 34
        self._line(snap, 52, fy, w - m - 8, fy, 0.7)
        for k, x in enumerate(range(52, int(w - m - 7), 10)):
            self._line(snap, x, fy, x, fy - (8 if k % 10 == 0 else 4), 0.6)
        self._label(snap, "0", 50, fy + 4, 5.5, 0.6)
        self._label(snap, "SCALE 1:1  ·  ALL DIMENSIONS IN PX", w * 0.42, fy + 4, 5.5, 0.6)

    # --- alien theme wallpaper: a 2120s colony CRT terminal (Romulus-style), drawn ----------------

    @staticmethod
    def _box(snap, x, y, bw, bh, alpha=0.9, width=2.0, color="fg", fill=None):
        if fill is not None:
            snap.append_color(theme.color(color, fill), rect(x, y, bw, bh))
        pb = Gsk.PathBuilder.new()
        pb.add_rect(rect(x, y, bw, bh))
        snap.append_stroke(pb.to_path(), Gsk.Stroke.new(width), theme.color(color, alpha))

    def _alien(self, snap, w, h):
        t = self.t
        VT, ST = "VT323", "Share Tech Mono"
        snap.append_color(theme.color("bg"), rect(0, 0, w, h))
        # faint phosphor bloom in the middle of the tube
        snap.append_radial_gradient(rect(0, 0, w, h), point(w / 2, h * 0.45), w * 0.9, h * 0.6, 0, 1,
                                    _stops(
                                        (0.0, theme.color("glow", 0.55)), (1.0, theme.color("glow", 0.0))))
        m = 16
        # header: inverse bar + tab row (like the colony store terminal)
        self._box(snap, m, m + 34, w - 2 * m, 26, 1.0, 1.0, fill=0.92)
        self._label(snap, "MU/TH/UR 6000  ·  INTERFACE 2037", m + 12, m + 34, 15, 1.0, color="bg", font=VT)
        self._box(snap, m, m + 66, w - 2 * m, 3, 0.9, 1.0, fill=0.9)
        tabs = ("MAIN", "NAV", "COMMS", "NO LOCATION")
        tw = (w - 2 * m) / len(tabs)
        active = int(t / 6) % len(tabs)
        for k, name in enumerate(tabs):
            x = m + k * tw
            if k == active:
                snap.append_color(theme.color("fg", 0.9), rect(x, m + 75, tw - 4, 20))
            else:
                self._line(snap, x + tw - 3, m + 75, x + tw - 3, m + 95, 0.8, 2)
            self._label(snap, name, x + 6, m + 75, 11, 1.0 if k == active else 0.75,
                        color="bg" if k == active else "fg", font=VT)
        self._label(snap, "▶▶▶", m, m + 104, 16, 0.85, font=VT, glow=5)
        self._label(snap, "◀◀◀", w - m - 58, m + 104, 16, 0.85, font=VT, glow=5)
        self._label(snap, "SCAN", w / 2 - 34, m + 98, 26, 0.95, font=VT, glow=5)
        # starfield + wireframe planet (turning) + target bracket around a drifting contact
        rnd = random.Random(3)
        for _ in range(70):
            sx, sy = rnd.random() * w, h * 0.22 + rnd.random() * h * 0.38
            a = 0.25 + 0.6 * abs(math.sin(t * rnd.uniform(0.3, 1.4) + rnd.random() * 6))
            sz = 2 if rnd.random() < 0.2 else 1
            snap.append_color(theme.color("fg", a), rect(sx, sy, sz, sz))
        cx, cy, r = w * 0.58, h * 0.56, w * 0.34
        tilt = -0.35                             # north pole tilted toward the viewer
        rot = t * 0.12
        for k in range(12):                      # meridians on the front hemisphere, turning
            lam = rot + k * math.pi / 6
            pb, pen = Gsk.PathBuilder.new(), False
            for j in range(49):
                phi = math.pi * j / 48 - math.pi / 2
                x0, y0, z0 = math.cos(phi) * math.sin(lam), math.sin(phi), math.cos(phi) * math.cos(lam)
                y1, z1 = y0 * math.cos(tilt) - z0 * math.sin(tilt), y0 * math.sin(tilt) + z0 * math.cos(tilt)
                if z1 < 0:
                    pen = False
                    continue
                (pb.line_to if pen else pb.move_to)(cx + r * x0, cy + r * y1)
                pen = True
            snap.append_stroke(pb.to_path(), Gsk.Stroke.new(1.3), theme.color("fg", 0.55))
        for k in range(1, 8):                    # parallels (tilted circles, front half)
            phi = -math.pi / 2 + k * math.pi / 8
            pb, pen = Gsk.PathBuilder.new(), False
            for j in range(73):
                lam = math.tau * j / 72
                x0, y0, z0 = math.cos(phi) * math.sin(lam), math.sin(phi), math.cos(phi) * math.cos(lam)
                y1, z1 = y0 * math.cos(tilt) - z0 * math.sin(tilt), y0 * math.sin(tilt) + z0 * math.cos(tilt)
                if z1 < 0:
                    pen = False
                    continue
                (pb.line_to if pen else pb.move_to)(cx + r * x0, cy + r * y1)
                pen = True
            snap.append_stroke(pb.to_path(), Gsk.Stroke.new(1.2), theme.color("fg", 0.45))
        snap.push_blur(4)
        self._circle(snap, cx, cy, r, 0.9, 3.0)
        snap.pop()
        self._circle(snap, cx, cy, r, 0.85, 1.6)
        bx = w * 0.30 + math.sin(t * 0.07) * w * 0.08
        by = h * 0.32 + math.cos(t * 0.05) * h * 0.03
        for k in range(5):
            snap.append_color(theme.color("fg", 0.9), rect(bx - 6 + (k * 7) % 13, by - 4 + (k * 5) % 9, 3, 3))
        L, g = 14, 26
        for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            x, y = bx + sx * g, by + sy * g * 0.8
            self._line(snap, x, y, x - sx * L, y, 0.95, 2)
            self._line(snap, x, y, x, y - sy * L, 0.95, 2)
        self._label(snap, "--ID DECRYPT--------\nUSCSS RENAISSANCE\nREG. 291835710\nB-CLASS BISHOP CN 812",
                    m + 4, h * 0.235, 11, 0.85, font=VT, glow=5)
        # readout column, right
        px, pw = w - m - 128, 128
        self._box(snap, px, h * 0.235, pw, 150, 0.8, 1.5)
        lines = ["FILES LOADED 8", "ID    : DIR", "002   : Rev_101", "030   : Rev_102", "041   : Rev_103",
                 "", "PLANET ORBIT", f"{743 + int(t) % 9} 32 {523 + int(t * 3) % 40}"]
        for k, ln in enumerate(lines):
            self._label(snap, ln, px + 8, h * 0.235 + 6 + k * 17, 11, 0.85, font=VT)
        if int(t * 2) % 2:
            snap.append_color(theme.color("fg", 0.95), rect(px + 8, h * 0.235 + 6 + 8 * 17 + 4, 9, 13))
        # function-key bar along the bottom (starts on the left), one key lit in turn
        fy = h * 0.80
        keys = ("FN1", "FN2", "FN3", "FN4", "FN5", "SELECT")
        kw = (w - 2 * m) / len(keys)
        lit = int(t / 2.5) % len(keys)
        for k, name in enumerate(keys):
            x = m + k * kw
            self._box(snap, x + 2, fy, kw - 6, 28, 0.9 if k == lit else 0.55, 2 if k == lit else 1.3,
                      fill=0.12 if k == lit else None)
            self._label(snap, name, x + 9, fy + 4, 12, 1.0 if k == lit else 0.7, font=VT)
        # dashed rule + big readouts
        for x in range(m + 8, int(w - m), 30):
            snap.append_color(theme.color("fg", 0.6), rect(x, fy + 46, 14, 3))
        self._label(snap, "POWER\nAVAILABLE", m + 6, fy + 62, 11, 0.8, font=VT)
        self._label(snap, "0", m + 84, fy + 54, 38, 0.95, font=VT, glow=5)
        self._label(snap, "NETWORK\nCAPACITY", w * 0.42, fy + 62, 11, 0.8, font=VT)
        self._label(snap, f"{10 + int(t / 20) % 4}/14", w * 0.42 + 76, fy + 54, 38, 0.95, font=VT, glow=5)
        self._label(snap, "(C) NOTAREAL DATA SYSTEMS", w * 0.30, h - m - 30, 9, 0.45, font=ST)
        # amber console keys (bottom-left) + a slow red alert lamp
        for k in range(3):
            snap.append_color(theme.color("orange", 0.85 if k != int(t) % 3 else 0.45),
                              rect(m + 4 + k * 22, h - m - 26, 16, 10))
        self._circle(snap, w - m - 14, h - m - 21, 6, 0.4 + 0.5 * abs(math.sin(t * 0.8)), fill=True,
                     color="hot-pink")
        # scanlines + vignette over everything
        for y in range(0, int(h), 3):
            snap.append_color(theme.color("bg-deep", 0.55), rect(0, y, w, 1))
        snap.append_radial_gradient(rect(0, 0, w, h), point(w / 2, h / 2), w * 0.85, h * 0.62, 0, 1,
                                    _stops((0.6, theme.color("bg-deep", 0.0)), (1.0, theme.color("bg-deep", 0.85))))

    # --- nasa theme wallpaper: a cream retro space-program sheet, drawn ------------------------

    @staticmethod
    def _ellipse(snap, cx, cy, rx, ry, alpha=0.8, width=1.4, color="fg", a0=0.0, a1=math.tau, dash=None):
        pb = Gsk.PathBuilder.new()
        n = 72
        for j in range(n + 1):
            a = a0 + (a1 - a0) * j / n
            (pb.move_to if j == 0 else pb.line_to)(cx + rx * math.cos(a), cy + ry * math.sin(a))
        st = Gsk.Stroke.new(width)
        if dash:
            st.set_dash(dash)
        snap.append_stroke(pb.to_path(), st, theme.color(color, alpha))

    def _pill(self, snap, x, y, pw, ph, color, alpha=1.0, outline=None):
        rr = Gsk.RoundedRect()
        rr.init_from_rect(rect(x, y, pw, ph), ph / 2)
        snap.push_rounded_clip(rr)
        snap.append_color(theme.color(color, alpha), rect(x, y, pw, ph))
        snap.pop()
        if outline:
            pb = Gsk.PathBuilder.new()
            pb.add_rounded_rect(rr)
            snap.append_stroke(pb.to_path(), Gsk.Stroke.new(1.5), theme.color(outline, 0.9))

    def _sparkle(self, snap, x, y, s, alpha=0.8, color="fg"):
        pb = Gsk.PathBuilder.new()
        pb.move_to(x, y - s)
        pb.quad_to(x, y, x + s, y)
        pb.quad_to(x, y, x, y + s)
        pb.quad_to(x, y, x - s, y)
        pb.quad_to(x, y, x, y - s)
        snap.append_fill(pb.to_path(), Gsk.FillRule.WINDING, theme.color(color, alpha))

    def _rrect(self, snap, x, y, rw, rh, r, color, alpha=1.0):
        rr = Gsk.RoundedRect()
        rr.init_from_rect(rect(x, y, rw, rh), r)
        snap.push_rounded_clip(rr)
        snap.append_color(theme.color(color, alpha), rect(x, y, rw, rh))
        snap.pop()

    def _plate(self, snap, x, y, pw, ph, r=6, color="surface"):
        """A moulded plastic plate: soft shade under it, light top lip, darker bottom lip."""
        self._rrect(snap, x + 1, y + 3, pw, ph, r, "fg-bright", 0.07)
        self._rrect(snap, x, y + 1, pw, ph, r, "fg-bright", 0.10)
        self._rrect(snap, x, y, pw, ph, r, color)
        rr = Gsk.RoundedRect()
        rr.init_from_rect(rect(x, y, pw, ph), r)
        snap.push_rounded_clip(rr)
        snap.append_color(Gdk.RGBA(red=1, green=1, blue=1, alpha=0.45), rect(x, y, pw, 1))
        snap.append_color(theme.color("fg-bright", 0.10), rect(x, y + ph - 2, pw, 2))
        snap.pop()

    def _screw(self, snap, x, y, a=0.6):
        self._circle(snap, x, y + 0.6, 3.4, 0.25, fill=True, color="fg-bright")
        self._circle(snap, x, y, 3.2, 1.0, fill=True, color="bg-deep")
        self._line(snap, x - 2.2 * math.cos(a), y - 2.2 * math.sin(a),
                   x + 2.2 * math.cos(a), y + 2.2 * math.sin(a), 0.55, 1.0, color="fg-bright")

    def _seam(self, snap, x1, x2, y):
        """A recessed panel seam: dark line with a light line under it."""
        snap.append_color(theme.color("fg-bright", 0.16), rect(x1, y, x2 - x1, 1))
        snap.append_color(Gdk.RGBA(red=1, green=1, blue=1, alpha=0.35), rect(x1, y + 1, x2 - x1, 1))

    def _keycap(self, snap, x, y, kw, kh, base="surface", legend=None, sub=None, ink="fg", r=6, lit=False):
        """A 70s keycap: soft shadow, darker skirt, a lighter face lit from above, printed legend."""
        self._rrect(snap, x, y + 3, kw, kh, r, "fg-bright", 0.16)               # shadow on the panel
        if lit:                                                                  # lamp key: glow around
            snap.push_blur(9)
            self._rrect(snap, x - 2, y - 2, kw + 4, kh + 4, r + 2, base, 0.8)
            snap.pop()
        self._rrect(snap, x, y, kw, kh, r, base)                                 # skirt
        self._rrect(snap, x, y + kh - 5, kw, 5, min(r, 3), "fg-bright", 0.10)
        fr = Gsk.RoundedRect()
        fr.init_from_rect(rect(x + 2, y + 1, kw - 4, kh - 6), max(r - 1, 1))
        snap.push_rounded_clip(fr)
        snap.append_linear_gradient(rect(x + 2, y + 1, kw - 4, kh - 6), point(0, y), point(0, y + kh),
                                    _stops((0.0, Gdk.RGBA(red=1, green=1, blue=1, alpha=0.6 if not lit else 0.75)),
                                           (0.5, Gdk.RGBA(red=1, green=1, blue=1, alpha=0.0)),
                                           (1.0, Gdk.RGBA(red=0, green=0, blue=0, alpha=0.04))))
        snap.pop()
        if legend:
            self._label(snap, legend, x + 7, y + 5, 8, 0.85, bold=True, color=ink, font="Adwaita Sans")
        if sub:
            self._label(snap, sub, x + 7, y + kh - 17, 4.5, 0.6, color=ink, font="Adwaita Sans")

    def _slab(self, snap, x, y, sw, sh, r, color="surface", lift=1.0):
        """A smooth raised shape: a wide, faint blurred shadow under a plain fill. No edges."""
        snap.push_blur(18 * lift)
        self._rrect(snap, x + 2, y + 10 * lift, sw - 4, sh, r, "fg-bright", 0.10)
        snap.pop()
        self._rrect(snap, x, y, sw, sh, r, color)

    def _nasa_static(self, w, h):
        """Everything that doesn't move, drawn once into a render node (the blurs are not cheap)."""
        key = (w, h, theme.hex("bg"), theme.hex("primary"))
        cache = getattr(self, "_nasa_cache", None)
        if cache and cache[0] == key:
            return cache[1]
        AS = "Adwaita Sans"
        snap = Gtk.Snapshot()
        snap.append_color(theme.color("bg"), rect(0, 0, w, h))
        snap.append_linear_gradient(rect(0, 0, w, h), point(0, 0), point(0, h), _stops(
            (0.0, Gdk.RGBA(red=1, green=1, blue=1, alpha=0.45)), (1.0, Gdk.RGBA(red=1, green=1, blue=1, alpha=0.0))))
        m = 22
        self._label(snap, "NOTAREAL", m, m + 30, 19, 0.92, bold=True, color="fg", font=AS)
        self._label(snap, "SPACE SYSTEMS  —  NR-1", m + 1, m + 62, 7, 0.55, color="fg", font=AS)
        self._rrect(snap, w - m - 14, m + 38, 14, 14, 7, "primary")
        # main slab with a dial
        sx, sy, sw, sh = m, m + 110, w - 2 * m, h * 0.40
        self._slab(snap, sx, sy, sw, sh, 34)
        cx, cy, cr = sx + sw / 2, sy + sh * 0.50, min(sw, sh) * 0.34
        well = Gsk.RoundedRect()                                 # a soft dished well
        well.init_from_rect(rect(cx - cr, cy - cr, 2 * cr, 2 * cr), cr)
        snap.push_rounded_clip(well)
        snap.append_radial_gradient(rect(cx - cr, cy - cr, 2 * cr, 2 * cr), point(cx, cy + cr * 0.15), cr * 1.15,
                                    cr * 1.15, 0, 1, _stops((0.0, theme.color("surface", 1.0)),
                                                            (0.75, theme.color("bg", 1.0)),
                                                            (1.0, theme.color("bg-deep", 1.0))))
        snap.pop()
        for k in range(60):
            a = k * math.tau / 60
            r1 = cr * (1.10 if k % 5 else 1.06)
            self._line(snap, cx + r1 * math.cos(a), cy + r1 * math.sin(a),
                       cx + cr * 1.15 * math.cos(a), cy + cr * 1.15 * math.sin(a), 0.22 if k % 5 else 0.4, 1.2)
        self._label(snap, "ORBIT", sx + 22, sy + 20, 7, 0.5, bold=True, color="fg", font=AS)
        self._label(snap, "TRACK 01", sx + sw - 74, sy + 20, 7, 0.5, bold=True, color="fg", font=AS)
        # three smooth keys, the orange one on the LEFT (left-handed)
        ky, kh = sy + sh + 34, 64
        kw = (w - 2 * m - 2 * 14) / 3
        for k, (name, col, ink) in enumerate((("GO", "primary", "surface"), ("HOLD", "surface", "fg"),
                                              ("MENU", "surface", "fg"))):
            x = m + k * (kw + 14)
            self._slab(snap, x, ky, kw, kh, 20, col, lift=0.7)
            self._label(snap, name, x + 18, ky + 21, 9, 0.9, bold=True, color=ink, font=AS)
        # slider track
        ty = ky + kh + 54
        self._rrect(snap, m, ty, w - 2 * m, 4, 2, "fg", 0.08)
        self._label(snap, "THRUST", m, ty + 16, 6.5, 0.5, bold=True, color="fg", font=AS)
        self._label(snap, "AD ASTRA PER NOTAREAL", m, h - m - 26, 6, 0.4, bold=True, color="fg", font=AS)
        node = snap.to_node()
        self._nasa_cache = (key, node, (cx, cy, cr, ty))
        return node

    def _nasa(self, snap, w, h):
        """NASA v4 (10-17): smooth whites with splashes of orange - soft white slabs lifted by wide
        faint shadows, an orange dot circling a dial, an orange slider, small grey type. No
        outlines. The still parts are cached (_nasa_static); only the dot + slider redraw."""
        t = self.t
        node = self._nasa_static(w, h)
        if node:
            snap.append_node(node)
        cx, cy, cr, ty = self._nasa_cache[2]
        a = t * 0.25 - math.pi / 2
        ox, oy = cx + cr * 0.78 * math.cos(a), cy + cr * 0.78 * math.sin(a)
        self._circle(snap, ox, oy + 3, 11, 0.12, fill=True, color="fg-bright")
        self._circle(snap, ox, oy, 11, 1.0, fill=True, color="primary")
        self._label(snap, f"{int((t * 0.25 / math.tau * 360) % 360):03d}°", cx - 26, cy - 14, 15, 0.85,
                    bold=True, color="fg", font="Adwaita Sans")
        m = 22
        fx = m + (w - 2 * m) * (0.35 + 0.25 * math.sin(t * 0.18))
        self._rrect(snap, m, ty, fx - m, 4, 2, "primary")
        self._circle(snap, fx, ty + 5, 10, 0.12, fill=True, color="fg-bright")
        self._circle(snap, fx, ty + 2, 10, 1.0, fill=True, color="surface")
        self._circle(snap, fx, ty + 2, 4, 1.0, fill=True, color="primary")


def _stops(*pairs):
    out = []
    for off, col in pairs:
        cs = Gsk.ColorStop()
        cs.offset, cs.color = off, col
        out.append(cs)
    return out


DRAWN_WALLPAPERS = {"micro": "_micro", "alien": "_alien", "nasa": "_nasa"}


class PhantomShell(Gtk.Application):
    SETTLE_S = 2.0
    TOPLEVEL_WATCH = os.path.expanduser("~/overview-widget/toplevel-watch")

    def _watch_apps(self, win):
        """Unmap the home screen while an app covers it. A mapped-but-covered layer surface gets no
        frame callbacks, so after a rotation phoc kept the screen black waiting for it (3 s timeout)."""
        self.app_active = False
        self._hide_timer = 0
        if not os.path.exists(self.TOPLEVEL_WATCH):
            return
        proc = Gio.Subprocess.new([self.TOPLEVEL_WATCH], Gio.SubprocessFlags.STDOUT_PIPE)
        stream = Gio.DataInputStream.new(proc.get_stdout_pipe())

        def on_line(src, res):
            line, _ = src.read_line_finish_utf8(res)
            if line is None:          # watcher died: stay visible, like before
                self.app_active = False
                self._update_mapped()
                return
            self.app_active = line.strip() != "none"
            self._update_mapped()
            src.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)
        self._toplevel_proc = proc

    def _update_mapped(self):
        if self._hide_timer:
            GLib.source_remove(self._hide_timer)
            self._hide_timer = 0
        if self.app_active and not self.summoned:
            def hide():
                self._hide_timer = 0
                if self.app_active and not self.summoned:
                    self.window.set_visible(False)
                return False
            self._hide_timer = GLib.timeout_add(300, hide)   # debounce app switches
        elif not self.window.get_visible():
            self.window.present()

    def _settle_frames(self, win):
        """After a rotation phoc blanks the screen until every layer surface has committed a frame for
        its latest configure, else it waits out a 3 s timeout. A repeated configure with an unchanged
        size makes GTK ack without drawing, so keep drawing for a moment after any size/monitor change."""
        st = {"until": 0.0, "tick": 0}

        def poke(*_):
            st["until"] = time.monotonic() + self.SETTLE_S
            if not st["tick"]:
                st["tick"] = win.add_tick_callback(tick)

        def tick(w, _clock):
            w.queue_draw()
            self.bg.queue_draw()
            if time.monotonic() > st["until"]:
                st["tick"] = 0
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        def layout(_surface, w, h):
            # only a real size change (rotation); re-showing over the overview at the same size
            # used to cost 2 s of full-screen redraws right when the user starts swiping
            if (w, h) != st.get("size"):
                st["size"] = (w, h)
                poke()
        win.connect("realize", lambda w: w.get_surface().connect("layout", layout))
        Gdk.Display.get_default().get_monitors().connect("items-changed", poke)
        Gio.bus_get_sync(Gio.BusType.SESSION).signal_subscribe(
            "org.gnome.Mutter.DisplayConfig", "org.gnome.Mutter.DisplayConfig", "MonitorsChanged",
            "/org/gnome/Mutter/DisplayConfig", None, Gio.DBusSignalFlags.NONE, poke)

    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.apps = Apps()
        self.summoned = False
        self.window = None

    # --- UI --------------------------------------------------------------

    def do_activate(self):
        if self.window:
            return
        Gtk.Settings.get_default().set_property("gtk-entry-select-on-focus", False)
        theme.add_css(CSS)

        win = Gtk.ApplicationWindow(application=self)
        win.add_css_class("phantom")
        LayerShell.init_for_window(win)
        LayerShell.set_namespace(win, "phantomshell")
        for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM,
                     LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(win, edge, True)
        self.window = win
        self._settle_frames(win)
        self._watch_apps(win)

        overlay = Gtk.Overlay()
        win.set_child(overlay)
        self.bg = VaporBackground()  # tap on the empty background summons the search box
        tap = Gtk.GestureClick()
        tap.connect("released", lambda *a: self.summon())
        self.bg.add_controller(tap)
        overlay.set_child(self.bg)
        theme.on_change(self.bg.queue_draw)   # `nrtheme <name>`: new wallpaper + colors
        self.windows = []

        self.column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                              margin_top=TOP_CLEARANCE, margin_start=18, margin_end=18, margin_bottom=14)
        overlay.add_overlay(self.column)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               vexpand=True, valign=Gtk.Align.FILL)
        self.stack.add_named(self._search_panel(), "search")
        self.stack.add_named(self._apps_panel(), "apps")
        self.column.append(self.stack)
        # footer (disclaimer + kanji) removed 10-17; _footer() kept
        # self.column.append(self._footer())

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        win.add_controller(keys)

        self._set_mode(summoned=False)
        win.present()
        self._register_dbus()
        self._watch_home_swipe()
        # voiced / alarmd change reminders.json behind our back
        self._rem_mon = Gio.File.new_for_path(REMINDERS_FILE).monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
        self._rem_mon.connect("changed", lambda *a: self.summoned and GLib.timeout_add(200, once(self.refresh)))

    def _watch_home_swipe(self):
        """Dismiss when Phosh's bottom-edge swipe (overview) happens while summoned."""
        try:
            fd = os.open(TOUCH_DEVICE, os.O_RDONLY | os.O_NONBLOCK)
            absinfo = bytearray(24)
            fcntl.ioctl(fd, EVIOCGABS_Y, absinfo)
        except OSError as err:
            print("phantomshell: no touch input:", err, file=sys.stderr)
            return
        _value, ymin, ymax = struct.unpack_from("iii", absinfo)
        edge = ymin + (ymax - ymin) * EDGE_START
        travel = (ymax - ymin) * EDGE_SWIPE
        touch = {"start": None, "last": None}

        def on_input(_fd, _cond):
            try:
                data = os.read(fd, EVENT.size * 64)
            except BlockingIOError:
                return True
            for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _s, _us, etype, code, value = EVENT.unpack_from(data, off)
                if etype == EV_ABS and code == ABS_MT_POSITION_Y:
                    if touch["start"] is None:
                        touch["start"] = value
                    touch["last"] = value
                    if (self.summoned and touch["start"] > edge
                            and touch["start"] - value > travel):
                        touch["start"] = -1  # once per touch
                        self.dismiss()
                elif etype == EV_KEY and code == BTN_TOUCH and not value:
                    # ABS events of the next touch come before its BTN_TOUCH down
                    touch["start"] = touch["last"] = None
            return True

        GLib.io_add_watch(fd, GLib.PRIORITY_DEFAULT, GLib.IO_IN, on_input)

    def _panel(self, title, on_close=None):
        """Retro window with an offset ghost layer; returns (outer widget, body box)."""
        outer = Gtk.Overlay()
        ghost = Gtk.Box(margin_start=12, margin_top=12)
        ghost.add_css_class("ghost")
        outer.set_child(ghost)
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_end=12, margin_bottom=12)
        frame.add_css_class("retro")
        outer.add_overlay(frame)
        outer.set_measure_overlay(frame, True)

        bar = Gtk.Box(spacing=4)
        bar.add_css_class("titlebar")
        label = Gtk.Label(label=title, xalign=0, hexpand=True, ellipsize=3)
        label.add_css_class("title")
        bar.append(label)
        if on_close:
            close = Gtk.Button(label="✕", valign=Gtk.Align.CENTER)
            close.add_css_class("tb-btn")
            close.connect("clicked", lambda *a: on_close())
            bar.append(close)
        frame.append(bar)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.add_css_class("retro-body")
        frame.append(body)
        self.windows.append(outer)
        return outer, body

    def _search_panel(self):
        outer, panel = self._panel("SEARCH", lambda: self.dismiss())
        self.entry = Gtk.Entry(hexpand=True, placeholder_text="SEARCH THE WEB...")
        self.entry.add_css_class("web")
        self.entry.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, "system-search-symbolic")
        self.entry.set_input_hints(Gtk.InputHints.NO_EMOJI)
        self.entry.connect("activate", self._on_enter)
        self.entry.connect("changed", self._on_typing)
        entry_tap = Gtk.GestureClick()
        entry_tap.connect("pressed", lambda *a: None if self.summoned else self.summon())
        self.entry.add_controller(entry_tap)
        panel.append(self.entry)

        self.suggest = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                   max_children_per_line=3, column_spacing=6, row_spacing=6)
        panel.append(self.suggest)
        self.status = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.status.add_css_class("status")
        panel.append(self.status)

        rule = Gtk.Box()
        rule.add_css_class("rule")
        panel.append(rule)
        self.lists = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.lists)
        panel.append(scroll)
        return outer

    def refresh(self):
        """Rebuild the reminders + notes lists (only while summoned)."""
        box = self.lists
        while box.get_first_child():
            box.remove(box.get_first_child())

        def section(title):
            lab = Gtk.Label(label=title, xalign=0)
            lab.add_css_class("section")
            box.append(lab)

        def item(when, text, on_delete):
            row = Gtk.Box(spacing=8)
            rm = Gtk.Button(label="✕", valign=Gtk.Align.CENTER)   # left side: user is left-handed
            rm.add_css_class("del")
            rm.connect("clicked", lambda *a: (on_delete(), self.refresh()))
            row.append(rm)
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            if when:
                w = Gtk.Label(label=when, xalign=0)
                w.add_css_class("item-when")
                col.append(w)
            t = Gtk.Label(label=text, xalign=0, wrap=True, wrap_mode=2)
            t.add_css_class("item-text")
            col.append(t)
            row.append(col)
            box.append(row)

        def empty(text):
            lab = Gtk.Label(label=text, xalign=0)
            lab.add_css_class("empty")
            box.append(lab)

        section("REMINDERS")
        items = sorted(load_json(REMINDERS_FILE), key=lambda r: r["due"])
        for r in items:
            item(fmt_due(r["due"]), r["text"], lambda r=r: remove_reminder(r))
        if not items:
            empty('NONE · "REMIND 10M CALL DAD" ABOVE, OR ASK BY VOICE')

        section("NOTES")
        notes = list(reversed(_notes()))[:NOTES_SHOWN]   # newest first
        for n in notes:
            stamp = datetime.datetime.fromtimestamp(n["time"]).strftime("%d %b").upper()
            item(stamp, n["text"].strip(), lambda n=n: delete_note(n))
        if not notes:
            empty('NONE · "NOTE BUY MILK" ABOVE')
        return False

    def _flash(self, text):
        self.status.set_label(text.upper())
        self.status.set_visible(True)

    def _on_typing(self, entry):
        self.suggest.remove_all()
        text = entry.get_text().strip()
        if not text or text.split()[0].lower() in ("remind", "reminder", "timer", "note"):
            return
        for app in self.apps.match(text, limit=3):
            chip = Gtk.Button(label=app.get_display_name().upper())
            chip.add_css_class("chip")
            chip.connect("clicked", lambda b, a=app: self.launch(a))
            self.suggest.append(chip)

    def _on_enter(self, entry):
        text = entry.get_text().strip()
        if not text:
            return
        word, _, rest = text.partition(" ")
        word, rest = word.lower(), rest.strip()
        if word in ("remind", "reminder", "timer"):
            words = rest.split()
            due, used = parse_when(words)
            label = " ".join(words[used:])
            if word == "timer":
                label = label or f"TIMER {words[0].upper() if words else ''} DONE"
            if not due or not label:
                self._flash("REMIND <10M|2H|17:30|TOMORROW [9:00]> <TEXT>  ·  TIMER <5M> [LABEL]")
                return
            items = load_json(REMINDERS_FILE)
            items.append({"due": due.timestamp(), "text": label})
            save_json(REMINDERS_FILE, sorted(items, key=lambda r: r["due"]))
            self._flash(f"SET FOR {fmt_due(due.timestamp())}")
        elif word == "note" and rest:
            add_note(rest)
            self._flash("NOTE SAVED")
        else:
            self.web_search(text)
            return
        entry.set_text("")
        self.refresh()

    def web_search(self, query):
        url = "https://www.google.com/search?q=" + GLib.uri_escape_string(query, None, False)
        try:
            Gio.AppInfo.launch_default_for_uri(url, launch_context())
        except GLib.Error as err:
            self._flash(f"FAILED: {err.message}")
            return
        self.dismiss()

    def _apps_panel(self):
        outer, panel = self._panel("APPLICATIONS", lambda: self.stack.set_visible_child_name("search"))
        self.app_search = Gtk.SearchEntry(placeholder_text="SEARCH APPS...")
        self.app_grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                    min_children_per_line=4, max_children_per_line=4,
                                    column_spacing=8, row_spacing=8, valign=Gtk.Align.START)
        self.app_grid.set_filter_func(self._filter_app)
        for app in self.apps.apps:
            tile = Gtk.Button()
            tile.add_css_class("app-tile")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.FILL,
                          vexpand=True)
            box.add_css_class("keycap")
            icon = Gtk.Image.new_from_gicon(app.get_icon()) if app.get_icon() \
                else Gtk.Image.new_from_icon_name("application-x-executable")
            icon.set_pixel_size(36)
            icon.set_vexpand(True)
            name = Gtk.Label(label=app.get_display_name().upper(), ellipsize=3,
                             max_width_chars=8, justify=Gtk.Justification.CENTER)
            name.add_css_class("app-tile-name")
            box.append(icon)
            box.append(name)
            tile.set_child(box)
            tile.connect("clicked", lambda b, a=app: self.launch(a))
            square = Gtk.AspectFrame(ratio=1.0, obey_child=False)
            square.set_child(tile)
            child = Gtk.FlowBoxChild()
            child.app = app
            child.set_child(square)
            self.app_grid.append(child)
        scroll = Gtk.ScrolledWindow(min_content_height=420, vexpand=True,
                                    hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.app_grid)
        panel.append(scroll)
        self.app_search.add_css_class("search")
        self.app_search.connect("search-changed", lambda *a: self.app_grid.invalidate_filter())
        self.app_search.connect("activate", self._launch_first_visible)
        panel.append(self.app_search)
        return outer

    def _filter_app(self, child):
        q = self.app_search.get_text().lower().strip()
        app = child.app
        return not q or q in app.get_display_name().lower() or q in (app.get_id() or "").lower()

    def _launch_first_visible(self, *a):
        q = self.app_search.get_text()
        for app in self.apps.apps:
            if not q or q.lower() in app.get_display_name().lower() or q.lower() in (app.get_id() or "").lower():
                self.launch(app)
                return

    def _footer(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_bottom=18)
        t = Gtk.Label(label="WARNING")
        t.add_css_class("warning-title")
        w = Gtk.Label(label="THE DEVELOPER IS LEGALLY REQUIRED TO INFORM YOU THAT THIS LAUNCHER GOES HARD\n"
                            "CONTINUED USE INDICATES THAT YOU HAVE EXCEEDED THE RECOGNIZED LIMITS OF ORDINARY COOL",
                      justify=Gtk.Justification.CENTER)
        w.add_css_class("warning")
        k = Gtk.Label(label="識知人機")
        k.add_css_class("kanji")
        # WARNING disclaimer removed 10-17 (t, w kept for an easy revert)
        for x in (k,):
            box.append(x)
        return box

    # --- modes -----------------------------------------------------------

    def _set_mode(self, summoned):
        self.summoned = summoned
        win = self.window
        # the overview widget hides its joystick while summoned (it covers the panel)
        bus = self.get_dbus_connection()
        if bus:
            bus.emit_signal(None, OBJ_PATH, APP_ID, "SummonedChanged", GLib.Variant("(b)", (summoned,)))
        if summoned:
            win.add_css_class("summoned")
            LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
            LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.EXCLUSIVE)
            if self.stack.get_visible_child_name() == "search":
                self.refresh()
                GLib.idle_add(once(self.entry.grab_focus_without_selecting))
            self.bg.start()
            self._play_appear()
        else:
            win.remove_css_class("summoned")
            self.bg.stop()
            LayerShell.set_layer(win, LayerShell.Layer.BOTTOM)
            # phoc speaks layer-shell v3: no ON_DEMAND, so the home screen takes no
            # keyboard; tapping it summons the search box instead.
            LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)

    def _play_appear(self):
        for w in self.windows:
            w.remove_css_class("appear")
        # re-adding the class on a later frame restarts the CSS animation
        GLib.timeout_add(30, lambda: [w.add_css_class("appear") for w in self.windows] and False)

    def summon(self):
        if not self.window.get_visible():
            self.window.present()
        if not self.summoned:
            self._set_mode(True)

    def dismiss(self):
        self.entry.set_text("")
        self.status.set_visible(False)
        self.stack.set_visible_child_name("search")
        if self.summoned:
            self._set_mode(False)
            if getattr(self, "app_active", False):
                self._update_mapped()

    def toggle(self):
        self.dismiss() if self.summoned else self.summon()

    def show_apps(self):
        self.summon()
        self.app_search.set_text("")
        self.stack.set_visible_child_name("apps")
        self._play_appear()
        self.app_search.grab_focus()

    def _on_key(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self.dismiss()
            return True
        return False

    def open_by_name(self, query):
        """Fuzzy-launch an app (used by ~/.local/bin/launch)."""
        self.apps.reload()  # pick up apps installed since notarealShell started
        matches = self.apps.match(query, limit=1)
        if not matches:
            return f"no app matches '{query}'"
        app = matches[0]
        self.launch(app)
        return f"opening {app.get_display_name()}"

    def launch(self, app):
        try:
            app.launch([], launch_context())
        except GLib.Error as err:
            self._flash(f"FAILED: {err.message}")
            return
        self.app_search.set_text("")
        self.dismiss()

    # --- D-Bus -------------------------------------------------------------

    def _register_dbus(self):
        bus = self.get_dbus_connection()
        node = Gio.DBusNodeInfo.new_for_xml(IFACE_XML)

        def on_call(conn, sender, path, iface, method, params, inv):
            if method == "Open":
                inv.return_value(GLib.Variant("(s)", (self.open_by_name(params.unpack()[0]),)))
                return
            {"Summon": self.summon, "Dismiss": self.dismiss, "Toggle": self.toggle,
             "ShowApps": self.show_apps}[method]()   # ShowApps: the joystick's "all apps" slot
            inv.return_value(None)

        bus.register_object(OBJ_PATH, node.interfaces[0], on_call, None, None)


# --- notes and reminders ----------------------------------------------------
# Reminders are fired by ~/alarm/alarmd now; here they are only listed, added and deleted.

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def parse_when(words):
    """'10m' | '2h' | '17:30' | 'tomorrow [9:00]' | 'in 5m' -> (datetime, words used)."""
    now = datetime.datetime.now()
    i = 1 if words and words[0].lower() == "in" else 0
    if i >= len(words):
        return None, 0
    w = words[i].lower()
    m = re.fullmatch(r"(\d+)(s|m|min|h|d)", w)
    if m:
        n, unit = int(m[1]), m[2]
        delta = {"s": "seconds", "m": "minutes", "min": "minutes", "h": "hours", "d": "days"}[unit]
        return now + datetime.timedelta(**{delta: n}), i + 1
    if w == "tomorrow":
        day = now + datetime.timedelta(days=1)
        hhmm = re.fullmatch(r"(\d{1,2})[:.](\d{2})", words[i + 1]) if i + 1 < len(words) else None
        hour, minute, used = (int(hhmm[1]), int(hhmm[2]), i + 2) if hhmm else (9, 0, i + 1)
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0), used
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", w)
    if m:
        due = now.replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
        if due <= now:
            due += datetime.timedelta(days=1)
        return due, i + 1
    return None, 0


def fmt_due(ts):
    due = datetime.datetime.fromtimestamp(ts)
    today = datetime.date.today()
    if due.date() == today:
        return due.strftime("TODAY %H:%M")
    if due.date() == today + datetime.timedelta(days=1):
        return due.strftime("TOMORROW %H:%M")
    return due.strftime("%a %d %b %H:%M").upper()


# Notes live in the Iotas notes app (via ~/voice/iotas_notes.py); notes.json is only the
# fallback when Iotas isn't installed. `note clear` only removes notes made here or by
# voice (Iotas category "notarealShell"), never ones written in Iotas.
sys.path.insert(0, os.path.expanduser("~/voice"))
try:
    import iotas_notes  # noqa: E402
except ImportError:
    iotas_notes = None


def _iotas():
    return iotas_notes is not None and iotas_notes.available()


def _notes():
    """Oldest first: [{"time", "text", "id"?}]."""
    if _iotas():
        return list(reversed(iotas_notes.recent()))
    return load_json(NOTES_FILE)


def remove_reminder(r):
    save_json(REMINDERS_FILE, [x for x in load_json(REMINDERS_FILE)
                               if not (x.get("due") == r["due"] and x.get("text") == r["text"])])


def add_note(text):
    if _iotas():
        iotas_notes.add(text)
    else:
        notes = load_json(NOTES_FILE)
        notes.append({"time": time.time(), "text": text})
        save_json(NOTES_FILE, notes)


def delete_note(n):
    if _iotas() and "id" in n:
        iotas_notes.delete(n["id"])
    else:
        save_json(NOTES_FILE, [x for x in load_json(NOTES_FILE)
                               if not (x.get("time") == n["time"] and x.get("text") == n["text"])])


if __name__ == "__main__":
    sys.exit(PhantomShell().run(None))
