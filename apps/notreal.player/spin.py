#!/usr/bin/env python3
"""notarealPlayer: a Navidrome client for the phone, vapor themed, vinyl now-playing."""
import math
import os
import subprocess
import sys
import threading
import gi
# 10-12 hard freeze right after an animated cover started on the iris HW decoder:
# keep this app's (tiny) cover videos on software decoding.
os.environ["GST_PLUGIN_FEATURE_RANK"] = ",".join(
    f"v4l2{c}dec:NONE" for c in ("vp8", "vp9", "h264", "h265", "mpeg2"))
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango
import notareal_theme  # noqa: E402  ~/theme/theme.css

# themes other than vapor/micro (nasa, alien, ...): the vapor drawing with its hexes swapped for palette names
TINT = {"#0b0e1c": "fg-bright", "#1f2647": "bg-deep", "#1c2340": "bg", "#c5a0ff": "accent",
        "#4a5280": "accent-light", "#dfe3ff": "accent", "#7ef0d2": "orange", "#161c36": "fg-bright",
        "#2a3157": "fg"}


def tc(c):
    """Vapor hex -> the active theme's color when the theme is a tinted one, else unchanged."""
    if notareal_theme.value("wallpaper") in ("vapor", "vaporlight", "micro", ""):
        return c
    return notareal_theme.hex(TINT.get(c, "fg"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api import ApiError, Offline  # noqa: E402
from store import QUALITY, Settings, Store  # noqa: E402
from player import EQ_FREQS, EQ_PRESETS, Player, usb_cards  # noqa: E402
from mpris import Mpris  # noqa: E402
from cassette import Cassette  # noqa: E402

APP_ID = "dev.notareal.Player"
APP_NAME = "notreal.player"
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_GRID_ANIM = 8  # each animated tile is a full GStreamer pipeline (~30 threads, ~40 fds)

# every video pipeline costs dozens of fds; the default soft limit (1024) crashed the app on launch
try:
    import resource
    _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (_hard, _hard))
except (ValueError, OSError):
    pass


class ScreenWatch:
    """Is the display on? (Phosh's org.gnome.Mutter.DisplayConfig PowerSaveMode, 0 = on.)
    Cover videos pause while it's off so music in the pocket doesn't decode h264 for nobody."""

    def __init__(self):
        self.on = True
        self.cbs = []
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
            self.bus.signal_subscribe(None, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                      "/org/gnome/Mutter/DisplayConfig", None, Gio.DBusSignalFlags.NONE,
                                      lambda *_: self._poll())
        except GLib.Error:
            self.bus = None
        self._poll()
        GLib.timeout_add_seconds(3, self._poll)  # in case the property change isn't signalled

    def _poll(self):
        if self.bus is None:
            return False
        on = True
        try:
            r = self.bus.call_sync("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                                   "org.freedesktop.DBus.Properties", "Get",
                                   GLib.Variant("(ss)", ("org.gnome.Mutter.DisplayConfig", "PowerSaveMode")),
                                   None, Gio.DBusCallFlags.NONE, 500, None).unpack()
            on = r[0] == 0
        except GLib.Error:
            pass
        if on != self.on:
            self.on = on
            for cb in list(self.cbs):
                cb()
        return True

    def connect(self, cb):
        self.cbs.append(cb)


_screen = None


def screen():
    global _screen
    if _screen is None:
        _screen = ScreenWatch()
    return _screen


def visible_only(w):
    """Run a widget's animation tick only while it can be seen: mapped, the window active and the
    screen on. Paused mid-spin it keeps its angle and picks up again when it's back on screen."""
    orig = w._on_tick

    def tick(widget, clock):
        root = w.get_root()
        if not (w.get_mapped() and root is not None and root.is_active() and screen().on):
            w.tick = w.last = None
            return False
        return orig(widget, clock)

    def resume(*_):
        if w.tick is None and w.get_mapped():
            w._ensure_tick()   # ends itself again when there's nothing to animate
    w._on_tick = tick
    w.connect("map", resume)
    w.connect("realize", lambda *_: w.get_root().connect("notify::is-active", resume))
    screen().connect(resume)


def rgba(spec, alpha=None):
    c = Gdk.RGBA()
    c.parse(spec)
    if alpha is not None:
        c.alpha = alpha
    return c


def bg(fn, done=None, fail=None):
    """Run fn() in a thread, deliver the result (or exception) on the main loop."""
    def run():
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            if fail:
                GLib.idle_add(lambda: (fail(e), False)[1])
            else:
                print("bg error:", e)
            return
        if done:
            GLib.idle_add(lambda: (done(res), False)[1])
    threading.Thread(target=run, daemon=True).start()


def fmt_time(s):
    s = int(s or 0)
    return f"{s // 60}:{s % 60:02d}"


def label(text="", css=None, xalign=0.0, ellipsize=True, lines=1):
    lb = Gtk.Label(label=text, xalign=xalign)
    if ellipsize:
        lb.set_ellipsize(Pango.EllipsizeMode.END)
    if lines > 1:
        lb.set_wrap(True)
        lb.set_lines(lines)
    for c in (css or "").split():
        lb.add_css_class(c)
    return lb


def icon_button(icon, cb, css="flat", tooltip=None):
    b = Gtk.Button(icon_name=icon)
    for c in css.split():
        b.add_css_class(c)
    if tooltip:
        b.set_tooltip_text(tooltip)
    b.connect("clicked", lambda *_: cb())
    return b


PORTRAIT_FILE = os.path.expanduser("~/.config/rotated-portrait-apps")


def portrait_locked():
    try:
        with open(PORTRAIT_FILE) as f:
            return APP_ID in {line.strip() for line in f}
    except OSError:
        return False


def set_portrait_locked(on):
    """The rotated daemon keeps the screen upright while an app listed here is active."""
    try:
        with open(PORTRAIT_FILE) as f:
            apps = [line.strip() for line in f if line.strip()]
    except OSError:
        apps = []
    apps = [a for a in apps if a != APP_ID] + ([APP_ID] if on else [])
    with open(PORTRAIT_FILE, "w") as f:
        f.write("".join(a + "\n" for a in apps))


class Item(GObject.Object):
    def __init__(self, data):
        super().__init__()
        self.data = data


# --- covers ---------------------------------------------------------------------
_tex_cache = {}


def texture(path):
    if not path:
        return None
    tex = _tex_cache.get(path)
    if tex is None:
        try:
            tex = Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            return None
        if len(_tex_cache) > 300:
            _tex_cache.pop(next(iter(_tex_cache)))
        _tex_cache[path] = tex
    return tex


def load_cover(store, pic, cover_id, size=300):
    """Load a cover into a Gtk.Picture (safe with recycled list widgets)."""
    pic._want = cover_id
    pic.set_paintable(None)

    def got(path):
        if getattr(pic, "_want", None) == cover_id:
            pic.set_paintable(texture(path))
    store.cover(cover_id, size, got)


def cover_picture(size, css="cover"):
    pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
    pic.set_size_request(size, size)
    pic.set_overflow(Gtk.Overflow.HIDDEN)
    pic.add_css_class(css)
    return pic


# --- the vinyl --------------------------------------------------------------------
def _rrect(x, y, w, h, rad):
    return Gsk.RoundedRect().init_from_rect(Graphene.Rect().init(x, y, w, h), rad)


def _pt(x, y):
    return Graphene.Point().init(x, y)


class Vinyl(Gtk.Widget):
    """A circular cover that spins like a record, with spin-up/down inertia and an optional tonearm
    that drops onto the record when playing and tracks through the song."""

    # tonearm geometry, in units of the record radius, relative to the record centre
    PIVOT = (0.93, -1.02)
    ARM = 1.42
    REST, START, END = 1.12, 0.90, 0.52     # needle distance from centre: resting / track start / track end

    def __init__(self, natural=360, grooves=True, arm=False):
        super().__init__()
        self.natural = natural
        self.grooves = grooves
        self.arm = arm
        self.paintable = None
        self.handler = None
        self.angle = 0.0
        self.speed = 0.0          # deg/s
        self.target = 0.0
        self.rpm = 8.0
        self.playing = False
        self.progress = 0.0
        self.arm_angle = None     # degrees, eased toward arm_target()
        self.tick = None
        self.last = None
        self.set_overflow(Gtk.Overflow.HIDDEN)
        notareal_theme.on_change(self.queue_draw)   # `nrtheme <name>`: vapor record / micro drawing
        visible_only(self)

    def set_paintable(self, p):
        if self.paintable is not None and self.handler:
            self.paintable.disconnect(self.handler)
        self.paintable = p
        self.handler = p.connect("invalidate-contents", lambda *_: self.queue_draw()) if p else None
        self.queue_draw()

    def set_spinning(self, on):
        self.playing = on
        self.target = self.rpm * 6.0 if on else 0.0
        self._ensure_tick()

    def set_progress(self, frac):
        self.progress = max(0.0, min(1.0, frac))
        if self.arm:
            self._ensure_tick()

    def _ensure_tick(self):
        if self.tick is None:
            self.last = None
            self.tick = self.add_tick_callback(self._on_tick)

    def _geometry(self):
        w, h = self.get_width(), self.get_height()
        if not self.arm:
            r = (min(w, h) - 4) / 2
            return r, w / 2, h / 2
        # the record plus the arm's pivot sticking out top-right
        span_x, span_y = 1.0 + self.PIVOT[0] + 0.16, 1.0 - self.PIVOT[1] + 0.16
        r = min((w - 8) / span_x, (h - 8) / span_y)
        cx = (w - span_x * r) / 2 + r
        cy = (h - span_y * r) / 2 + (span_y - 1.0) * r
        return r, cx, cy

    def arm_target(self):
        """Arm angle (deg, screen coords) that puts the needle at the right distance from the centre."""
        rho = (self.START + (self.END - self.START) * self.progress) if self.playing else self.REST
        px, py = self.PIVOT
        dist = math.hypot(px, py)
        phi = math.degrees(math.atan2(-py, -px))          # direction pivot -> centre
        c = (dist ** 2 + self.ARM ** 2 - rho ** 2) / (2 * dist * self.ARM)
        return phi - math.degrees(math.acos(max(-1.0, min(1.0, c))))

    def _on_tick(self, _w, clock):
        t = clock.get_frame_time() / 1e6
        dt = 0 if self.last is None else min(0.1, t - self.last)
        self.last = t
        self.speed += (self.target - self.speed) * min(1.0, dt * 2.0)   # turntable inertia
        self.angle = (self.angle + self.speed * dt) % 360
        arm_moving = False
        if self.arm:
            goal = self.arm_target()
            if self.arm_angle is None:
                self.arm_angle = goal
            diff = goal - self.arm_angle
            step = 35.0 * dt                    # deg/s, an unhurried cue lever
            self.arm_angle = goal if abs(diff) <= step else self.arm_angle + math.copysign(step, diff)
            arm_moving = abs(goal - self.arm_angle) > 0.01
        self.queue_draw()
        if self.target == 0 and self.speed < 0.3 and not arm_moving:
            self.speed = 0
            self.tick = None
            return False
        return True

    def do_measure(self, orientation, for_size):
        return min(64, self.natural), self.natural, -1, -1

    def do_snapshot(self, snap):
        r, cx, cy = self._geometry()
        if r <= 4:
            return
        d = 2 * r
        disc = Graphene.Rect().init(cx - r, cy - r, d, d)
        micro = notareal_theme.value("wallpaper") == "micro"   # flat ink-on-paper drawing, no glows
        ink, paper = notareal_theme.hex("fg-bright"), notareal_theme.hex("bg")
        if self.grooves and not micro:
            snap.append_outset_shadow(_rrect(cx - r, cy - r, d, d, r), rgba("#000000", 0.5), 0, r * 0.04, 0,
                                      r * 0.12)
        snap.push_rounded_clip(_rrect(cx - r, cy - r, d, d, r))
        snap.append_color(rgba(ink if micro else tc("#0b0e1c")), disc)
        inner = r * (0.965 if self.grooves else 1.0)
        snap.save()
        snap.translate(_pt(cx, cy))
        snap.rotate(self.angle)
        snap.push_rounded_clip(_rrect(-inner, -inner, 2 * inner, 2 * inner, inner))
        if self.paintable is not None:
            ar = self.paintable.get_intrinsic_aspect_ratio() or 1.0
            pw, ph = (2 * inner * ar, 2 * inner) if ar >= 1 else (2 * inner, 2 * inner / ar)
            snap.translate(_pt(-pw / 2, -ph / 2))
            self.paintable.snapshot(snap, pw, ph)
        else:
            snap.append_color(rgba(notareal_theme.hex("bg-deep") if micro else tc("#1f2647")),
                              Graphene.Rect().init(-inner, -inner, 2 * inner, 2 * inner))
        snap.pop()
        snap.restore()
        if self.grooves:
            for k in range(6):
                pb = Gsk.PathBuilder.new()
                pb.add_circle(_pt(cx, cy), r * (0.30 + k * 0.115))
                snap.append_stroke(pb.to_path(), Gsk.Stroke.new(1.0), rgba("#000000", 0.10))
            if not micro:
                snap.append_conic_gradient(disc, _pt(cx, cy), 30, self._glint())
            pb = Gsk.PathBuilder.new()
            pb.add_circle(_pt(cx, cy), r - 0.5)
            snap.append_stroke(pb.to_path(), Gsk.Stroke.new(1.5), rgba(ink, 0.9) if micro else rgba(tc("#c5a0ff"), 0.55))
            hole = r * 0.045
            snap.push_rounded_clip(_rrect(cx - hole * 2.4, cy - hole * 2.4, hole * 4.8, hole * 4.8, hole * 2.4))
            snap.append_color(rgba("#0b0e1c", 0.55), disc)
            snap.pop()
            snap.push_rounded_clip(_rrect(cx - hole, cy - hole, hole * 2, hole * 2, hole))
            snap.append_color(rgba(paper if micro else tc("#1c2340")), disc)
            snap.pop()
        snap.pop()
        if self.grooves and micro:
            self._micro_marks(snap, r, cx, cy, ink)
        if self.arm:
            self._draw_arm(snap, r, cx, cy)

    def _draw_arm(self, snap, r, cx, cy):
        if self.arm_angle is None:
            self.arm_angle = self.arm_target()
        px, py = cx + self.PIVOT[0] * r, cy + self.PIVOT[1] * r
        L = self.ARM * r
        # lifted when not playing or still travelling: bigger, softer shadow
        lift = 1.0 if not self.playing or abs(self.arm_target() - self.arm_angle) > 0.5 else 0.35
        micro = notareal_theme.value("wallpaper") == "micro"
        for shadow in ((False,) if micro else (True, False)):
            snap.save()
            if shadow:
                snap.translate(_pt(r * 0.03 * lift, r * 0.05 * lift))
            snap.translate(_pt(px, py))
            snap.rotate(self.arm_angle)
            self._arm_shapes(snap, r, L, shadow, micro)
            snap.restore()
        # pivot base on top
        if micro:
            self._micro_pivot(snap, r, px, py)
            return
        for rad, col in ((0.15, "#161c36"), (0.13, "#2a3157"), (0.055, "#dfe3ff")):
            pb = Gsk.PathBuilder.new()
            pb.add_circle(_pt(px, py), r * rad)
            snap.append_fill(pb.to_path(), Gsk.FillRule.WINDING, rgba(tc(col)))
        pb = Gsk.PathBuilder.new()
        pb.add_circle(_pt(px, py), r * 0.13)
        snap.append_stroke(pb.to_path(), Gsk.Stroke.new(2.0), rgba(tc("#c5a0ff"), 0.9))

    # --- micro theme: the turntable as a technical drawing (ink hairlines on paper) ---
    @staticmethod
    def _hair(snap, pts, col, width=1.0, close=False):
        pb = Gsk.PathBuilder.new()
        for i, (x, y) in enumerate(pts):
            (pb.move_to if i == 0 else pb.line_to)(x, y)
        if close:
            pb.close()
        snap.append_stroke(pb.to_path(), Gsk.Stroke.new(width), col)

    def _micro_marks(self, snap, r, cx, cy, ink):
        """Centre crosshair through the spindle and a fixed degree scale just inside the rim."""
        hole = r * 0.045
        for a, b in (((cx - hole * 5, cy), (cx - hole * 1.6, cy)), ((cx + hole * 1.6, cy), (cx + hole * 5, cy)),
                     ((cx, cy - hole * 5), (cx, cy - hole * 1.6)), ((cx, cy + hole * 1.6), (cx, cy + hole * 5))):
            self._hair(snap, (a, b), rgba(ink, 0.9))
        for k in range(72):
            a = k * math.tau / 72
            r1 = r * (0.93 if k % 6 == 0 else 0.955)
            self._hair(snap, ((cx + r1 * math.cos(a), cy + r1 * math.sin(a)),
                              (cx + r * 0.965 * math.cos(a), cy + r * 0.965 * math.sin(a))),
                       rgba(notareal_theme.hex("bg"), 0.75 if k % 6 == 0 else 0.45))

    def _micro_pivot(self, snap, r, px, py):
        ink, paper = notareal_theme.hex("fg-bright"), notareal_theme.hex("bg")
        for rad, fill in ((0.15, paper), (0.055, ink)):
            pb = Gsk.PathBuilder.new()
            pb.add_circle(_pt(px, py), r * rad)
            path = pb.to_path()
            snap.append_fill(path, Gsk.FillRule.WINDING, rgba(fill))
            snap.append_stroke(path, Gsk.Stroke.new(1.5), rgba(ink))
        pb = Gsk.PathBuilder.new()
        pb.add_circle(_pt(px, py), r * 0.11)
        snap.append_stroke(pb.to_path(), Gsk.Stroke.new(1.0), rgba(ink, 0.5))
        e = r * 0.22   # centre mark through the pivot, like a drawing's
        self._hair(snap, ((px, py - e), (px, py - r * 0.16)), rgba(ink, 0.7))
        self._hair(snap, ((px - e, py), (px - r * 0.16, py)), rgba(ink, 0.7))
        # live readout of where the needle is, left of the pivot above the record
        layout = self.create_pango_layout(f"ARM {self.arm_angle:+06.1f}°  {self.progress * 100:05.1f}%"
                                          if self.playing else "ARM REST")
        fd = Pango.FontDescription.from_string("JetBrains Mono")
        fd.set_absolute_size(max(7.0, r * 0.045) * Pango.SCALE)
        layout.set_font_description(fd)
        _ink, lg = layout.get_pixel_extents()
        snap.save()
        snap.translate(_pt(px - r * 0.25 - lg.width, py - lg.height - r * 0.02))
        snap.append_layout(layout, rgba(notareal_theme.hex("accent"), 0.9))
        snap.restore()

    @staticmethod
    def _arm_shapes(snap, r, L, shadow, micro=False):
        # micro: ink parts, paper cartridge, signal-orange stylus (vapor colors mapped by name)
        mic = {"#4a5280": notareal_theme.hex("accent"), "#dfe3ff": notareal_theme.hex("fg-bright"),
               "#c5a0ff": notareal_theme.hex("fg-bright"), "#1c2340": notareal_theme.hex("bg"),
               "#7ef0d2": notareal_theme.hex("orange")}

        def col(c, a=1.0):
            return rgba("#000000", 0.35) if shadow else rgba(mic[c] if micro else tc(c), a)

        def fill_rr(x, y, w, h, rad, c):
            pb = Gsk.PathBuilder.new()
            pb.add_rounded_rect(_rrect(x, y, w, h, rad))
            snap.append_fill(pb.to_path(), Gsk.FillRule.WINDING, c)
        # counterweight behind the pivot
        fill_rr(-0.30 * r, -0.07 * r, 0.20 * r, 0.14 * r, 0.03 * r, col("#4a5280"))
        # tube
        pb = Gsk.PathBuilder.new()
        pb.move_to(-0.12 * r, 0)
        pb.line_to(L - 0.16 * r, 0)
        st = Gsk.Stroke.new(max(2.0, 0.034 * r))
        st.set_line_cap(Gsk.LineCap.ROUND)
        snap.append_stroke(pb.to_path(), st, col("#dfe3ff"))
        # headshell + cartridge + finger lift
        fill_rr(L - 0.19 * r, -0.05 * r, 0.22 * r, 0.10 * r, 0.02 * r, col("#c5a0ff"))
        fill_rr(L - 0.12 * r, -0.032 * r, 0.12 * r, 0.064 * r, 0.012 * r, col("#1c2340"))
        pb = Gsk.PathBuilder.new()
        pb.move_to(L - 0.15 * r, -0.05 * r)
        pb.line_to(L - 0.10 * r, -0.12 * r)
        st = Gsk.Stroke.new(max(1.5, 0.014 * r))
        st.set_line_cap(Gsk.LineCap.ROUND)
        snap.append_stroke(pb.to_path(), st, col("#dfe3ff"))
        if not shadow:
            pb = Gsk.PathBuilder.new()
            pb.add_circle(_pt(L, 0), max(1.5, 0.014 * r))
            snap.append_fill(pb.to_path(), Gsk.FillRule.WINDING, col("#7ef0d2"))

    _stops = None

    def _glint(self):
        if Vinyl._stops is None:
            def stop(o, a):
                s = Gsk.ColorStop()
                s.offset = o
                s.color = rgba("#ffffff", a)
                return s
            Vinyl._stops = [stop(0.0, 0.0), stop(0.10, 0.10), stop(0.20, 0.0), stop(0.5, 0.0),
                            stop(0.60, 0.08), stop(0.70, 0.0), stop(1.0, 0.0)]
        return Vinyl._stops


# --- track rows -------------------------------------------------------------------
class TrackRow(Gtk.ListBoxRow):
    def __init__(self, win, track, number=None, show_cover=False):
        super().__init__()
        self.track = track
        self.win = win
        box = Gtk.Box(spacing=10, margin_start=10, margin_end=6, margin_top=6, margin_bottom=6)
        if show_cover:
            pic = cover_picture(42, "cover-small")
            load_cover(win.store, pic, track.get("coverArt"), 100)
            box.append(pic)
        elif number is not None:
            n = label(str(number), "dim num", xalign=1.0)
            n.set_width_chars(2)
            box.append(n)
        txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.title = label(track.get("title", "?"), "row-title")
        txt.append(self.title)
        txt.append(label(track.get("artist", ""), "row-sub"))
        box.append(txt)
        self.fav = Gtk.Image(icon_name="heart-filled-symbolic", css_classes=["fav-dot"])
        box.append(self.fav)
        self.local = Gtk.Image(icon_name="folder-download-symbolic", css_classes=["local-dot"])
        box.append(self.local)
        box.append(label(fmt_time(track.get("duration")), "row-sub", xalign=1.0, ellipsize=False))
        more = Gtk.MenuButton(icon_name="view-more-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER)
        more.set_popover(win.track_menu(track))
        box.append(more)
        self.set_child(box)
        self.refresh()

    def refresh(self):
        self.local.set_visible(self.win.store.is_local(self.track["id"]))
        self.fav.set_visible(self.win.store.is_starred(self.track))
        cur = self.win.player.current
        if cur and cur["id"] == self.track["id"]:
            self.title.add_css_class("playing")
        else:
            self.title.remove_css_class("playing")


class TrackList(Gtk.ListBox):
    def __init__(self, win, tracks, numbered=True, show_cover=False):
        super().__init__(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list-flat"])
        self.win = win
        self.tracks = tracks
        for i, t in enumerate(tracks):
            num = t.get("track") if numbered and t.get("track") else (i + 1 if numbered else None)
            self.append(TrackRow(win, t, num, show_cover))
        self.connect("row-activated", lambda _lb, row: win.player.play_tracks(
            self.tracks, start=row.get_index(), shuffle=False))
        self._h = [win.store_events("downloaded", self.refresh), win.store_events("starred", self.refresh),
                   win.player.connect("track-changed", lambda *_: self.refresh())]

    def refresh(self, *_):
        row = self.get_first_child()
        while row:
            if isinstance(row, TrackRow):
                row.refresh()
            row = row.get_next_sibling()


# --- pages ---------------------------------------------------------------------------
class CollectionPage(Adw.NavigationPage):
    """Album or playlist: big cover, play/shuffle/offline buttons, track list."""

    def __init__(self, win, kind, item):
        super().__init__(title=item.get("name", ""))
        self.win, self.kind, self.item = win, kind, item
        self.tracks = []
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        tv.set_content(self.scroller)
        self.set_child(tv)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=8, margin_bottom=24)
        clamp = Adw.Clamp(maximum_size=600, child=self.box)
        self.scroller.set_child(clamp)

        pic = cover_picture(230, "cover-big")
        pic.set_halign(Gtk.Align.CENTER)
        self.media = None
        if kind == "album":
            def anim(path):
                if path:
                    self.media = Gtk.MediaFile.new_for_filename(path)
                    self.media.set_loop(True)
                    self.media.set_muted(True)
                    pic.set_paintable(self.media)
                    self._media_state()
                else:
                    load_cover(win.store, pic, item.get("coverArt"), 600)
            win.store.anim_lookup(item["id"], anim)
            self._page_shown = True
            self.connect("hidden", lambda *_: (setattr(self, "_page_shown", False), self._media_state()))
            self.connect("shown", lambda *_: (setattr(self, "_page_shown", True), self._media_state()))
            screen().connect(self._media_state)
        else:
            load_cover(win.store, pic, item.get("coverArt"), 600)
        self.box.append(pic)
        self.box.append(label(item.get("name", ""), "page-title", xalign=0.5, lines=2))
        sub = item.get("artist") or item.get("owner") or ""
        if item.get("year"):
            sub += f" · {item['year']}"
        self.subtitle = label(sub, "page-sub", xalign=0.5)
        self.box.append(self.subtitle)

        actions = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        play = Gtk.Button(css_classes=["pill", "suggested-action"])
        play.set_child(Adw.ButtonContent(icon_name="media-playback-start-symbolic", label="Play"))
        play.connect("clicked", lambda *_: win.player.play_tracks(self.tracks, shuffle=False))
        shuf = Gtk.Button(css_classes=["pill"])
        shuf.set_child(Adw.ButtonContent(icon_name="media-playlist-shuffle-symbolic", label="Shuffle"))
        shuf.connect("clicked", lambda *_: win.player.play_tracks(self.tracks, shuffle=True))
        self.pin_btn = Gtk.ToggleButton(icon_name="folder-download-symbolic", css_classes=["circular", "pin"],
                                        tooltip_text="Keep offline",
                                        active=win.store.is_pinned(kind, item["id"]))
        self.pin_btn.connect("toggled", self._pin_toggled)
        more = Gtk.MenuButton(icon_name="view-more-symbolic", css_classes=["circular"])
        more.set_popover(win.collection_menu(lambda: self.tracks))
        for b in (play, shuf, self.pin_btn, more):
            actions.append(b)
        if kind == "album":
            heart = heart_button(lambda on: win.store.set_star("album", item, on), "circular")
            set_heart(heart, win.store.is_starred(item))
            actions.insert_child_after(heart, shuf)
        self.box.append(actions)
        self.pin_label = label("", "pin-status", xalign=0.5)
        self.box.append(self.pin_label)
        self.spinner = Adw.Spinner(height_request=32)
        self.box.append(self.spinner)
        self._evt = win.store_events("downloaded", self.update_pin)
        win.store_events("pins-changed", self.update_pin)
        bg(self._fetch, self._loaded, self._failed)

    def _media_state(self):
        if self.media is not None:
            self.media.play() if getattr(self, "_page_shown", True) and screen().on else self.media.pause()

    def _fetch(self):
        if self.kind == "album":
            return self.win.store.album(self.item["id"]).get("song", [])
        return self.win.store.playlist(self.item["id"]).get("entry", [])

    def _loaded(self, tracks):
        self.spinner.set_visible(False)
        self.tracks = tracks
        secs = sum(t.get("duration") or 0 for t in tracks)
        extra = f"{len(tracks)} tracks · {secs // 3600}h {secs % 3600 // 60}m" if secs >= 3600 \
            else f"{len(tracks)} tracks · {secs // 60} min"
        self.subtitle.set_label((self.subtitle.get_label() + " · " if self.subtitle.get_label() else "") + extra)
        self.box.append(TrackList(self.win, tracks, numbered=self.kind == "album",
                                  show_cover=self.kind == "playlist"))
        if self.kind == "album" and self.win.store.s["animated_covers"]:
            self.win.store.want_anim(self.item["id"])
        self.update_pin()

    def _failed(self, e):
        self.spinner.set_visible(False)
        self.box.append(label("Offline and not downloaded" if isinstance(e, Offline) else str(e),
                              "dim", xalign=0.5))

    def _pin_toggled(self, btn):
        store = self.win.store
        if btn.get_active() and not store.is_pinned(self.kind, self.item["id"]):
            if self.tracks:
                store.pin(self.kind, self.item, self.tracks)
                self.win.toast(f"Downloading {len(self.tracks)} tracks for offline")
        elif not btn.get_active() and store.is_pinned(self.kind, self.item["id"]):
            store.unpin(self.kind, self.item["id"])
            self.win.toast("Removed from offline (tracks stay in the cache until evicted)")
        self.update_pin()

    def update_pin(self, *_):
        store = self.win.store
        if store.is_pinned(self.kind, self.item["id"]):
            done, total = store.pin_progress(self.kind, self.item["id"])
            self.pin_label.set_label("✓ available offline" if done >= total else f"downloading {done}/{total}")
        else:
            n = sum(1 for t in self.tracks if store.is_local(t["id"]))
            self.pin_label.set_label(f"{n}/{len(self.tracks)} cached" if n else "")


class AlbumGrid(Gtk.ScrolledWindow):
    """Infinite-scrolling cover grid fed by a loader(offset) -> list."""

    def __init__(self, win, loader=None, albums=None):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.win = win
        self.loader = loader
        self.model = Gio.ListStore(item_type=Item)
        self.offset = 0
        self.loading = False
        self.done = False
        f = Gtk.SignalListItemFactory()
        f.connect("setup", self._setup)
        f.connect("bind", self._bind)
        f.connect("unbind", self._unbind)
        self.tiles = set()
        self._sync_id = 0
        self.get_vadjustment().connect("value-changed", self._queue_sync)
        self.connect("map", self._queue_sync)
        self.connect("realize", lambda *_: self.get_root().connect("notify::is-active", self._queue_sync))
        screen().connect(self._queue_sync)
        self.get_vadjustment().connect("changed", self._queue_sync)
        self.connect("unmap", lambda *_: [self._stop_media(p) for p in list(self.tiles)])
        grid = Gtk.GridView(model=Gtk.NoSelection(model=self.model), factory=f, max_columns=4, min_columns=2,
                            single_click_activate=True, css_classes=["album-grid"])
        grid.connect("activate", lambda _g, pos: win.open_collection("album", self.model.get_item(pos).data))
        self.set_child(grid)
        self.connect("edge-reached", lambda _s, pos: pos == Gtk.PositionType.BOTTOM and self.more())
        if albums is not None:
            self.model.splice(0, 0, [Item(a) for a in albums])
            self.done = True
        else:
            self.more()

    def reload(self, loader=None):
        if loader:
            self.loader = loader
        self.model.remove_all()
        self.offset, self.done = 0, False
        self.more()

    def more(self):
        if self.loading or self.done or not self.loader:
            return
        self.loading = True
        off = self.offset
        loader = self.loader

        def done(albums):
            self.loading = False
            if loader is not self.loader:
                return
            if not albums:
                self.done = True
                return
            self.model.splice(self.model.get_n_items(), 0, [Item(a) for a in albums])
            self.offset = off + len(albums)
            if len(albums) < 60:
                self.done = True

        def fail(e):
            self.loading = False
            self.win.toast(f"Couldn't load albums: {e}")
        bg(lambda: loader(off), done, fail)

    def _setup(self, _f, li):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["album-card"])
        pic = cover_picture(140)
        frame = Gtk.AspectFrame(ratio=1.0, obey_child=False, child=pic)
        box.append(frame)
        box.append(label("", "card-title"))
        box.append(label("", "card-sub"))
        li.set_child(box)

    def _bind(self, _f, li):
        a = li.get_item().data
        box = li.get_child()
        frame = box.get_first_child()
        title = frame.get_next_sibling()
        sub = title.get_next_sibling()
        title.set_label(a.get("name", "?"))
        sub.set_label(a.get("artist", ""))
        self._load_tile(frame.get_child(), a.get("id"), a.get("coverArt"))


    def _unbind(self, _f, li):
        pic = li.get_child().get_first_child().get_child()
        self._stop_media(pic)
        pic._anim_for = pic._anim_path = None
        pic.set_paintable(None)
        self.tiles.discard(pic)

    # animated covers: only tiles actually on screen get a (software h264) video pipeline
    def _load_tile(self, pic, album_id, cover_id):
        """The animated mp4 when the album has one, else the still (never a still while we ask)."""
        self._stop_media(pic)
        pic._anim_for, pic._anim_path = album_id, None
        pic._want, pic._still_for = cover_id, None
        pic.set_paintable(None)
        self.tiles.add(pic)
        if not self.win.store.s["grid_anim"]:  # off by default: still covers only in grids
            load_cover(self.win.store, pic, cover_id, 300)
            return

        def anim(path):
            if getattr(pic, "_anim_for", None) != album_id:
                return  # recycled for another album meanwhile
            if path:
                pic._anim_path = path
                self._queue_sync()
            else:
                load_cover(self.win.store, pic, cover_id, 300)
        self.win.store.anim_lookup(album_id, anim, thumb=True)

    def _stop_media(self, pic):
        m = getattr(pic, "_media", None)
        if m:
            pic._media = None
            pic.set_paintable(None)
            m.clear()

    def _queue_sync(self, *_):
        if not self._sync_id:
            self._sync_id = GLib.timeout_add(150, self._sync)

    def _sync(self):
        self._sync_id = 0
        h = self.get_height()
        root = self.get_root()
        # not while in the background or with the screen off
        on = self.get_mapped() and root is not None and root.is_active() and screen().on
        want = []
        for pic in list(self.tiles):
            path = getattr(pic, "_anim_path", None)
            if on and path and pic.get_mapped():
                ok, r = pic.compute_bounds(self)
                if ok and r.get_y() + r.get_height() > 0 and r.get_y() < h:
                    want.append((r.get_y(), r.get_x(), pic))
        want.sort(key=lambda w: (w[0], w[1]))
        keep = {id(w[2]) for w in want[:MAX_GRID_ANIM]}
        for pic in list(self.tiles):
            vis = id(pic) in keep
            m = getattr(pic, "_media", None)
            if not vis and getattr(pic, "_anim_path", None) and not m and getattr(pic, "_still_for", None) != pic._anim_for \
                    and any(w[2] is pic for w in want):
                pic._still_for = pic._anim_for  # over the cap: the still cover (the lookup is already done)
                load_cover(self.win.store, pic, pic._want, 300)
            if vis and not m:
                m = Gtk.MediaFile.new_for_filename(path)
                m.set_loop(True)
                m.set_muted(True)
                pic._media = m
                pic._still_for = None
                pic.set_paintable(m)
                m.play()
            elif not vis and m:
                self._stop_media(pic)
        return False


class AlbumsTab(Gtk.Box):
    SORTS = [("newest", "Recently added"), ("recent", "Recently played"), ("frequent", "Most played"),
             ("alphabeticalByName", "A–Z"), ("alphabeticalByArtist", "By artist"), ("random", "Random"),
             ("starred", "Favourites"), ("offline", "Downloaded")]

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        bar = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=6, margin_bottom=2)
        self.drop = Gtk.DropDown.new_from_strings([n for _, n in self.SORTS])
        keys = [k for k, _ in self.SORTS]
        cur = win.store.s["album_sort"]
        self.drop.set_selected(keys.index(cur) if cur in keys else 0)
        self.drop.add_css_class("flat")
        bar.append(self.drop)
        self.append(bar)
        self.grid = AlbumGrid(win, self._loader())
        ov = Gtk.Overlay(vexpand=True)
        ov.set_child(self.grid)
        fab = Gtk.Button(icon_name="media-playlist-shuffle-symbolic", tooltip_text="Shuffle all songs",
                         halign=Gtk.Align.START, valign=Gtk.Align.END, margin_start=16, margin_bottom=16,
                         css_classes=["shuffle-fab"])
        fab.connect("clicked", lambda *_: win.play_pool("all"))
        ov.add_overlay(fab)
        self.append(ov)
        self.drop.connect("notify::selected", self._changed)

    def _loader(self):
        kind = self.SORTS[self.drop.get_selected()][0]
        return lambda off: self.win.store.album_list(kind, off)

    def _changed(self, *_):
        self.win.store.s.set("album_sort", self.SORTS[self.drop.get_selected()][0])
        self.grid.reload(self._loader())


