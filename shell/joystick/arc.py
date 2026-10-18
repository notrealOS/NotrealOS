#!/usr/bin/env python3
"""arc: a left-handed thumb-arc launcher.

Put your thumb in the bottom-left corner and drag up/out: apps fan out in two rings around the
corner, most used on the inner ring, nearest the thumb. Slide onto one (it grows and glows, its
name shows), let go to open it. Let go back near the corner to cancel. A tap on the corner opens
the arc and leaves it up for tapping; tap empty space to close. The last outer slot (⋯) opens
notarealShell's full app list.

The touch starts on our own small corner surface, so the whole drag belongs to us and the app
below never sees it. Usage is learned from toplevel-watch: every time an app's window becomes
active it scores a point, decaying with a 7-day half-life.
"""
import json
import math
import os
import subprocess
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.expanduser("~/.local/share/arc")
USAGE = os.path.join(DATA, "usage.json")
TOPLEVEL_WATCH = os.path.expanduser("~/overview-widget/toplevel-watch")
os.makedirs(DATA, exist_ok=True)

CORNER = 44            # logical px: the touch zone in the bottom-left corner
HALF_LIFE = 7 * 86400
OPEN_MS = 170
# rings: (radius as a fraction of the arc size, icon px, slot angles in degrees from straight up,
# listed in comfort order: the best-ranked app gets the first angle). Angles stay <= 70 deg so a
# drag never looks like lisgd's flat bottom-edge swipe.
RINGS = [
    (0.40, 60, [38, 58, 18, 72]),
    (0.72, 52, [30, 46, 14, 62, 76]),
]
MORE = "__more__"


def rgba(spec, a=None):
    c = Gdk.RGBA()
    c.parse(spec)
    if a is not None:
        c.alpha = a
    return c


def stop(offset, color):
    s = Gsk.ColorStop()
    s.offset, s.color = offset, color
    return s


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# --- usage ------------------------------------------------------------------------------
class Usage:
    def __init__(self):
        try:
            with open(USAGE) as f:
                self.data = json.load(f)       # desktop id -> [score, last_ts]
        except (OSError, ValueError):
            self.data = {}
        self.map = {}
        self.save_id = 0

    def desktop_for(self, app_id):
        if app_id in self.map:
            return self.map[app_id]
        found = None
        try:
            info = Gio.DesktopAppInfo.new(app_id + ".desktop")
        except TypeError:                      # PyGObject raises instead of returning NULL
            info = None
        if info:
            found = info.get_id()
        else:
            low = app_id.lower()
            for a in Gio.AppInfo.get_all():
                wm = a.get_startup_wm_class() if isinstance(a, Gio.DesktopAppInfo) else None
                ex = os.path.basename(a.get_executable() or "")
                if (wm and wm.lower() == low) or a.get_id().lower() == low + ".desktop" or ex == low:
                    found = a.get_id()
                    break
        self.map[app_id] = found
        return found

    def score(self, did):
        s, last = self.data.get(did, [0.0, 0])
        return s * 0.5 ** ((time.time() - last) / HALF_LIFE)

    def bump(self, did):
        self.data[did] = [self.score(did) + 1.0, time.time()]
        if not self.save_id:
            self.save_id = GLib.timeout_add_seconds(10, self.save)

    def save(self):
        self.save_id = 0
        tmp = USAGE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, USAGE)
        return False

    def ranked(self, n):
        """n app infos: most used first, topped up with Phosh favourites, then alphabetical."""
        apps = {a.get_id(): a for a in Gio.AppInfo.get_all() if a.should_show()}
        order = sorted((d for d in self.data if d in apps), key=self.score, reverse=True)
        try:
            favs = Gio.Settings.new("sm.puri.phosh").get_strv("favorites")
        except Exception:  # noqa: BLE001
            favs = []
        for d in favs + sorted(apps, key=lambda d: apps[d].get_display_name().lower()):
            if d in apps and d not in order:
                order.append(d)
        return [apps[d] for d in order[:n]]