class ListTab(Gtk.ScrolledWindow):
    """Generic list of artists or playlists."""

    def __init__(self, win, kind):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.win, self.kind = win, kind
        self.lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list-flat"])
        self.lb.connect("row-activated", self._activated)
        self.set_child(Adw.Clamp(maximum_size=700, child=self.lb))
        self.reload()

    def reload(self):
        fn = self.win.store.artists if self.kind == "artist" else self.win.store.playlists
        bg(fn, self._fill, lambda e: self.win.toast(str(e)))

    def _fill(self, items):
        self.lb.remove_all()
        if self.kind == "playlist":
            self.lb.append(self._smart_row("unheard", "✦ Unheard", "songs you've never played · shuffled"))
        for it in items:
            row = Gtk.ListBoxRow()
            row.item = it
            box = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
            pic = cover_picture(52, "cover-round" if self.kind == "artist" else "cover-small")
            load_cover(self.win.store, pic, it.get("coverArt"), 150)
            box.append(pic)
            txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
            txt.append(label(it.get("name", "?"), "row-title"))
            if self.kind == "artist":
                sub = f"{it.get('albumCount', 0)} albums"
            else:
                sub = f"{it.get('songCount', 0)} tracks · {it.get('duration', 0) // 60} min"
            txt.append(label(sub, "row-sub"))
            box.append(txt)
            if self.kind == "playlist" and self.win.store.is_pinned("playlist", it["id"]):
                box.append(Gtk.Image(icon_name="folder-download-symbolic", css_classes=["local-dot"]))
            row.set_child(box)
            self.lb.append(row)

    def _smart_row(self, kind, title, sub):
        row = Gtk.ListBoxRow()
        row.item = {"smart": kind}
        box = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        box.append(Gtk.Label(label="✦", css_classes=["smart-cover"], width_request=52, height_request=52))
        txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        txt.append(label(title, "row-title"))
        subl = label(sub, "row-sub")
        txt.append(subl)
        box.append(txt)
        row.set_child(box)
        if kind == "unheard":
            show = lambda n: subl.set_label(f"{n:,} songs you've never played · shuffled")  # noqa: E731
            n, age = self.win.store.unheard_cached()
            if n is not None:
                show(n)
            if age > 6 * 3600:  # recount (whole-library listing, ~8 s) - not while the app starts up
                GLib.timeout_add_seconds(20, lambda: (bg(self.win.store.unheard_count, show), False)[1])
        return row

    def _activated(self, _lb, row):
        if row.item.get("smart"):
            self.win.play_pool(row.item["smart"])
            return
        if self.kind == "artist":
            self.win.open_artist(row.item)
        else:
            self.win.open_collection("playlist", row.item)


class OfflineTab(Gtk.ScrolledWindow):
    def __init__(self, win):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.win = win
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=12, margin_end=12,
                           margin_top=12, margin_bottom=12)
        self.set_child(Adw.Clamp(maximum_size=700, child=self.box))
        self.pending = False
        win.store_events("pins-changed", self.queue_refresh)
        win.store_events("downloaded", self.queue_refresh)
        self.refresh()

    def queue_refresh(self, *_):
        if not self.pending:
            self.pending = True
            GLib.timeout_add(1500, self.refresh)

    def refresh(self):
        self.pending = False
        store = self.win.store
        while (c := self.box.get_first_child()):
            self.box.remove(c)
        (asize, acount), (psize, pcount) = store.cache_usage()
        gb = 1024 ** 3
        self.box.append(label(f"Offline: {pcount} tracks · {psize / gb:.1f} GB", "row-title"))
        self.box.append(label(f"Auto cache: {acount} tracks · {asize / gb:.1f} / {store.s['cache_limit_gb']} GB",
                              "row-sub"))
        queued = len(store.dl_pending)
        if queued:
            self.box.append(label(f"{queued} downloads queued", "fmt"))
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list-flat"])
        pins = store.pins()
        if not pins:
            self.box.append(label("Nothing pinned yet. Tap the download button on an album or playlist "
                                  "to keep it offline.", "dim", lines=4, ellipsize=False))
        for kind, pid, name, cover in pins:
            row = Gtk.ListBoxRow()
            row.pin = (kind, {"id": pid, "name": name, "coverArt": cover})
            b = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6)
            pic = cover_picture(52, "cover-small")
            load_cover(store, pic, cover, 150)
            b.append(pic)
            t = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
            t.append(label(name, "row-title"))
            done, total = store.pin_progress(kind, pid)
            t.append(label(f"{kind} · {done}/{total}" + ("" if done >= total else " downloading"), "row-sub"))
            b.append(t)
            if total:
                lvl = Gtk.LevelBar(value=done / total, valign=Gtk.Align.CENTER, width_request=50)
                b.append(lvl)
            row.set_child(b)
            lb.append(row)
        lb.connect("row-activated", lambda _l, r: self.win.open_collection(*r.pin))
        self.box.append(lb)
        return False