# --- the arc ----------------------------------------------------------------------------
class ArcView(Gtk.Widget):
    def __init__(self, launcher):
        super().__init__()
        self.launcher = launcher
        self.slots = []          # (app or MORE, ring index, angle, paintable)
        self.sel = None
        self.progress = 0.0
        self.opened_at = 0.0
        self.tick = None
        self.sel_glow = 0.0      # eased 0..1 for the selected slot
        self.last_sel = None

    def set_apps(self, apps):
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        self.slots = []
        it = iter(apps)
        for ri, (_frac, px, angles) in enumerate(RINGS):
            for k, ang in enumerate(angles):
                if ri == len(RINGS) - 1 and k == len(angles) - 1:
                    self.slots.append((MORE, ri, ang, None))
                    continue
                app = next(it, None)
                if app is None:
                    continue
                gicon = app.get_icon() or Gio.ThemedIcon.new("application-x-executable")
                paint = theme.lookup_by_gicon(gicon, px, max(2, self.get_scale_factor()), Gtk.TextDirection.NONE,
                                              Gtk.IconLookupFlags(0))
                self.slots.append((app, ri, ang, paint))
        self.sel = None

    def open(self):
        self.progress = 0.0
        self.opened_at = time.monotonic()
        self.sel = None
        if self.tick is None:
            self.tick = self.add_tick_callback(self._on_tick)

    def _on_tick(self, _w, _clock):
        t = (time.monotonic() - self.opened_at) * 1000 / OPEN_MS
        self.progress = 1 - (1 - min(1.0, t)) ** 3          # ease-out cubic
        target = 1.0 if self.sel is not None else 0.0
        self.sel_glow += (target - self.sel_glow) * 0.35
        self.queue_draw()
        if self.progress >= 1.0 and abs(target - self.sel_glow) < 0.01:
            self.sel_glow = target
            self.tick = None
            return False
        return True

    def _size(self):
        return min(self.get_width(), self.get_height())

    def slot_pos(self, ri, ang, prog=1.0):
        S = self._size()
        r = RINGS[ri][0] * S * (0.55 + 0.45 * prog)
        a = math.radians(ang)
        return r * math.sin(a), self.get_height() - r * math.cos(a)

    def pick(self, px, py):
        """Slot under a point given relative to the corner (x right, y UP); None if none."""
        S = self._size()
        r = math.hypot(px, py)
        if r < 0.20 * S or r > 1.15 * S:
            return None
        ri = 0 if r < (RINGS[0][0] + RINGS[1][0]) / 2 * S else 1
        ang = math.degrees(math.atan2(px, py))
        best = None
        for i, (_app, sri, sang, _p) in enumerate(self.slots):
            if sri == ri and (best is None or abs(sang - ang) < abs(self.slots[best][2] - ang)):
                best = i
        if best is not None and abs(self.slots[best][2] - ang) > 16:
            return None
        return best

    def select(self, i):
        if i != self.sel:
            self.sel = i
            if self.tick is None:
                self.tick = self.add_tick_callback(self._on_tick)

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        S = self._size()
        if S <= 0:
            return
        p = self.progress
        corner = Graphene.Point().init(0, h)
        # navy-to-violet glow out of the corner, built from stacked discs (outermost first)
        grow = 0.6 + 0.4 * p
        for frac, col, a in ((1.04, "#3a2a78", 0.10), (1.00, "#33286e", 0.16), (0.96, "#2a2460", 0.30),
                             (0.92, "#232457", 0.55), (0.80, "#1c2340", 0.45), (0.55, "#161c36", 0.40)):
            pb = Gsk.PathBuilder.new()
            pb.add_circle(corner, frac * S * grow)
            snap.append_fill(pb.to_path(), Gsk.FillRule.WINDING, rgba(col, a * p))
        pb = Gsk.PathBuilder.new()
        pb.add_circle(corner, 0.92 * S * grow)
        snap.append_stroke(pb.to_path(), Gsk.Stroke.new(1.5), rgba("#c5a0ff", 0.35 * p))
        # faint ring guides
        for frac, _px, _a in RINGS:
            pb = Gsk.PathBuilder.new()
            pb.add_circle(corner, frac * S * (0.55 + 0.45 * p))
            snap.append_stroke(pb.to_path(), Gsk.Stroke.new(1.0), rgba("#c5a0ff", 0.16 * p))
        for i, (app, ri, ang, paint) in enumerate(self.slots):
            x, y = self.slot_pos(ri, ang, p)
            size = RINGS[ri][1]
            selected = i == self.sel
            grow = 1.0 + (0.38 * self.sel_glow if selected else 0.0)
            if self.sel is not None and not selected:
                alpha = 0.55
            else:
                alpha = 1.0
            if selected:
                g = size * 1.25 * grow
                snap.append_radial_gradient(
                    Graphene.Rect().init(x - g, y - g, 2 * g, 2 * g), Graphene.Point().init(x, y), g, g, 0.0, 1.0,
                    [stop(0.0, rgba("#f7a1e8", 0.55 * self.sel_glow)), stop(0.45, rgba("#c5a0ff", 0.35 * self.sel_glow)),
                     stop(1.0, rgba("#6fd6f2", 0.0))])
            snap.push_opacity(alpha * min(1.0, p * 1.4))
            d = size * grow
            if app == MORE:
                self._text(snap, "⋯", x, y, 22 * grow, "#eef0ff", center=True)
            elif paint is not None:
                snap.save()
                snap.translate(Graphene.Point().init(x - d / 2, y - d / 2))
                paint.snapshot(snap, d, d)
                snap.restore()
            snap.pop()
        if self.sel is not None:
            app = self.slots[self.sel][0]
            name = "all apps" if app == MORE else app.get_display_name()
            self._text(snap, name, w * 0.58, h - S * 1.0 + 18, 17, "#ffffff", center=True, glow=True)

    def _text(self, snap, text, x, y, pt, color, center=False, glow=False):
        layout = self.create_pango_layout(text)
        layout.set_font_description(Pango.FontDescription.from_string(f"JetBrains Mono Bold {pt}px"))
        _ink, logical = layout.get_pixel_extents()
        ox = x - logical.width / 2 if center else x
        oy = y - logical.height / 2 if center else y
        if glow:
            for dx, dy, col, a in ((0, 0, "#c5a0ff", 0.55), (1.5, 0, "#f7a1e8", 0.35), (-1.5, 0, "#6fd6f2", 0.35),
                                   (0, 1.5, "#c5a0ff", 0.35), (0, -1.5, "#c5a0ff", 0.35)):
                snap.save()
                snap.translate(Graphene.Point().init(ox + dx, oy + dy))
                snap.append_layout(layout, rgba(col, a))
                snap.restore()
        snap.save()
        snap.translate(Graphene.Point().init(ox, oy))
        snap.append_layout(layout, rgba(color))
        snap.restore()