class FavouritesTab(Gtk.Box):
    """Starred albums / songs / artists (Navidrome favourites)."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.group = Adw.ToggleGroup(margin_start=12, margin_end=12, margin_top=8, margin_bottom=4,
                                     css_classes=["round"])
        for name, text in (("songs", "Songs"), ("albums", "Albums"), ("artists", "Artists")):
            self.group.add(Adw.Toggle(name=name, label=text))
        self.group.set_active_name("songs")
        self.append(self.group)
        self.inner = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.append(self.inner)
        self.data = None
        self.dirty = True
        self.group.connect("notify::active-name", lambda *_: self.show())
        win.store_events("starred", self._starred)
        self.connect("map", lambda *_: self.dirty and self.reload())

    def _starred(self, *_):
        self.dirty = True
        if self.get_mapped():
            GLib.timeout_add(1200, lambda: (self.dirty and self.reload(), False)[1])

    def reload(self):
        self.dirty = False
        bg(self.win.store.starred, self._loaded, lambda e: self.win.toast(str(e)))

    def _loaded(self, data):
        self.data = data
        self.show()

    def show(self):
        if self.data is None:
            return
        while (c := self.inner.get_first_child()):
            self.inner.remove(c)
        kind = self.group.get_active_name()
        items = self.data.get({"songs": "song", "albums": "album", "artists": "artist"}[kind], [])
        if not items:
            self.inner.add_child(Adw.StatusPage(icon_name="heart-outline-symbolic", title="No favourites yet",
                                                description="Tap the heart on a song or album."))
            return
        if kind == "albums":
            self.inner.add_child(AlbumGrid(self.win, albums=items))
            return
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=6, margin_bottom=12)
        sc.set_child(Adw.Clamp(maximum_size=700, child=box))
        if kind == "songs":
            row = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
            play = Gtk.Button(css_classes=["pill", "suggested-action"])
            play.set_child(Adw.ButtonContent(icon_name="media-playback-start-symbolic", label="Play"))
            play.connect("clicked", lambda *_: self.win.player.play_tracks(items, shuffle=False))
            shuf = Gtk.Button(css_classes=["pill"])
            shuf.set_child(Adw.ButtonContent(icon_name="media-playlist-shuffle-symbolic", label="Shuffle"))
            shuf.connect("clicked", lambda *_: self.win.player.play_tracks(items, shuffle=True))
            row.append(play)
            row.append(shuf)
            box.append(row)
            box.append(label(f"{len(items)} songs", "row-sub", xalign=0.5))
            box.append(TrackList(self.win, items, numbered=False, show_cover=True))
        else:
            lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list-flat"])
            for a in items:
                r = Adw.ActionRow(title=GLib.markup_escape_text(a.get("name", "?")),
                                  subtitle=f"{a.get('albumCount', 0)} albums", activatable=True)
                r.connect("activated", lambda _r, a=a: self.win.open_artist(a))
                lb.append(r)
            box.append(lb)
        self.inner.add_child(sc)


class SearchPage(Adw.NavigationPage):
    def __init__(self, win):
        super().__init__(title="Search")
        self.win = win
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.entry = Gtk.SearchEntry(hexpand=True, placeholder_text="Artists, albums, songs")
        hb.set_title_widget(self.entry)
        tv.add_top_bar(hb)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8, margin_bottom=16)
        sc.set_child(Adw.Clamp(maximum_size=700, child=self.box))
        tv.set_content(sc)
        self.set_child(tv)
        self.entry.connect("search-changed", self._changed)
        self.connect("shown", lambda *_: self.entry.grab_focus())
        self.gen = 0

    def _changed(self, *_):
        q = self.entry.get_text().strip()
        self.gen += 1
        gen = self.gen
        if len(q) < 2:
            return
        bg(lambda: self.win.store.api.search(q), lambda r: gen == self.gen and self._show(r),
           lambda e: self.win.toast(str(e)))

    def _show(self, r):
        while (c := self.box.get_first_child()):
            self.box.remove(c)
        if r.get("artist"):
            self.box.append(label("ARTISTS", "section", xalign=0.0))
            lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list-flat"])
            for a in r["artist"]:
                row = Adw.ActionRow(title=GLib.markup_escape_text(a["name"]), activatable=True)
                row.connect("activated", lambda _r, a=a: self.win.open_artist(a))
                lb.append(row)
            self.box.append(lb)
        if r.get("album"):
            self.box.append(label("ALBUMS", "section", xalign=0.0))
            grid = AlbumGrid(self.win, albums=r["album"])
            grid.set_vexpand(False)
            grid.set_propagate_natural_height(True)
            self.box.append(grid)
        if r.get("song"):
            self.box.append(label("SONGS", "section", xalign=0.0))
            self.box.append(TrackList(self.win, r["song"], numbered=False, show_cover=True))


class ArtistPage(Adw.NavigationPage):
    def __init__(self, win, artist):
        super().__init__(title=artist.get("name", ""))
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self.set_child(tv)
        sp = Adw.Spinner()
        tv.set_content(sp)
        bg(lambda: win.store.artist(artist["id"]).get("album", []),
           lambda albums: tv.set_content(AlbumGrid(win, albums=albums)),
           lambda e: win.toast(str(e)))


# --- now playing -----------------------------------------------------------------------
def heart_button(cb, css="flat circular"):
    b = Gtk.ToggleButton(icon_name="heart-outline-symbolic", css_classes=css.split() + ["heart"],
                         valign=Gtk.Align.CENTER)

    def toggled(btn):
        btn.set_icon_name("heart-filled-symbolic" if btn.get_active() else "heart-outline-symbolic")
        if not getattr(btn, "_quiet", False):
            cb(btn.get_active())
    b.connect("toggled", toggled)
    return b


def set_heart(btn, on):
    btn._quiet = True
    btn.set_active(bool(on))
    btn._quiet = False


class NowPlaying(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["np"])
        self.win = win
        self.player = win.player
        self.seeking = None

        top = Gtk.Box(margin_start=8, margin_end=8, margin_top=6)
        top.append(icon_button("pan-down-symbolic", win.close_now_playing, "flat circular", "Close"))
        top.append(label("NOW PLAYING", "np-head", xalign=0.5))
        top.get_last_child().set_hexpand(True)
        top.append(icon_button("view-list-symbolic", win.show_queue, "flat circular", "Queue"))
        self.append(top)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, vexpand=True,
                       margin_start=20, margin_end=20, margin_bottom=20)
        self.append(body)
        self.vinyl = Vinyl(natural=480, arm=True)
        self.vinyl.rpm = win.store.s["rpm"]
        self.tape = Cassette(natural=480)
        visible_only(self.tape)
        self.tape.rpm = win.store.s["rpm"]
        # record or tape (Settings > Look, or long-press to flip); tap = play/pause
        self.deck = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE,
                              transition_duration=250)
        self.deck.add_named(self.vinyl, "vinyl")
        self.deck.add_named(self.tape, "cassette")
        self.deck.set_visible_child_name(win.store.s["np_style"])
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self.player.toggle())
        self.deck.add_controller(click)
        hold = Gtk.GestureLongPress()
        hold.connect("pressed", lambda *_: (click.reset(), self.set_style(
            "cassette" if self.deck.get_visible_child_name() == "vinyl" else "vinyl")))
        self.deck.add_controller(hold)
        body.append(self.deck)

        trow = Gtk.Box(spacing=6, margin_top=6)
        self.heart = heart_button(self._star)
        trow.append(self.heart)
        tcol = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.title = label("Nothing playing", "np-title", xalign=0.5)
        self.artist = label("", "np-artist", xalign=0.5)
        tcol.append(self.title)
        tcol.append(self.artist)
        trow.append(tcol)
        self.hifi = Gtk.ToggleButton(label="HI-FI", css_classes=["chip"], valign=Gtk.Align.CENTER,
                                     tooltip_text="Bit-perfect output to the DAC")
        self.hifi.connect("toggled", self._hifi_toggled)
        rcol = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        mix = Gtk.Button(label="✦ MIX", css_classes=["chip", "mix"], tooltip_text="AudioMuse mix from this song")
        mix.connect("clicked", lambda *_: self.player.current and win.audiomuse_mix(self.player.current))
        rcol.append(self.hifi)
        rcol.append(mix)
        trow.append(rcol)
        body.append(trow)
        # 1-5 star rating, on the left (left-handed); tap the lit top star again to clear
        self.stars = Gtk.Box(spacing=0, halign=Gtk.Align.START, css_classes=["stars"])
        self.star_btns = []
        for n in range(1, 6):
            b = Gtk.Button(label="☆", css_classes=["flat"], tooltip_text=f"{n} star{'s' if n > 1 else ''}")
            b.connect("clicked", lambda _b, n=n: self._rate(n))
            self.stars.append(b)
            self.star_btns.append(b)
        body.append(self.stars)
        self.src_fmt = label("", "fmt", xalign=0.5)
        self.out_fmt = label("", "fmt", xalign=0.5)
        body.append(self.src_fmt)
        body.append(self.out_fmt)

        self.scale = Gtk.Scale(adjustment=Gtk.Adjustment(lower=0, upper=1, step_increment=1),
                               draw_value=False, css_classes=["seek"])
        self.scale.connect("change-value", self._seek_drag)
        body.append(self.scale)
        times = Gtk.Box()
        self.pos_lbl = label("0:00", "time")
        self.dur_lbl = label("0:00", "time", xalign=1.0)
        self.dur_lbl.set_hexpand(True)
        times.append(self.pos_lbl)
        times.append(self.dur_lbl)
        body.append(times)

        ctl = Gtk.Box(spacing=14, halign=Gtk.Align.CENTER, margin_top=4)
        self.shuf = icon_button("media-playlist-shuffle-symbolic", self.player.toggle_shuffle, "flat circular")
        self.prev = icon_button("media-skip-backward-symbolic", self.player.previous, "flat circular big-ctl")
        self.playb = icon_button("media-playback-start-symbolic", self.player.toggle, "play-big circular")
        self.nextb = icon_button("media-skip-forward-symbolic", self.player.next, "flat circular big-ctl")
        self.rep = icon_button("media-playlist-repeat-symbolic", self.player.cycle_repeat, "flat circular")
        for b in (self.shuf, self.prev, self.playb, self.nextb, self.rep):
            ctl.append(b)
        body.append(ctl)

        # swipe down anywhere (except on the seek bar) closes
        swipe = Gtk.GestureSwipe()
        swipe.connect("swipe", lambda _g, vx, vy: vy > 900 and abs(vy) > 2 * abs(vx) and win.close_now_playing())
        self.add_controller(swipe)

        self.player.connect("track-changed", lambda *_: self.on_track())
        self.player.connect("state-changed", lambda *_: self.on_state())
        self.player.connect("position", self.on_position)
        self.player.connect("format", lambda _p, out: self.out_fmt.set_label(out))
        win.store_events("starred", self._starred)
        win.store_events("rated", self._rated)
        self.on_state()

    def set_style(self, style):
        self.win.store.s.set("np_style", style)
        self.deck.set_visible_child_name(style)
        self.tape.set_spinning(self.player.playing)
        self.vinyl.set_spinning(self.player.playing)

    def _star(self, on):
        t = self.player.current
        if t:
            self.win.store.set_star("song", t, on)

    def _rate(self, n):
        t = self.player.current
        if t:
            self.win.store.set_rating(t, 0 if self.win.store.rating(t) == n else n)

    def _rated(self, item_id, n):
        t = self.player.current
        if t and t["id"] == item_id:
            self._show_stars(n)

    def _show_stars(self, n):
        for i, b in enumerate(self.star_btns):
            b.set_label("★" if i < n else "☆")
            (b.add_css_class if i < n else b.remove_css_class)("lit")

    def _starred(self, kind, item_id, on):
        t = self.player.current
        if kind == "song" and t and t["id"] == item_id:
            set_heart(self.heart, on)

    def _hifi_toggled(self, btn):
        s = self.win.store.s
        on = btn.get_active()
        if on == (s["output"] == "direct"):
            return
        if on:
            cards = usb_cards()
            if not cards:
                self.win.toast("No USB DAC connected")
                btn.set_active(False)
                return
            if s["direct_device"] not in [c[0] for c in cards]:
                s["direct_device"] = cards[0][0]
            s.set("output", "direct")
            self.win.toast(f"Bit-perfect → {dict(cards)[s['direct_device']]} (use the DAC/phone volume)")
        else:
            s.set("output", "pulse")
        self.player.apply_output()

    def on_track(self):
        t = self.player.current
        if not t:
            return
        self.title.set_label(t.get("title", ""))
        self.artist.set_label(" · ".join(x for x in (t.get("artist"), t.get("album")) if x))
        self.src_fmt.set_label(self.player.source_format())
        set_heart(self.heart, self.win.store.is_starred(t))
        self._show_stars(self.win.store.rating(t))
        self.vinyl.set_progress(0)
        self.tape.set_text(t.get("title", ""), " · ".join(x for x in (t.get("artist"), t.get("album")) if x))
        self.tape.set_progress(0)

    def on_state(self):
        p = self.player
        self.playb.set_icon_name("media-playback-pause-symbolic" if p.playing else "media-playback-start-symbolic")
        self.vinyl.set_spinning(p.playing)
        self.tape.set_spinning(p.playing)
        self.shuf.set_opacity(1.0 if p.s["shuffle"] else 0.45)
        rep = p.s["repeat"]
        self.rep.set_icon_name("media-playlist-repeat-song-symbolic" if rep == "one"
                               else "media-playlist-repeat-symbolic")
        self.rep.set_opacity(1.0 if rep != "off" else 0.45)
        self.hifi.set_active(p.s["output"] == "direct")
        if p.s["output"] == "direct":
            self.hifi.add_css_class("on")
        else:
            self.hifi.remove_css_class("on")

    def on_position(self, _p, pos, dur):
        if dur > 0:
            self.vinyl.set_progress(pos / dur)
            self.tape.set_progress(pos / dur)
        if self.seeking is not None:
            return
        adj = self.scale.get_adjustment()
        adj.set_upper(max(dur, 1))
        adj.set_value(pos)
        self.pos_lbl.set_label(fmt_time(pos))
        self.dur_lbl.set_label(fmt_time(dur))

    def _seek_drag(self, _scale, _scroll, value):
        first = self.seeking is None
        self.seeking = value
        self.pos_lbl.set_label(fmt_time(value))
        if first:
            GLib.timeout_add(250, self._do_seek)
        return False

    def _do_seek(self):
        self.player.seek(self.seeking)
        self.win.mpris.seeked()
        self.seeking = None
        return False


class MiniPlayer(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, css_classes=["mini"])
        self.win = win
        self.progress = Gtk.ProgressBar(css_classes=["mini-progress"])
        self.append(self.progress)
        inner = Gtk.Box(spacing=14, margin_start=14, margin_end=8, margin_top=12, margin_bottom=12, hexpand=True)
        self.append(inner)
        self.vinyl = Vinyl(natural=72, grooves=False)
        self.vinyl.rpm = win.store.s["rpm"]
        self.vinyl.set_size_request(72, 72)
        inner.append(self.vinyl)
        txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True, valign=Gtk.Align.CENTER)
        self.title = label(APP_NAME, "mini-title")
        self.sub = label("pick something to play", "mini-sub")
        txt.append(self.title)
        txt.append(self.sub)
        inner.append(txt)
        self.playb = icon_button("media-playback-start-symbolic", win.player.toggle, "flat circular mini-btn")
        inner.append(self.playb)
        inner.append(icon_button("media-skip-forward-symbolic", win.player.next, "flat circular mini-btn"))
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: win.open_now_playing())
        inner.add_controller(click)
        swipe = Gtk.GestureSwipe()
        swipe.connect("swipe", lambda _g, vx, vy: vy < -500 and win.open_now_playing())
        inner.add_controller(swipe)
        win.player.connect("track-changed", lambda *_: self.on_track())
        win.player.connect("state-changed", lambda *_: self.on_state())
        win.player.connect("position", lambda _p, pos, dur: dur > 0 and self.progress.set_fraction(pos / dur))

    def on_track(self):
        t = self.win.player.current
        if t:
            self.title.set_label(t.get("title", ""))
            self.sub.set_label(t.get("artist", ""))

    def on_state(self):
        p = self.win.player.playing
        self.playb.set_icon_name("media-playback-pause-symbolic" if p else "media-playback-start-symbolic")
        self.vinyl.set_spinning(p)


class CoverManager:
    """Resolves the current track's cover: the animated mp4 when the album has one, otherwise the
    still image. The still is never shown first; the record stays blank until we know."""

    def __init__(self, win):
        self.win = win
        self.media = None
        self.album = None
        self.views = [win.np.vinyl, win.mini.vinyl]
        win.player.connect("track-changed", lambda *_: self.on_track())

    def on_track(self):
        t = self.win.player.current
        if not t:
            return
        store = self.win.store
        want = t["id"]
        # the lock screen always gets the still
        store.cover(t.get("coverArt"), 800, lambda p: self._still_for_mpris(want, p))
        album = t.get("albumId")
        if album and album == self.album:
            return
        self.album = album
        self._show(None)

        def anim(path):
            if self.album != album:
                return
            if path:
                media = Gtk.MediaFile.new_for_filename(path)
                media.set_loop(True)
                media.set_muted(True)
                self._show(media)
            else:
                store.cover(t.get("coverArt"), 800, lambda p: self.album == album and self._show(texture(p)))
        store.anim_lookup(album, anim)

    def _still_for_mpris(self, want, path):
        cur = self.win.player.current
        if cur and cur["id"] == want:
            self.win.mpris.set_art(path)

    def _show(self, paintable):
        old = self.media
        self.media = paintable if isinstance(paintable, Gtk.MediaFile) else None
        for v in self.views:
            v.set_paintable(paintable)
        if old is not None:
            old.clear()  # free the old pipeline now, not whenever the GC gets to it
        self.update()

    def update(self):
        if self.media is not None:
            self.media.play() if not self.win.props.suspended and screen().on else self.media.pause()


# --- settings ----------------------------------------------------------------------------
class SettingsDialog(Adw.PreferencesDialog):
    def __init__(self, win):
        super().__init__(title="Settings")
        self.win = win
        self.s = s = win.store.s
        player = win.player

        # playback
        pg = Adw.PreferencesPage(title="Hi-Fi", icon_name="audio-headphones-symbolic")
        out = Adw.PreferencesGroup(title="Output",
                                   description="Direct output sends the decoded stream to the DAC at its native "
                                               "rate and bit depth (bit-perfect). Volume, ReplayGain and EQ are "
                                               "bypassed there.")
        self.cards = usb_cards()
        opts = ["System mixer (PulseAudio)"] + [f"Direct → {lbl}" for _, lbl in self.cards]
        row = Adw.ComboRow(title="Output", model=Gtk.StringList.new(opts))
        if s["output"] == "direct" and s["direct_device"] in [c[0] for c in self.cards]:
            row.set_selected(1 + [c[0] for c in self.cards].index(s["direct_device"]))
        row.connect("notify::selected", self._output)
        out.add(row)
        pg.add(out)

        rg = Adw.PreferencesGroup(title="ReplayGain")
        modes = ["off", "track", "album"]
        r = Adw.ComboRow(title="Mode", model=Gtk.StringList.new(["Off", "Track", "Album"]),
                         selected=modes.index(s["replaygain"]))
        r.connect("notify::selected", lambda r, _p: self._set_out("replaygain", modes[r.get_selected()]))
        rg.add(r)
        pre = Adw.SpinRow.new_with_range(-12, 12, 0.5)
        pre.set_title("Pre-amp (dB)")
        pre.set_value(s["rg_preamp"])
        pre.connect("notify::value", lambda r, _p: self._set_out("rg_preamp", r.get_value()))
        rg.add(pre)
        pg.add(rg)

        eq = Adw.PreferencesGroup(title="Equalizer")
        sw = Adw.SwitchRow(title="Enable", active=s["eq_enabled"])
        sw.connect("notify::active", lambda r, _p: self._set_out("eq_enabled", r.get_active()))
        eq.add(sw)
        names = list(EQ_PRESETS)
        pr = Adw.ComboRow(title="Preset", model=Gtk.StringList.new(names + ["Custom"]))
        pr.set_selected(names.index(s["eq_preset"]) if s["eq_preset"] in names else len(names))
        pr.connect("notify::selected", self._preset)
        eq.add(pr)
        sliders = Gtk.Box(spacing=2, homogeneous=True, margin_top=10, margin_bottom=10)
        self.eq_scales = []
        for i, freq in enumerate(EQ_FREQS):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            sc = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL, inverted=True, height_request=150,
                           adjustment=Gtk.Adjustment(lower=-12, upper=12, step_increment=0.5,
                                                     value=s["eq_bands"][i]))
            sc.add_mark(0, Gtk.PositionType.LEFT, None)
            sc.connect("value-changed", self._band, i, pr)
            self.eq_scales.append(sc)
            col.append(sc)
            col.append(label(freq, "eq-freq", xalign=0.5, ellipsize=False))
            sliders.append(col)
        eq.add(sliders)
        pg.add(eq)

        aq = Adw.PreferencesGroup(title="Queue")
        auto = Adw.SwitchRow(title="✦ Auto-queue with AudioMuse",
                             subtitle="When the queue is about to end, add songs like the one playing",
                             active=s["auto_queue"])

        def auto_changed(r, _p):
            s.set("auto_queue", r.get_active())
            if r.get_active():
                player._maybe_autoqueue()
        auto.connect("notify::active", auto_changed)
        aq.add(auto)

        xf = Adw.SwitchRow(title="Smart crossfade",
                           subtitle="Albums stay gapless, quiet outros fade early, silent intros are "
                                    "waited out. Off in HI-FI (bit-perfect).",
                           active=s["crossfade"])
        xf.connect("notify::active", lambda r, _p: s.set("crossfade", r.get_active()))
        aq.add(xf)
        xl = Adw.SpinRow.new_with_range(1, 12, 1)
        xl.set_title("Crossfade length")
        xl.set_subtitle("seconds")
        xl.set_value(s["crossfade_s"])
        xl.connect("notify::value", lambda r, _p: s.set("crossfade_s", float(r.get_value())))
        xf.bind_property("active", xl, "sensitive", GObject.BindingFlags.SYNC_CREATE)
        aq.add(xl)
        pg.add(aq)

        q = Adw.PreferencesGroup(title="Streaming quality",
                                 description="Downloads for offline and the cache are always the original files.")
        keys = list(QUALITY)
        for key, title in (("quality_wifi", "Wi-Fi"), ("quality_mobile", "Mobile data")):
            r = Adw.ComboRow(title=title, model=Gtk.StringList.new([QUALITY[k][0] for k in keys]),
                             selected=keys.index(s[key]))
            r.connect("notify::selected", lambda r, _p, key=key: s.set(key, keys[r.get_selected()]))
            q.add(r)
        pg.add(q)
        self.add(pg)

        # storage
        st = Adw.PreferencesPage(title="Storage", icon_name="drive-harddisk-symbolic")
        g = Adw.PreferencesGroup(title="Automatic cache",
                                 description="Played tracks and the next ones in the queue are saved so they "
                                             "play instantly and offline. Oldest are removed past the limit.")
        a = Adw.SwitchRow(title="Cache while playing", active=s["auto_cache"])
        a.connect("notify::active", lambda r, _p: s.set("auto_cache", r.get_active()))
        g.add(a)
        lim = Adw.SpinRow.new_with_range(1, 64, 1)
        lim.set_title("Cache size limit (GB)")
        lim.set_value(s["cache_limit_gb"])
        lim.connect("notify::value", lambda r, _p: s.set("cache_limit_gb", int(r.get_value())))
        g.add(lim)
        pf = Adw.SpinRow.new_with_range(0, 10, 1)
        pf.set_title("Prefetch next tracks")
        pf.set_value(s["prefetch"])
        pf.connect("notify::value", lambda r, _p: s.set("prefetch", int(r.get_value())))
        g.add(pf)
        mob = Adw.SwitchRow(title="Download on mobile data", active=s["cache_on_mobile"])
        mob.connect("notify::active", lambda r, _p: s.set("cache_on_mobile", r.get_active()))
        g.add(mob)
        (asize, acount), (psize, pcount) = win.store.cache_usage()
        use = Adw.ActionRow(title="Auto cache", subtitle=f"{acount} tracks · {asize / 1024 ** 3:.2f} GB")
        clr = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER, css_classes=["destructive-action"])

        def clear(*_):
            win.store.clear_auto_cache()
            use.set_subtitle("0 tracks · 0 GB")
        clr.connect("clicked", clear)
        use.add_suffix(clr)
        g.add(use)
        g.add(Adw.ActionRow(title="Pinned offline", subtitle=f"{pcount} tracks · {psize / 1024 ** 3:.2f} GB"))
        st.add(g)
        self.add(st)

        # look
        lk = Adw.PreferencesPage(title="Look", icon_name="applications-graphics-symbolic")
        g = Adw.PreferencesGroup(title="Now playing")
        an = Adw.SwitchRow(title="Animated covers", subtitle="cover.webp from the album folder",
                           active=s["animated_covers"])
        an.connect("notify::active", lambda r, _p: s.set("animated_covers", r.get_active()))
        g.add(an)
        am = Adw.SwitchRow(title="Fetch animated covers on mobile data", active=s["anim_on_mobile"])
        am.connect("notify::active", lambda r, _p: s.set("anim_on_mobile", r.get_active()))
        g.add(am)
        ga = Adw.SwitchRow(title="Animate album grids", subtitle="Heavy: one video per cover on screen (max 8)",
                           active=s["grid_anim"])
        ga.connect("notify::active", lambda r, _p: s.set("grid_anim", r.get_active()))
        g.add(ga)
        speeds = [(33.3, "33⅓ rpm"), (16.0, "16 rpm"), (8.0, "8 rpm (slow)"), (4.0, "4 rpm (drift)"),
                  (2.0, "2 rpm (glacial)"), (0.0, "Don't spin")]
        sp = Adw.ComboRow(title="Record speed", model=Gtk.StringList.new([n for _, n in speeds]))
        sp.set_selected(next((i for i, (v, _) in enumerate(speeds) if abs(v - s["rpm"]) < 0.1), 0))

        def speed(r, _p):
            v = speeds[r.get_selected()][0]
            s.set("rpm", v)
            for vin in (win.np.vinyl, win.mini.vinyl, win.np.tape):
                vin.rpm = v
                vin.set_spinning(player.playing)
        sp.connect("notify::selected", speed)
        g.add(sp)
        styles = [("vinyl", "Vinyl record"), ("cassette", "Vaporwave cassette")]
        st = Adw.ComboRow(title="Now playing style", subtitle="Long-press the record or tape to flip",
                          model=Gtk.StringList.new([n for _, n in styles]))
        st.set_selected(next((i for i, (v, _) in enumerate(styles) if v == s["np_style"]), 0))
        st.connect("notify::selected", lambda r, _p: win.np.set_style(styles[r.get_selected()][0]))
        g.add(st)
        port = Adw.SwitchRow(title="Keep portrait", subtitle="Don't auto-rotate while this app is open",
                             active=portrait_locked())
        port.connect("notify::active", lambda r, _p: set_portrait_locked(r.get_active()))
        g.add(port)
        lk.add(g)
        acc = Adw.PreferencesGroup(title="Account")
        acc.add(Adw.ActionRow(title=GLib.markup_escape_text(s["user"] or "-"),
                              subtitle=GLib.markup_escape_text(s["server"])))
        out_btn = Adw.ButtonRow(title="Sign out", css_classes=["destructive-action"])
        out_btn.connect("activated", lambda *_: (win.store.logout(), self.close(), win.show_login()))
        acc.add(out_btn)
        lk.add(acc)
        self.add(lk)

    def _output(self, row, _p):
        i = row.get_selected()
        if i == 0:
            self.s.set("output", "pulse")
        else:
            self.s["direct_device"] = self.cards[i - 1][0]
            self.s.set("output", "direct")
        self.win.player.apply_output()
        self.win.np.on_state()

    def _set_out(self, key, value):
        self.s.set(key, value)
        self.win.player.apply_output()

    def _preset(self, row, _p):
        names = list(EQ_PRESETS)
        i = row.get_selected()
        if i >= len(names):
            return
        self.s["eq_preset"] = names[i]
        self._applying = True
        for sc, v in zip(self.eq_scales, EQ_PRESETS[names[i]]):
            sc.set_value(v)
        self._applying = False
        self.s.set("eq_bands", list(EQ_PRESETS[names[i]]))
        self.win.player.apply_eq()

    def _band(self, sc, i, preset_row):
        bands = list(self.s["eq_bands"])
        bands[i] = sc.get_value()
        self.s["eq_bands"] = bands
        if not getattr(self, "_applying", False):
            self.s["eq_preset"] = "Custom"
            preset_row.set_selected(len(EQ_PRESETS))
        self.s.save()
        self.win.player.apply_eq()


# --- window ------------------------------------------------------------------------------
class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME, default_width=420, default_height=860)
        self.app = app
        self.store = app.store
        self.player = app.player
        self.mpris = app.mpris
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.store.on("online", lambda on: self.toast("Back online" if on else "Offline: showing downloaded music"))
        self.player.connect("error", lambda _p, msg: self.toast(msg))
        self.player.connect("auto-queued", lambda _p, n, title: self.toast(f"✦ Auto-queued {n} songs like “{title}”"))
        if self.store.api:
            self.build()
        else:
            self.show_login()

    def store_events(self, event, cb):
        self.store.on(event, cb)

    def toast(self, msg):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(msg)), timeout=3))

    # DAP mode (~/dap/nrdap, 10-21): music only, radios + background off. The flag file is the
    # truth (the shell's widget can toggle it too), so the button just mirrors it.
    DAP_FLAG = os.path.expanduser("~/.local/state/nrdap/on")

    def _dap_button(self):
        self.dap_btn = Gtk.Button(label="DAP", css_classes=["flat", "dap-btn"],
                                  tooltip_text="DAP mode: music only, radios off")
        self.dap_btn.connect("clicked", lambda *_: self._dap_toggle())
        os.makedirs(os.path.dirname(self.DAP_FLAG), exist_ok=True)
        mon = Gio.File.new_for_path(os.path.dirname(self.DAP_FLAG)).monitor_directory(Gio.FileMonitorFlags.NONE, None)
        mon.connect("changed", lambda *_: self._dap_sync())
        self._dap_mon = mon
        self._dap_sync()
        return self.dap_btn

    def _dap_sync(self):
        on = os.path.exists(self.DAP_FLAG)
        if on:
            self.dap_btn.add_css_class("dap-on")
        else:
            self.dap_btn.remove_css_class("dap-on")

    def _dap_toggle(self):
        on = os.path.exists(self.DAP_FLAG)
        self.toast("Leaving DAP mode…" if on else "DAP mode: radios off, background stopped")
        subprocess.Popen([os.path.expanduser("~/dap/nrdap"), "off" if on else "on"], start_new_session=True)

    def build(self):
        self.nav = Adw.NavigationView()
        root = Adw.ToolbarView()
        hb = Adw.HeaderBar(css_classes=["vapor-bar"])
        hb.set_title_widget(label("NOTREAL.PLAYER", "app-title", xalign=0.5, ellipsize=False))
        hb.pack_start(icon_button("system-search-symbolic", lambda: self.nav.push(SearchPage(self))))
        hb.pack_start(self._dap_button())
        hb.pack_end(icon_button("emblem-system-symbolic", self.show_settings))
        root.add_top_bar(hb)
        self.stack = Adw.ViewStack()
        self.stack.add_titled_with_icon(AlbumsTab(self), "albums", "Albums", "media-optical-symbolic")
        self.stack.add_titled_with_icon(ListTab(self, "artist"), "artists", "Artists", "system-users-symbolic")
        self.stack.add_titled_with_icon(FavouritesTab(self), "favourites", "Favs", "heart-filled-symbolic")
        self.playlists_tab = ListTab(self, "playlist")
        self.stack.add_titled_with_icon(self.playlists_tab, "playlists", "Playlists", "view-list-symbolic")
        self.stack.add_titled_with_icon(OfflineTab(self), "offline", "Offline", "folder-download-symbolic")
        root.set_content(self.stack)
        sw = Adw.ViewSwitcherBar(stack=self.stack, reveal=True)
        root.add_bottom_bar(sw)
        self.nav.add(Adw.NavigationPage(child=root, title=APP_NAME, tag="root"))

        self.np = NowPlaying(self)
        self.mini = MiniPlayer(self)
        self.mini_revealer = Gtk.Revealer(child=self.mini, reveal_child=False,
                                          transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.nav.set_vexpand(True)
        main.append(self.nav)
        main.append(self.mini_revealer)
        self.pages = Gtk.Stack(transition_duration=280)
        self.pages.add_named(main, "main")
        self.pages.add_named(self.np, "np")
        self.covers = CoverManager(self)
        self.connect("notify::suspended", lambda *_: self.covers.update())
        screen().connect(self.covers.update)
        self.player.connect("track-changed", lambda *_: self.mini_revealer.set_reveal_child(True))
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, kv, *_: kv == Gdk.KEY_Escape and self.np_open()
                     and (self.close_now_playing() or True))
        self.add_controller(keys)
        self.toasts.set_child(self.pages)
        GLib.idle_add(lambda: (self.player.restore_state(), False)[1])
        bg(self.store.sync_pins)

    def show_login(self):
        page = Adw.StatusPage(title=APP_NAME, description="Sign in to your Navidrome server",
                              icon_name="dev.notareal.Player")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        group = Adw.PreferencesGroup()
        server = Adw.EntryRow(title="Server", text=self.store.s["server"])
        user = Adw.EntryRow(title="Username")
        pw = Adw.PasswordEntryRow(title="Password")
        for r in (server, user, pw):
            group.add(r)
        box.append(group)
        btn = Gtk.Button(label="Sign in", css_classes=["pill", "suggested-action"], halign=Gtk.Align.CENTER)
        box.append(btn)
        page.set_child(Adw.Clamp(maximum_size=420, child=box))

        def go(*_):
            btn.set_sensitive(False)

            def ok(_):
                self.build()
            def fail(e):
                btn.set_sensitive(True)
                self.toast(f"Sign in failed: {e}")
            bg(lambda: self.store.login(server.get_text().strip(), user.get_text().strip(), pw.get_text()),
               ok, fail)
        btn.connect("clicked", go)
        pw.connect("entry-activated", go)
        self.toasts.set_child(page)

    # navigation
    def np_open(self):
        return self.pages.get_visible_child_name() == "np"

    def open_now_playing(self):
        if self.player.current:
            self.pages.set_transition_type(Gtk.StackTransitionType.SLIDE_UP)
            self.pages.set_visible_child_name("np")

    def close_now_playing(self):
        self.pages.set_transition_type(Gtk.StackTransitionType.SLIDE_DOWN)
        self.pages.set_visible_child_name("main")

    def play_pool(self, kind):
        """Shuffle the whole library ("all") or the never-played songs ("unheard"): 500 at a time,
        the player refills from the same pool when the queue runs low."""
        self.toast("Shuffling everything…" if kind == "all" else "Finding songs you haven't heard…")

        def start(res):
            batch, take, total = res
            if not batch:
                self.toast("You've heard everything!" if kind == "unheard" else "Library is empty")
                return
            self.player.play_tracks(batch, shuffle=False, refill=take)
            self.toast(f"Shuffling all {total:,} songs" if kind == "all" else f"Unheard · {total:,} songs")
        bg(lambda: self.store.song_pool(kind), start, lambda e: self.toast(f"Couldn't load the library: {e}"))

    def open_collection(self, kind, item):
        self.close_now_playing()
        self.nav.push(CollectionPage(self, kind, item))

    def open_artist(self, artist):
        self.close_now_playing()
        self.nav.push(ArtistPage(self, artist))

    def show_settings(self):
        SettingsDialog(self).present(self)

    def show_queue(self):
        dlg = Adw.Dialog(title="Queue", content_width=420, content_height=640)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list-flat"])
        cur_row = None
        for i, t in enumerate(self.player.queue):
            row = Adw.ActionRow(title=GLib.markup_escape_text(t.get("title", "?")),
                                subtitle=GLib.markup_escape_text(("✦ " if t.get("autoQueued") else "")
                                                                 + t.get("artist", "")), activatable=True)
            if i == self.player.index:
                row.add_css_class("current")
                cur_row = row
            elif i > self.player.index:
                rm = Gtk.Button(icon_name="list-remove-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER)
                rm.connect("clicked", lambda _b, i=i, row=row: (self.player.remove_at(i), dlg.close(),
                                                               self.show_queue()))
                row.add_suffix(rm)
            if self.store.is_local(t["id"]):
                row.add_suffix(Gtk.Image(icon_name="folder-download-symbolic", css_classes=["local-dot"]))
            row.connect("activated", lambda _r, i=i: (self.player.play_index(i), dlg.close()))
            lb.append(row)
        sc.set_child(lb)
        tv.set_content(sc)
        dlg.set_child(tv)
        dlg.present(self)
        if cur_row:
            GLib.timeout_add(200, lambda: (cur_row.grab_focus(), False)[1])

    def audiomuse_mix(self, track):
        """AudioMuse-AI (through Navidrome's similar-songs) -> play as the up-next queue or save."""
        dlg = Adw.AlertDialog(heading="✦ AudioMuse mix",
                              body=f"Songs that sound like “{track.get('title', '?')}” by {track.get('artist', '?')}")
        count = Adw.SpinRow.new_with_range(10, 200, 5)
        count.set_title("Songs")
        count.set_value(50)
        box = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        box.append(count)
        dlg.set_extra_child(box)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("save", "Save playlist")
        dlg.add_response("play", "Play mix")
        dlg.set_response_appearance("play", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("play")
        dlg.set_close_response("cancel")

        def chosen(_d, resp):
            if resp == "cancel":
                return
            n = int(count.get_value())
            self.toast("AudioMuse is listening…")

            def got(songs):
                songs = [x for x in songs if x["id"] != track["id"]]
                if not songs:
                    self.toast("AudioMuse found nothing similar")
                    return
                if resp == "play":
                    cur = self.player.current
                    if cur and cur["id"] == track["id"]:
                        self.player.replace_upcoming(songs)
                        self.toast(f"Up next: {len(songs)} songs like this one")
                    else:
                        self.player.play_tracks([track] + songs, shuffle=False)
                        self.toast(f"Playing a {len(songs) + 1}-song mix")
                else:
                    name = f"✦ {track.get('title', 'Mix')} mix"
                    bg(lambda: self.store.api.create_playlist(name, [track["id"]] + [x["id"] for x in songs]),
                       lambda pl: (self.toast(f"Saved playlist “{name}” ({len(songs) + 1} songs)"),
                                   self.playlists_tab.reload()),
                       lambda e: self.toast(f"Couldn't save: {e}"))
            bg(lambda: self.store.api.similar(track["id"], n), got,
               lambda e: self.toast(f"AudioMuse failed: {e}"))
        dlg.connect("response", chosen)
        dlg.present(self)

    def track_menu(self, track):
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        def item(text, fn):
            b = Gtk.Button(label=text, css_classes=["flat"])
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda *_: (pop.popdown(), fn()))
            box.append(b)
        def fav():
            on = not self.store.is_starred(track)
            self.store.set_star("song", track, on)
            self.toast("Added to favourites" if on else "Removed from favourites")
        item("♥ Favourite / unfavourite", fav)
        item("✦ AudioMuse mix…", lambda: self.audiomuse_mix(track))
        item("Play next", lambda: self.player.play_next([track]))
        item("Add to queue", lambda: self.player.add_to_queue([track]))
        if track.get("albumId"):
            item("Go to album", lambda: self.open_collection("album", {
                "id": track["albumId"], "name": track.get("album", ""), "artist": track.get("artist", ""),
                "coverArt": track.get("coverArt"), "year": track.get("year")}))
        pop.set_child(box)
        return pop

    def collection_menu(self, get_tracks):
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for text, fn in (("Play next", self.player.play_next), ("Add to queue", self.player.add_to_queue)):
            b = Gtk.Button(label=text, css_classes=["flat"])
            b.connect("clicked", lambda _b, fn=fn: (pop.popdown(), fn(get_tracks())))
            box.append(b)
        pop.set_child(box)
        return pop


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.win = None
        self.inhibit_cookie = 0

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        # shared look from ~/theme/theme.css, then this app's style.css (both live-reload)
        notareal_theme.apply(os.path.join(HERE, "style.css"))
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(os.path.join(HERE, "icons"))
        self.store = Store(Settings())
        self.player = Player(self.store)
        self.mpris = Mpris(self, self.player)
        self.player.connect("state-changed", self._inhibit)
        self.hold()   # keep playing with the window closed

    # Gtk.Application.inhibit() registers nothing under Phosh (GetInhibitors stayed empty while
    # playing), so gsd-power idle-suspended the phone 5 min after the screen went off, mid-song.
    # Ask the session manager directly; fall back to holding a logind sleep lock.
    def _inhibit(self, *_):
        if self.player.playing and not self.inhibit_cookie:
            self.inhibit_cookie = self._inhibit_on()
        elif not self.player.playing and self.inhibit_cookie:
            self._inhibit_off(self.inhibit_cookie)
            self.inhibit_cookie = 0

    def _inhibit_on(self):
        bus = self.get_dbus_connection() or Gio.bus_get_sync(Gio.BusType.SESSION)
        try:
            r = bus.call_sync("org.gnome.SessionManager", "/org/gnome/SessionManager", "org.gnome.SessionManager",
                              "Inhibit", GLib.Variant("(susu)", (APP_ID, 0, "Playing music", 4)),  # 4 = suspend
                              GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 2000, None)
            return ("sm", r.unpack()[0])
        except GLib.Error as e:
            print("session inhibit failed:", e.message)
        try:
            sysbus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
            r, fds = sysbus.call_with_unix_fd_list_sync(
                "org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "Inhibit",
                GLib.Variant("(ssss)", ("sleep", APP_NAME, "Playing music", "block")),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, 2000, None, None)
            return ("fd", fds.get(r.unpack()[0]))
        except GLib.Error as e:
            print("logind inhibit failed:", e.message)
        return 0

    def _inhibit_off(self, cookie):
        kind, val = cookie
        if kind == "sm":
            bus = self.get_dbus_connection() or Gio.bus_get_sync(Gio.BusType.SESSION)
            try:
                bus.call_sync("org.gnome.SessionManager", "/org/gnome/SessionManager", "org.gnome.SessionManager",
                              "Uninhibit", GLib.Variant("(u)", (val,)), None, Gio.DBusCallFlags.NONE, 2000, None)
            except GLib.Error as e:
                print("uninhibit failed:", e.message)
        else:
            os.close(val)

    def do_activate(self):
        if not self.win:
            self.win = Window(self)
            self.win.connect("close-request", self._on_close)
        self.win.present()

    def _on_close(self, *_):
        # closing the window keeps music going; quit when nothing is playing
        if not self.player.playing:
            self.player.shutdown()
            self.release()
        self.win = None
        return False

    def do_shutdown(self):
        self.player.shutdown()
        Adw.Application.do_shutdown(self)


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