class CornerHint(Gtk.Widget):
    """A barely-there quarter ring so the corner is findable (no pycairo on this phone)."""

    def do_measure(self, orientation, for_size):
        return CORNER, CORNER, -1, -1

    def do_snapshot(self, snap):
        h = self.get_height()
        pb = Gsk.PathBuilder.new()
        pb.add_circle(Graphene.Point().init(0, h), self.get_width() * 0.62)
        snap.push_clip(Graphene.Rect().init(0, 0, self.get_width(), h))
        snap.append_stroke(pb.to_path(), Gsk.Stroke.new(2.0), rgba("#c5a0ff", 0.30))
        snap.pop()


class Launcher(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.notarealinux.Arc", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.usage = Usage()
        self.locked = False
        self.sticky = False
        self.drag_from = None

    def do_activate(self):
        self.hold()
        css = Gtk.CssProvider()
        css.load_from_data(b"window.arc { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, 800)
        self._build_corner()
        self._build_arc()
        self._watch_apps()
        self._watch_lock()
        # kill -USR1 <pid>: open the arc (sticky) for testing
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 10, lambda: (self.show_arc(True), True)[1])

    # --- windows -----------------------------------------------------------------------
    def _layer(self, win, ns, size):
        win.add_css_class("arc")
        LayerShell.init_for_window(win)
        LayerShell.set_namespace(win, ns)
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)
        LayerShell.set_exclusive_zone(win, -1)
        LayerShell.set_anchor(win, LayerShell.Edge.BOTTOM, True)
        LayerShell.set_anchor(win, LayerShell.Edge.LEFT, True)
        win.set_default_size(*size)

    def _build_corner(self):
        self.corner = Gtk.ApplicationWindow(application=self)
        self._layer(self.corner, "arc-corner", (CORNER, CORNER))
        hint = CornerHint()
        self.corner.set_child(hint)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._corner_begin)
        drag.connect("drag-update", self._corner_update)
        drag.connect("drag-end", self._corner_end)
        hint.add_controller(drag)
        self.corner.present()

    def _build_arc(self):
        self.arcwin = Gtk.ApplicationWindow(application=self)
        self._layer(self.arcwin, "arc", (540, 540))
        self.view = ArcView(self)
        self.view.set_hexpand(True)
        self.view.set_vexpand(True)
        self.arcwin.set_child(self.view)
        drag = Gtk.GestureDrag()                     # sticky mode: tap or slide on the arc
        drag.connect("drag-begin", self._arc_begin)
        drag.connect("drag-update", self._arc_update)
        drag.connect("drag-end", self._arc_end)
        self.view.add_controller(drag)

    def _arc_size(self):
        mons = Gdk.Display.get_default().get_monitors()
        if mons.get_n_items():
            g = mons.get_item(0).get_geometry()
            return min(g.width, g.height)
        return 540

    def show_arc(self, sticky):
        if self.locked:
            return
        self.sticky = sticky
        S = self._arc_size()
        self.arcwin.set_default_size(S, S)
        self.view.set_apps(self.usage.ranked(sum(len(r[2]) for r in RINGS) - 1))
        self.view.open()
        self.arcwin.present()

    def hide_arc(self):
        self.sticky = False
        self.arcwin.set_visible(False)

    # --- corner drag (the main gesture) -----------------------------------------------------
    def _corner_begin(self, _g, x, y):
        self.drag_from = (x, y)
        self.arc_shown_by_drag = False

    def _rel(self, x, y):
        """corner-surface coords -> point relative to the screen corner, y up."""
        return x, CORNER - y

    def _corner_update(self, _g, dx, dy):
        x0, y0 = self.drag_from
        px, py = self._rel(x0 + dx, y0 + dy)
        if not self.arc_shown_by_drag:
            if math.hypot(dx, dy) < 14:
                return
            self.arc_shown_by_drag = True
            self.show_arc(sticky=False)
        self.view.select(self.view.pick(px, py))

    def _corner_end(self, _g, dx, dy):
        if not self.arc_shown_by_drag:
            if self.arcwin.get_visible():
                self.hide_arc()
            else:
                self.show_arc(sticky=True)           # a tap: leave it up
            return
        i = self.view.sel
        self.hide_arc()
        if i is not None:
            self.launch(self.view.slots[i][0])

    # --- sticky mode -----------------------------------------------------------------------------
    def _arc_begin(self, _g, x, y):
        self.arc_from = (x, y)
        self.view.select(self.view.pick(x, self.view.get_height() - y))

    def _arc_update(self, _g, dx, dy):
        x, y = self.arc_from
        self.view.select(self.view.pick(x + dx, self.view.get_height() - (y + dy)))

    def _arc_end(self, _g, _dx, _dy):
        i = self.view.sel
        self.hide_arc()
        if i is not None:
            self.launch(self.view.slots[i][0])

    # --- actions ----------------------------------------------------------------------------------
    def launch(self, app):
        if app == MORE:
            subprocess.Popen(["gdbus", "call", "--session", "-d", "org.notarealinux.PhantomShell",
                              "-o", "/org/notarealinux/PhantomShell",
                              "-m", "org.notarealinux.PhantomShell.ShowApps"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        log("launch", app.get_id())
        self.usage.bump(app.get_id())
        try:
            app.launch([], Gdk.Display.get_default().get_app_launch_context())
        except GLib.Error as e:
            log("launch failed:", e.message)

    def _watch_apps(self):
        if not os.path.exists(TOPLEVEL_WATCH):
            return
        proc = Gio.Subprocess.new([TOPLEVEL_WATCH], Gio.SubprocessFlags.STDOUT_PIPE)
        stream = Gio.DataInputStream.new(proc.get_stdout_pipe())
        self.last_active = None

        def on_line(src, res):
            line, _ = src.read_line_finish_utf8(res)
            if line is None:
                return
            line = line.strip()
            if line.startswith("active "):
                app_id = line[7:]
                if app_id != self.last_active:
                    did = self.usage.desktop_for(app_id)
                    if did:
                        self.usage.bump(did)
                self.last_active = app_id
            else:
                self.last_active = None
            src.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)
        self.toplevel_proc = proc

    def _watch_lock(self):
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)

        def apply(locked):
            self.locked = locked
            if locked:
                self.hide_arc()
                self.corner.set_visible(False)
            else:
                self.corner.present()

        bus.signal_subscribe("org.gnome.ScreenSaver", "org.gnome.ScreenSaver", "ActiveChanged",
                             "/org/gnome/ScreenSaver", None, Gio.DBusSignalFlags.NONE,
                             lambda *a: apply(a[5].unpack()[0]))
        try:
            r = bus.call_sync("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver",
                              "GetActive", None, None, Gio.DBusCallFlags.NONE, 2000, None)
            apply(r.unpack()[0])
        except GLib.Error:
            pass


if __name__ == "__main__":
    Launcher().run([])
