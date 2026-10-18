#!/usr/bin/env python3
"""notarealMaps: Google's maps, places and traffic routes with voice turn-by-turn, vapor look.

Map = Google Map Tiles API (own night style) in libshumate; search/details = Places API (New);
routes = Routes API (traffic aware); GPS = GeoClue (modem GPS); voice = piper.
Left-handed: actions sit on the left.
"""
import datetime
import math
import os
import signal
import sys
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Shumate", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango, Shumate  # noqa: E402
import notareal_theme  # noqa: E402  ~/theme/theme.css

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gmaps  # noqa: E402
import nav  # noqa: E402
import saved  # noqa: E402
import voice  # noqa: E402
from location import Location, Simulator  # noqa: E402

APP_ID = "dev.notareal.Maps"
HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.environ.get("NOTAREAL_MAPS_SIM") == "1"
MODES = [("DRIVE", "🚗", "drive"), ("TWO_WHEELER", "🛵", "moto"), ("BICYCLE", "🚲", "bike"),
         ("WALK", "🚶", "walk"), ("TRANSIT", "🚆", "transit")]
NAV_MODES = ("DRIVE", "TWO_WHEELER", "BICYCLE", "WALK")
TRAFFIC_COLORS = {"NORMAL": "#8fe0f2", "SLOW": "#ffb86b", "TRAFFIC_JAM": "#ff5c8a"}
ROUTE_COLOR = "#c5a0ff"
ALT_COLOR = "#c5a0ff"            # drawn at 45 % (see draw_route)
MAX_RECENTS = 12


# map-only vapor shades -> palette names, used when another theme is active
MAP_EXTRA = {"#141a33": "bg", "#ff5c8a": "hot-pink"}


def rgba(s):
    """Vapor hex / css color -> Gdk.RGBA in the ACTIVE theme (same-named color)."""
    if notareal_theme.active() != "vapor" and isinstance(s, str) and s.lower() in MAP_EXTRA:
        s = notareal_theme.hex(MAP_EXTRA[s.lower()])
    s = notareal_theme.resolve(s)
    c = Gdk.RGBA()
    c.parse(s)
    return c


def bg(fn, done=None, fail=None):
    def run():
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            print("bg:", repr(e), flush=True)
            if fail:
                GLib.idle_add(lambda e=e: (fail(e), False)[1])
            return
        if done:
            GLib.idle_add(lambda: (done(res), False)[1])
    threading.Thread(target=run, daemon=True).start()


def label(text="", css=None, xalign=0.0, wrap=False, ellipsize=True, lines=0):
    lb = Gtk.Label(label=text, xalign=xalign)
    if css:
        for c in css.split():
            lb.add_css_class(c)
    if wrap:
        lb.set_wrap(True)
        lb.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        if lines:
            lb.set_lines(lines)
            lb.set_ellipsize(Pango.EllipsizeMode.END)
    elif ellipsize:
        lb.set_ellipsize(Pango.EllipsizeMode.END)
    return lb


def button(text=None, icon=None, css=None, cb=None, tip=None):
    b = Gtk.Button()
    if text and icon:
        b.set_child(Adw.ButtonContent(icon_name=icon, label=text))
    elif icon:
        b.set_icon_name(icon)
    else:
        b.set_label(text or "")
    if css:
        for c in css.split():
            b.add_css_class(c)
    if cb:
        b.connect("clicked", lambda *_: cb())
    if tip:
        b.set_tooltip_text(tip)
    return b


def fmt_duration(s):
    s = int(s)
    if s < 3600:
        return "%d min" % max(1, round(s / 60))
    return "%d h %02d" % (s // 3600, (s % 3600) // 60)


def fmt_km(m):
    return nav.short_distance(m)


def stars(r):
    return "★ %.1f" % r if r else ""


# --- the GPS puck: an arrow drawn with GSK (no cairo here) -----------------------------------

class Puck(Gtk.Widget):
    def __init__(self):
        super().__init__()
        self.angle = None       # degrees on screen, None = no heading -> dot
        self.set_size_request(44, 44)
        self.set_can_target(False)

    def set_angle(self, a):
        self.angle = a
        self.queue_draw()

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        cx, cy = w / 2, h / 2
        halo = Gsk.PathBuilder.new()
        halo.add_circle(Graphene.Point().init(cx, cy), 20)
        snap.append_fill(halo.to_path(), Gsk.FillRule.WINDING, rgba("rgba(143,224,242,0.18)"))
        snap.save()
        snap.translate(Graphene.Point().init(cx, cy))
        b = Gsk.PathBuilder.new()
        if self.angle is None:
            b.add_circle(Graphene.Point().init(0, 0), 8)
        else:
            snap.rotate(self.angle)
            b.move_to(0, -14)
            b.line_to(10, 11)
            b.line_to(0, 5)
            b.line_to(-10, 11)
            b.close()
        path = b.to_path()
        snap.append_fill(path, Gsk.FillRule.WINDING, rgba("#8fe0f2"))
        snap.append_stroke(path, Gsk.Stroke.new(2.5), rgba("#1c2340"))
        snap.restore()


# --- key setup -------------------------------------------------------------------------------

class SetupPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.win = win
        self.add_css_class("setup")
        self.append(label("NOTREAL.MAPS", "app-title", xalign=0.5))
        self.append(label(
            "Paste your Google Maps Platform API key.\n\n"
            "console.cloud.google.com → new project → enable Map Tiles API, Places API (New) "
            "and Routes API → Credentials → Create API key.", "hint", wrap=True))
        self.entry = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text="AIza…")
        self.entry.add_css_class("key-entry")
        self.entry.connect("activate", lambda *_: self._save())
        self.append(self.entry)
        self.btn = button("SAVE KEY", css="primary-btn", cb=self._save)
        self.btn.set_halign(Gtk.Align.START)
        self.append(self.btn)
        self.status = label("", "hint", wrap=True)
        self.append(self.status)

    def _save(self):
        key = self.entry.get_text().strip()
        if not key:
            return
        self.btn.set_sensitive(False)
        self.status.set_label("checking…")
        g = gmaps.Google(key)

        def ok(_):
            gmaps.store_key(key)
            self.win.start(g)

        def fail(e):
            self.btn.set_sensitive(True)
            self.status.set_label("Google said: %s" % e)
        bg(lambda: g.tile_session(), ok, fail)


# --- main window -----------------------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="notreal.maps", default_width=420, default_height=900)
        self.g = None
        self.cfg = gmaps.load_config()
        self.fix = None
        self.rough = False
        self.follow = True
        self.navigating = None       # nav.Navigator while driving
        self.dest = None
        self.start_pt = None            # manual starting point {name, lat, lng} (no/poor GPS); None = GPS
        self.picking_start = False   # the next searched / long-pressed place becomes the start
        self.routes = []
        self.route_i = 0
        self.mode = self.cfg.get("mode", "DRIVE")
        self.place = None
        self.ac_timer = 0
        self.ac_gen = 0
        self.sim = None
        self.inhibit = 0
        self.voice = voice.Voice()
        self.voice.enabled = self.cfg.get("voice", True)
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        key = gmaps.get_key()
        if key:
            self.start(gmaps.Google(key))
        else:
            self.toasts.set_child(SetupPage(self))

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(text)), timeout=3))

    # --- build ---------------------------------------------------------------------------
    def start(self, g):
        self.g = g
        self.map = Shumate.Map()
        self.map.add_css_class("map")
        self.viewport = self.map.get_viewport()
        self.base_layer = None
        self.traffic_on = self.cfg.get("traffic", False)
        self.alt_layers, self.route_layers = [], []
        self.marks = Shumate.MarkerLayer.new(self.viewport)
        self.saved_layer = Shumate.MarkerLayer.new(self.viewport)
        self.saved_on = self.cfg.get("show_saved", True)
        self.puck = Puck()
        self.puck_marker = Shumate.Marker()
        self.puck_marker.set_child(self.puck)
        self.pin = Shumate.Marker()
        self.pin.set_child(label("⬤", "pin", xalign=0.5))
        self.pin.set_visible(False)
        self.pin.connect("notify::visible", self._sync_clear)
        self.result_marks = []

        ov = Gtk.Overlay(child=self.map)
        ov.add_overlay(self._top())
        self.entry.connect("changed", self._sync_clear)
        ov.add_overlay(self._fabs())
        ov.add_overlay(self._panel())
        ov.add_overlay(self._banner())
        ov.add_overlay(self._navbar())
        self.toasts.set_child(ov)

        drag = Gtk.GestureDrag(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        drag.connect("drag-update", self._user_moved)
        self.map.add_controller(drag)
        lp = Gtk.GestureLongPress()
        lp.connect("pressed", self._long_press)
        self.map.add_controller(lp)
        # Google-style one-finger zoom: double-tap, keep the finger down, drag down = in / up = out
        dz = Gtk.GestureDrag(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        dz.connect("drag-begin", self._dz_begin)
        dz.connect("drag-update", self._dz_update)
        dz.connect("drag-end", self._dz_end)
        self.map.add_controller(dz)
        self._dz = None
        self._tap = (0.0, 0.0, 0.0)       # time, x, y of the last quick tap
        self._press = (0.0, 0.0, 0.0)

        v = self.cfg.get("view")
        lat, lng, z = v if v else (59.3293, 18.0686, 12)
        self.viewport.set_zoom_level(z)
        self.map.center_on(lat, lng)
        self._set_base()
        notareal_theme.on_change(self._theme_changed)
        self.loc = None
        if not SIM:
            self.loc = Location(self._on_fix, lambda e: self.toast("GPS: " + e))

    def _theme_changed(self, *_):
        """Theme switch: new tile session/cache for the new look, recolor puck + routes."""
        if self.g and self.base_layer is not None:
            self._set_base()
        self.puck.queue_draw()
        if getattr(self, "routes", None) and getattr(self, "panel_kind", "") == "route":
            self.draw_route(self.routes, getattr(self, "route_i", 0))

    def _set_base(self):
        traffic = self.traffic_on

        def ok(session):
            # the source id names the tile cache: one per theme, or old-theme tiles would linger
            src = Shumate.RasterRenderer.new_full_from_url(
                ("google-traffic" if traffic else "google") + "-" + notareal_theme.active(), "Google", "© Google", "https://www.google.com/maps",
                0, 22, 256, Shumate.MapProjection.MERCATOR, self.g.tile_url(session))
            layer = Shumate.MapLayer.new(src, self.viewport)
            old = self.base_layer
            self.map.insert_layer_behind(layer, old) if old else self.map.add_layer(layer)
            if old:
                self.map.remove_layer(old)
            else:
                for lyr in (self.saved_layer, self.marks):
                    self.map.add_layer(lyr)
                self.marks.add_marker(self.pin)
                self.marks.add_marker(self.puck_marker)
                self._draw_saved()
            self.base_layer = layer
            self.map.set_map_source(src)
        bg(lambda: self.g.tile_session(traffic), ok, lambda e: self.toast("map: %s" % e))

    def _top(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.START)
        box.add_css_class("top")
        row = Gtk.Box(spacing=6)
        row.add_css_class("search-row")
        self.menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic")
        self.menu_btn.add_css_class("flat")
        self.menu_btn.set_popover(self._menu())
        row.append(self.menu_btn)
        # ✕ = clear everything (search, results, pins, routes) back to the plain map; left for the thumb
        self.clear_btn = Gtk.Button(icon_name="window-close-symbolic", visible=False, tooltip_text="clear")
        self.clear_btn.add_css_class("flat")
        self.clear_btn.add_css_class("clear-all")
        self.clear_btn.connect("clicked", lambda *_: self.reset_map())
        row.append(self.clear_btn)
        self.entry = Gtk.SearchEntry(placeholder_text="search Google Maps", hexpand=True)
        self.entry.add_css_class("search")
        self.entry.connect("search-changed", self._typed)
        self.entry.connect("activate", self._search)
        self.entry.connect("stop-search", lambda *_: self._clear_search())
        fc = Gtk.EventControllerFocus()
        fc.connect("enter", lambda *_: self._show_suggestions())
        self.entry.add_controller(fc)
        row.append(self.entry)
        box.append(row)
        self.sugg = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.sugg.add_css_class("suggestions")
        self.sugg.connect("row-activated", lambda lb, r: self._pick(r.item))
        self.sugg_rev = Gtk.Revealer(child=self.sugg, reveal_child=False)
        box.append(self.sugg_rev)
        self.top_box = box
        return box

    def _menu(self):
        pop = Gtk.Popover()
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        b.add_css_class("menu")
        self.voice_btn = Gtk.CheckButton(label="spoken directions", active=self.voice.enabled)
        self.voice_btn.connect("toggled", lambda c: self._set_pref("voice", c.get_active()))
        b.append(self.voice_btn)
        self.saved_btn = Gtk.CheckButton(label="saved places on the map", active=self.saved_on)
        self.saved_btn.connect("toggled", lambda c: self._show_saved_marks(c.get_active()))
        b.append(self.saved_btn)
        for text, cb in (("★ saved places", self.show_saved),
                         ("import from Google (Takeout)", self._import_dialog),
                         ("clear recent places", self._clear_recents),
                         ("change API key", self._change_key)):
            x = button(text, css="flat", cb=lambda cb=cb: (pop.popdown(), cb()))
            x.get_child().set_xalign(0)
            b.append(x)
        pop.set_child(b)
        return pop

    def _fabs(self):
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, halign=Gtk.Align.START,
                      valign=Gtk.Align.END, margin_start=14, margin_bottom=24)
        self.fab_box = col
        self.compass = button(css="fab", cb=self._north, tip="North up")
        self.compass.set_child(label("N", "fab-n", xalign=0.5))
        self.compass.set_visible(False)
        col.append(self.compass)
        self.traffic_btn = button(icon="network-cellular-signal-excellent-symbolic", css="fab", tip="Traffic",
                                  cb=self._toggle_traffic)
        if self.traffic_on:
            self.traffic_btn.add_css_class("on")
        col.append(self.traffic_btn)
        self.locate_btn = button(icon="find-location-symbolic", css="fab", tip="My location", cb=self._locate)
        col.append(self.locate_btn)
        return col

    def _panel(self):
        self.panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.panel.add_css_class("panel")
        handle = Gtk.Box(halign=Gtk.Align.CENTER)
        handle.add_css_class("handle")
        self.panel.append(handle)
        self.panel_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.panel_sc = Gtk.ScrolledWindow(child=self.panel_body, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                           propagate_natural_height=True, max_content_height=430)
        self.panel.append(self.panel_sc)
        sw = Gtk.GestureSwipe()
        sw.connect("swipe", lambda g, vx, vy: vy > 600 and self.close_panel())
        handle.add_controller(sw)
        self.panel_rev = Gtk.Revealer(child=self.panel, valign=Gtk.Align.END, reveal_child=False,
                                      transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        self.panel_rev.connect("notify::reveal-child", self._sync_clear)
        return self.panel_rev

    def _banner(self):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        b.add_css_class("banner")
        row = Gtk.Box(spacing=14)
        self.b_icon = label("⬆", "b-icon", xalign=0.5, ellipsize=False)
        row.append(self.b_icon)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self.b_dist = label("", "b-dist")
        self.b_text = label("", "b-text", wrap=True, lines=2)
        col.append(self.b_dist)
        col.append(self.b_text)
        row.append(col)
        b.append(row)
        self.b_then = label("", "b-then")
        b.append(self.b_then)
        self.banner_rev = Gtk.Revealer(child=b, valign=Gtk.Align.START, reveal_child=False,
                                       transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        return self.banner_rev

    def _navbar(self):
        bar = Gtk.Box(spacing=12)
        bar.add_css_class("navbar")
        end = button("END", css="end-btn", cb=self.end_nav)
        bar.append(end)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.n_eta = label("", "n-eta")
        self.n_left = label("", "n-left")
        col.append(self.n_eta)
        col.append(self.n_left)
        bar.append(col)
        self.n_recenter = button(icon="find-location-symbolic", css="flat", cb=self._recenter, tip="Re-center")
        self.n_recenter.set_visible(False)
        bar.append(self.n_recenter)
        self.n_speed = label("", "n-speed", xalign=1.0, ellipsize=False)
        bar.append(self.n_speed)
        self.navbar_rev = Gtk.Revealer(child=bar, valign=Gtk.Align.END, reveal_child=False,
                                       transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        return self.navbar_rev

    # --- prefs ---------------------------------------------------------------------------
    def _set_pref(self, k, v):
        self.cfg = gmaps.load_config()
        self.cfg[k] = v
        gmaps.save_config(self.cfg)
        if k == "voice":
            self.voice.enabled = v

    def _clear_recents(self):
        self._set_pref("recents", [])
        self.toast("recent places cleared")

    def _change_key(self):
        self.toasts.set_child(SetupPage(self))

    # --- location ------------------------------------------------------------------------
    def _on_fix(self, fix):
        first = self.fix is None
        self.rough = self.fix is not None and (self.fix.get("acc") or 0) > 500
        self.fix = fix
        self.puck_marker.set_location(fix["lat"], fix["lng"])
        self._puck_angle()
        if self.navigating:
            self._nav_fix(fix)
        elif self.panel_rev.get_reveal_child() and getattr(self, "panel_kind", "") == "place":
            pass   # a place is shown (e.g. a location from notarealChat): don't yank the map away
        elif (fix.get("acc") or 0) > 5000 and self._view_near(fix, 200000):
            pass   # cell-tower guess (10-13: Eskilstuna, ±25 km): stay on Stockholm / the last view
        elif first or (self.rough and (fix.get("acc") or 0) < 200):
            # cell-tower fixes are ~25 km off: don't zoom in on them
            self.map.go_to_full_with_duration(fix["lat"], fix["lng"], 15 if (fix.get("acc") or 0) < 500 else 11, 600)
        elif self.follow and getattr(self, "locating", False):
            self.map.center_on(fix["lat"], fix["lng"])

    def _view_near(self, fix, meters):
        """Is the map's centre within `meters` of the fix (i.e. no need to fly there)?"""
        c = (self.viewport.get_latitude(), self.viewport.get_longitude())
        return gmaps.dist(c, (fix["lat"], fix["lng"])) < meters

    def _puck_angle(self):
        if not self.fix or self.fix.get("heading") is None:
            self.puck.set_angle(None)
            return
        rot = math.degrees(self.viewport.get_rotation())
        self.puck.set_angle((self.fix["heading"] + rot) % 360)

    def _locate(self):
        if not self.fix:
            self.toast("waiting for GPS…")
            return
        self.locating = True
        self.follow = True
        z = max(self.viewport.get_zoom_level(), 15)
        self.map.go_to_full_with_duration(self.fix["lat"], self.fix["lng"], z, 500)

    def _north(self):
        self.viewport.set_rotation(0)
        self.compass.set_visible(False)
        self._puck_angle()

    def _user_moved(self, g, dx, dy):
        if self._dz or abs(dx) + abs(dy) < 12:
            return
        self.locating = False
        if self.navigating and self.follow:
            self.follow = False
            self.n_recenter.set_visible(True)
        if self.sugg_rev.get_reveal_child():
            self.sugg_rev.set_reveal_child(False)

    def _recenter(self):
        self.follow = True
        self.n_recenter.set_visible(False)
        if self.fix:
            self._nav_camera(self.fix)

    def _toggle_traffic(self):
        self.traffic_on = not self.traffic_on
        (self.traffic_btn.add_css_class if self.traffic_on else self.traffic_btn.remove_css_class)("on")
        self._set_pref("traffic", self.traffic_on)
        self._set_base()

    # --- search --------------------------------------------------------------------------
    def _near(self):
        if self.fix and (self.fix.get("acc") or 0) < 500:
            return self.fix["lat"], self.fix["lng"]
        if self.start_pt:
            return self.start_pt["lat"], self.start_pt["lng"]
        if self.fix:
            return self.fix["lat"], self.fix["lng"]
        return self.viewport.get_latitude(), self.viewport.get_longitude()

    def _set_entry(self, text):
        # filling the box ourselves is not typing (search-changed arrives later, so match the text)
        self.filled = text
        self.entry.set_text(text)

    def _typed(self, e):
        filled = getattr(self, "filled", None)
        if filled is not None:
            if e.get_text() == filled:
                self.filled = None
                return
            if not e.get_text():     # set_text clears first: skip that in-between state
                return
            self.filled = None
        if self.ac_timer:
            GLib.source_remove(self.ac_timer)
            self.ac_timer = 0
        if len(e.get_text().strip()) < 2:
            self._show_suggestions()
            return
        self.ac_timer = GLib.timeout_add(320, self._autocomplete)

    def _autocomplete(self):
        self.ac_timer = 0
        q = self.entry.get_text().strip()
        self.ac_gen += 1
        gen = self.ac_gen
        near = self._near()
        bg(lambda: self.g.autocomplete(q, near), lambda r: gen == self.ac_gen and self._fill_sugg(r, q),
           lambda e: self.toast(e))
        return False

    def _show_suggestions(self):
        if self.navigating:
            return
        if not self.entry.get_text().strip():
            rec = gmaps.load_config().get("recents", [])
            self._fill_sugg([dict(r, recent=True) for r in rec], None)

    def _fill_sugg(self, items, q):
        self.sugg.remove_all()
        if q:
            items = [{"search": q, "name": "search “%s”" % q, "sub": "show all results"}] + items
        if self.picking_start:   # 10-12: there was no way back to GPS once SET START was tapped
            items = [{"gps": True, "name": "My location",
                      "sub": "GPS" if self.fix else "waiting for a GPS fix…"}] + items
        for it in items[:8]:
            r = Gtk.ListBoxRow()
            r.item = it
            box = Gtk.Box(spacing=12)
            box.add_css_class("sugg-row")
            box.append(label("🎯" if it.get("gps") else "🔍" if it.get("search")
                             else "🕘" if it.get("recent") else "📍",
                             "sugg-ico", ellipsize=False))
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            col.append(label(it.get("name", ""), "sugg-name"))
            sub = it.get("sub") or it.get("address") or ""
            if it.get("meters"):
                sub = fmt_km(it["meters"]) + "  ·  " + sub
            if sub:
                col.append(label(sub, "sugg-sub"))
            box.append(col)
            r.set_child(box)
            self.sugg.append(r)
        self.sugg_rev.set_reveal_child(bool(items))

    def _clear_search(self):
        self._set_entry("")
        self.sugg_rev.set_reveal_child(False)
        self._clear_results()

    def reset_map(self):
        """The ✕: back to the default map — no search, results, pin, routes or panel; GPS start."""
        if self.navigating:
            return self.toast("end navigation first")
        self._clear_search()
        self._blur()
        self.dest = None
        if self.picking_start:
            self._drop_pick_toast()
            self._cancel_pick()
        self.start_pt = None
        self.close_panel()
        self.pin.set_visible(False)
        self._clear_routes()
        self.entry.set_placeholder_text("search Google Maps")
        if self.fix:
            self._locate()
        self._sync_clear()

    def _sync_clear(self, *_):
        if not hasattr(self, "panel_rev"):
            return True              # still building the window
        busy = bool(self.entry.get_text() or self.result_marks or self.pin.get_visible()
                    or self.panel_rev.get_reveal_child() or self.route_layers)
        if self.clear_btn.get_visible() != (busy and not self.navigating):
            self.clear_btn.set_visible(busy and not self.navigating)
        return True

    def _blur(self):
        self.ac_gen += 1             # results still on their way must not reopen the list
        if self.ac_timer:
            GLib.source_remove(self.ac_timer)
            self.ac_timer = 0
        self.sugg_rev.set_reveal_child(False)
        self.map.grab_focus()   # drops the on-screen keyboard

    def _pick(self, it):
        self._blur()
        if it.get("gps"):
            return self.use_gps_start()
        if it.get("search"):
            return self._search(None, it["search"])
        if it.get("recent") and it.get("lat") is not None and not it.get("id"):
            return self.show_place(it)
        self._set_entry(it.get("name", ""))
        bg(lambda: self.g.details(it["id"]), self.show_place, lambda e: self.toast(e))

    def _search(self, _e, q=None):
        q = q or self.entry.get_text().strip()
        if not q:
            return
        self._blur()
        near = self._near()
        bg(lambda: self.g.search(q, near), lambda r: self._results(q, r), lambda e: self.toast(e))

    def _clear_results(self):
        for m in self.result_marks:
            self.marks.remove_marker(m)
        self.result_marks = []
        self.last_results = None

    def _results(self, q, places):
        self._clear_results()
        if not places:
            self.toast("nothing found for “%s”" % q)
            return
        if len(places) == 1:
            return self.show_place_quick(places[0])
        for p in places:
            m = Shumate.Marker()
            dot = button(stars(p.get("rating")) or "•", css="result-pin")
            dot.connect("clicked", lambda _b, p=p: self.show_place_quick(p))
            m.set_child(dot)
            m.set_location(p["lat"], p["lng"])
            self.marks.add_marker(m)
            self.result_marks.append(m)
        self.fit([(p["lat"], p["lng"]) for p in places])
        self.last_results = (q, places)
        self.results_scroll = 0
        self.show_results()

    def show_results(self, scroll=0):
        """The result list of the last search (also the card's ‹ RESULTS button)."""
        q, places = self.last_results
        body = self._panel_begin()
        body.append(label("%d RESULTS" % len(places), "section"))
        for p in places:
            body.append(self._place_row(p))
        self.open_panel()
        if scroll:
            # after the list has been laid out, or the adjustment clamps it to 0
            GLib.timeout_add(60, lambda: (self.panel_sc.get_vadjustment().set_value(scroll), False)[1])

    def _in_results(self, p):
        if not getattr(self, "last_results", None):
            return False
        same = (lambda r: r.get("id") == p.get("id")) if p.get("id") else \
               (lambda r: (r.get("lat"), r.get("lng")) == (p.get("lat"), p.get("lng")))
        return any(same(r) for r in self.last_results[1])

    def _place_row(self, p):
        b = Gtk.Button()
        b.add_css_class("place-row")
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        col.append(label(p["name"], "p-name"))
        meta = "  ·  ".join(x for x in (stars(p.get("rating")) + (" (%d)" % p["ratings"] if p.get("ratings") else ""),
                                         p.get("type", "")) if x)
        if meta:
            col.append(label(meta, "p-meta"))
        hours = self._open_text(p)   # own line: at the end of meta it got ellipsized away
        if hours:
            col.append(label(hours, "p-meta " + ("p-open" if p.get("open") and not hours.startswith("closes soon")
                                                 else "p-closed")))
        col.append(label(p.get("address", ""), "p-addr"))
        b.set_child(col)
        b.connect("clicked", lambda *_: self.show_place_quick(p))
        return b

    @staticmethod
    def _open_text(p):
        """'open · closes 18:00' / 'closed · opens Tue 08:30' / 'open 24 hours' (10-13)."""
        o = p.get("open")
        if o is None:
            return ""
        when = p.get("next_close") if o else p.get("next_open")
        if not when:
            return "open 24 hours" if o and "next_close" in p else ("open now" if o else "closed")
        try:
            t = datetime.datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            return "open now" if o else "closed"
        tz = datetime.timezone(datetime.timedelta(minutes=p["utc_off"])) if p.get("utc_off") is not None else None
        t = t.astimezone(tz)
        now = datetime.datetime.now(t.tzinfo)
        day = "" if t.date() == now.date() else \
            "tomorrow " if t.date() == now.date() + datetime.timedelta(days=1) else t.strftime("%a ")
        hm = t.strftime("%H:%M")
        if o:
            soon = (t - now).total_seconds() < 3600
            return ("closes soon · %s" if soon else "open · closes %s") % (day + hm)
        return "closed · opens %s" % (day + hm)

    def show_place_quick(self, p):
        """Show what we have, then fill in details."""
        self.show_place(p)
        if p.get("id") and "hours" not in p:
            bg(lambda: self.g.details(p["id"]), lambda d: self.place is p and self.show_place(d),
               lambda e: self.toast(e))

    # --- place card ----------------------------------------------------------------------
    def _remember(self, p):
        cfg = gmaps.load_config()
        rec = [r for r in cfg.get("recents", []) if r.get("id") != p.get("id") or not p.get("id")]
        rec.insert(0, {k: p.get(k) for k in ("id", "name", "address", "lat", "lng")})
        cfg["recents"] = rec[:MAX_RECENTS]
        gmaps.save_config(cfg)
        self.cfg = cfg

    def show_place(self, p):
        if self.picking_start and p.get("lat") is not None:
            self.set_start(p)
            return
        self.place = p
        self.dest = p
        if p.get("lat") is None:
            return
        self._remember(p)
        self.pin.set_location(p["lat"], p["lng"])
        self.pin.set_visible(True)
        self._clear_routes()
        if not self.panel_rev.get_reveal_child() or getattr(self, "panel_kind", "") != "place":
            self.map.go_to_full_with_duration(p["lat"], p["lng"], max(15, self.viewport.get_zoom_level()), 500)
        if getattr(self, "panel_kind", "") == "list" and self.panel_rev.get_reveal_child():
            self.results_scroll = self.panel_sc.get_vadjustment().get_value()
        body = self._panel_begin("place")
        if self._in_results(p):
            # back to the other results (left-handed: top-left)
            n = len(self.last_results[1])
            back = button("‹ %d RESULTS" % n, css="back-results", tip="Back to the results",
                          cb=lambda: self.show_results(getattr(self, "results_scroll", 0)))
            back.set_halign(Gtk.Align.START)
            body.append(back)
        body.append(label(p["name"], "pc-name", wrap=True))
        meta = []
        if p.get("rating"):
            meta.append(stars(p["rating"]) + (" (%s)" % p["ratings"] if p.get("ratings") else ""))
        if p.get("type"):
            meta.append(p["type"])
        if p.get("price"):
            meta.append("$" * {"PRICE_LEVEL_INEXPENSIVE": 1, "PRICE_LEVEL_MODERATE": 2,
                               "PRICE_LEVEL_EXPENSIVE": 3, "PRICE_LEVEL_VERY_EXPENSIVE": 4}.get(p["price"], 0))
        if meta:
            body.append(label("  ·  ".join(m for m in meta if m), "pc-meta"))
        if p.get("open") is not None:
            txt = self._open_text(p)
            body.append(label("● " + txt, "open" if p["open"] and not txt.startswith("closes soon") else "closed"))
        if p.get("status") in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"):
            body.append(label(p["status"].replace("_", " ").lower(), "closed"))
        if self.fix:
            d = gmaps.dist((self.fix["lat"], self.fix["lng"]), (p["lat"], p["lng"]))
            body.append(label("%s away" % fmt_km(d), "pc-meta"))
        acts = Gtk.Box(spacing=8)
        acts.add_css_class("actions")
        acts.append(button("DIRECTIONS", icon="mark-location-symbolic", css="primary-btn",
                           cb=lambda: self.directions(p)))
        acts.append(button("FROM", css="act from", tip="Start directions from here",
                           cb=lambda: self.set_start(p)))
        star = button("★" if saved.is_saved(p) else "☆", css="act star", tip="Save")
        star.connect("clicked", lambda b: self._toggle_saved(p, b))
        acts.append(star)
        if p.get("phone"):
            acts.append(button(icon="call-start-symbolic", css="act", tip=p["phone"],
                               cb=lambda: self._launch("tel:" + (p.get("phone_intl") or p["phone"]).replace(" ", ""))))
        if p.get("web"):
            acts.append(button(icon="web-browser-symbolic", css="act", tip="Website",
                               cb=lambda: self._launch(p["web"])))
        acts.append(button(icon="emblem-shared-symbolic", css="act", tip="Copy link",
                           cb=lambda: self._copy(p)))
        acts.append(button(icon="go-home-symbolic", css="act", tip="Set as home",
                           cb=lambda: self._set_home(p)))
        body.append(acts)
        if p.get("address"):
            body.append(label(p["address"], "pc-addr", wrap=True))
        if p.get("note"):
            body.append(label("✎ " + p["note"], "pc-summary", wrap=True))
        if p.get("photos"):
            strip = Gtk.Box(spacing=6)
            for name in p["photos"]:
                pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
                pic.add_css_class("photo")
                pic.set_size_request(200, 140)
                strip.append(pic)
                bg(lambda name=name: self.g.photo(name, 480), lambda path, pic=pic: pic.set_filename(path))
            sc = Gtk.ScrolledWindow(child=strip, vscrollbar_policy=Gtk.PolicyType.NEVER, min_content_height=144)
            body.append(sc)
        if p.get("summary"):
            body.append(label(p["summary"], "pc-summary", wrap=True))
        if p.get("hours"):
            ex = Gtk.Expander(label="opening hours")
            ex.add_css_class("hours")
            hb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            today = datetime.datetime.now().weekday()
            for i, h in enumerate(p["hours"]):
                hb.append(label(h, "hour" + (" today" if i == today else "")))
            ex.set_child(hb)
            body.append(ex)
        if p.get("reviews"):
            body.append(label("REVIEWS", "section"))
            for r in p["reviews"]:
                rb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                rb.add_css_class("review")
                rb.append(label("%s  %s  ·  %s" % ("★" * int(r.get("stars") or 0), r["who"], r["when"]), "r-head"))
                rb.append(label(r["text"], "r-text", wrap=True, lines=6))
                body.append(rb)
        self.open_panel()

    def _set_home(self, p):
        cfg = gmaps.load_config()
        cfg["home"] = {k: p.get(k) for k in ("id", "name", "address", "lat", "lng")}
        gmaps.save_config(cfg)
        self.cfg = cfg
        self.toast("saved as home")

    # --- saved places ---------------------------------------------------------------------
    def _toggle_saved(self, p, btn):
        if saved.is_saved(p):
            saved.remove(p)
            btn.set_label("☆")
            self.toast("removed from saved")
        else:
            saved.add(p)
            btn.set_label("★")
            self.toast("saved")
        self._draw_saved()

    def _show_saved_marks(self, on):
        self.saved_on = on
        self._set_pref("show_saved", on)
        self._draw_saved()

    SAVED_MAX = 120     # 1700+ imported places: only those in view, nearest the centre

    def _draw_saved(self):
        self.saved_all = saved.all_places()
        if not getattr(self, "_saved_hooked", False):
            self._saved_hooked = True
            self.saved_timer = 0
            for prop in ("latitude", "longitude", "zoom-level"):
                self.viewport.connect("notify::" + prop, lambda *_: self._saved_soon())
        self._refresh_saved()

    def _saved_soon(self):
        # 10-16: restart on every move -> update only once the map is still (was: a rebuild
        # every 350 ms in the middle of a pan = stutter)
        if self.saved_timer:
            GLib.source_remove(self.saved_timer)
        self.saved_timer = GLib.timeout_add(400, self._refresh_saved)

    def _refresh_saved(self):
        self.saved_timer = 0
        shown = getattr(self, "_saved_markers", None)
        if shown is None:
            shown = self._saved_markers = {}
        if not self.saved_on or not self.saved_all:
            self.saved_layer.remove_all()
            shown.clear()
            return False
        w, h = self.map.get_width(), self.map.get_height()
        if w <= 0:
            w, h = 412, 915
        lat1, lng1 = self.viewport.widget_coords_to_location(self.map, 0, 0)
        lat2, lng2 = self.viewport.widget_coords_to_location(self.map, w, h)
        la, lb = sorted((lat1, lat2))
        oa, ob = sorted((lng1, lng2))
        c = (self.viewport.get_latitude(), self.viewport.get_longitude())
        vis = [p for p in self.saved_all if la <= p["lat"] <= lb and oa <= p["lng"] <= ob]
        vis.sort(key=lambda p: (p["lat"] - c[0]) ** 2 + (p["lng"] - c[1]) ** 2)
        z = self.viewport.get_zoom_level()
        cap = 30 if z < 12 else (60 if z < 14 else 100)     # zoomed out they overlap anyway
        want = {(p["lat"], p["lng"]): p for p in vis[:cap]}
        for key in [k for k in shown if k not in want]:        # keep markers still in view
            self.saved_layer.remove_marker(shown.pop(key))
        for key, p in want.items():
            if key in shown:
                continue
            m = Shumate.Marker()
            star = Gtk.Label(label="★", css_classes=["saved-pin"], tooltip_text=p.get("name") or "")
            tap = Gtk.GestureClick()        # a label + tap: far lighter than a Button per star
            tap.connect("released", lambda *_a, p=p: self._open_saved(p))
            star.add_controller(tap)
            m.set_child(star)
            m.set_location(p["lat"], p["lng"])
            self.saved_layer.add_marker(m)
            shown[key] = m
        return False

    def _open_saved(self, p):
        p = dict(p)
        if p.get("id"):
            return self.show_place_quick(p)   # fresh hours/photos; note + list stay from the store
        self.show_place(p)

    def show_saved(self):
        d = saved.load()
        body = self._panel_begin("saved")
        if not any(d["lists"].values()):
            body.append(label("NO SAVED PLACES YET", "section"))
            body.append(label("Tap ☆ on a place to save it, or import your Google Maps lists: "
                              "takeout.google.com → select only “Saved” and “Maps (your places)” → "
                              "download the zip to the phone → menu → import from Google.", "p-addr", wrap=True))
            body.append(button("IMPORT TAKEOUT ZIP", css="primary-btn", cb=self._import_dialog))
            return self.open_panel()
        here = (self.fix["lat"], self.fix["lng"]) if self.fix else None
        for name, places in sorted(d["lists"].items(), key=lambda kv: (kv[0] != saved.DEFAULT_LIST, kv[0].lower())):
            if not places:
                continue
            ex = Gtk.Expander(label="%s  ·  %d" % (name, len(places)), expanded=len(d["lists"]) == 1)
            ex.add_css_class("saved-list")
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            if here:
                places = sorted(places, key=lambda p: gmaps.dist(here, (p["lat"], p["lng"])))
            ex.set_child(col)
            ex.connect("notify::expanded", lambda e, _p, col=col, places=places:
                       e.get_expanded() and not col.get_first_child() and self._fill_saved(col, places, 0, here))
            if ex.get_expanded():
                self._fill_saved(col, places, 0, here)
            body.append(ex)
        self.open_panel()

    SAVED_PAGE = 60

    def _fill_saved(self, col, places, start, here):
        """Rows a page at a time: a 1600-place list would freeze the phone if built at once."""
        for p in places[start:start + self.SAVED_PAGE]:
            col.append(self._saved_row(p, here))
        rest = len(places) - start - self.SAVED_PAGE
        if rest > 0:
            more = button("show %d more" % min(rest, self.SAVED_PAGE), css="flat")
            more.connect("clicked", lambda b: (col.remove(b),
                                               self._fill_saved(col, places, start + self.SAVED_PAGE, here)))
            col.append(more)

    def _saved_row(self, p, here):
        b = Gtk.Button()
        b.add_css_class("place-row")
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        col.append(label(p["name"], "p-name"))
        meta = [x for x in (fmt_km(gmaps.dist(here, (p["lat"], p["lng"]))) if here else "",
                            "approx. location" if p.get("approx") else "", p.get("note", "")) if x]
        if meta:
            col.append(label("  ·  ".join(meta), "p-meta"))
        if p.get("address"):
            col.append(label(p["address"], "p-addr"))
        b.set_child(col)
        b.connect("clicked", lambda *_: self._open_saved(p))
        return b

    def _import_dialog(self):
        dlg = Gtk.FileDialog(title="Google Takeout zip")
        f = Gtk.FileFilter(name="Takeout (.zip)")
        f.add_suffix("zip")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dlg.set_filters(store)
        dl = os.path.expanduser("~/Downloads")
        if os.path.isdir(dl):
            dlg.set_initial_folder(Gio.File.new_for_path(dl))

        def done(d, res):
            try:
                path = d.open_finish(res).get_path()
            except GLib.Error:
                return
            self.import_takeout(path)
        dlg.open(self, None, done)

    def import_takeout(self, path):
        body = self._panel_begin("import")
        body.append(label("IMPORTING FROM GOOGLE", "section"))
        status = label(os.path.basename(path), "p-meta")
        body.append(status)
        self.open_panel()
        near = self._near()

        def progress(i, n):
            GLib.idle_add(status.set_text, "%d / %d places" % (i, n))

        def ok(r):
            self._draw_saved()
            self.toast("imported %d places from %d lists" % (r["added"], r["lists"]))
            self.show_saved()
            if r["missed"]:
                self.toast("%d couldn't be located" % r["missed"])
        bg(lambda: saved.import_takeout(path, self.g, near, progress), ok,
           lambda e: (status.set_text(str(e)), self.toast("import failed")))

    # --- outside requests (voiced: notarealmaps --search Q / --navigate Q) --------------
    HOME_WORDS = ("home", "my home", "my house", "house")

    def voice_search(self, q):
        if self.navigating:
            self.end_nav()
        mine = saved.find(q)
        if mine:
            return self._open_saved(mine)
        self._set_entry(q)
        self._search(None, q)

    def voice_navigate(self, q):
        if self.navigating:
            self.end_nav()
        if q.strip().lower() in self.HOME_WORDS:
            home = gmaps.load_config().get("home")
            if not home:
                self.toast("no home yet: open your home address and tap ⌂")
                return
            return self._go_when_located(dict(home, name=home.get("name") or "Home"))
        mine = saved.find(q)
        if mine:
            return self._go_when_located(dict(mine))
        self._set_entry(q)

        def found(places):
            if not places:
                self.toast("nothing found for “%s”" % q)
                return
            self.show_place_quick(places[0])
            self._go_when_located(places[0])
        bg(lambda: self.g.search(q, self._near()), found, lambda e: self.toast(e))

    def _go_when_located(self, place):
        """Directions need a GPS fix (or a manual start); right after launch there may not be one yet."""
        if self.fix or self.start_pt:
            self.show_place(place)
            self.directions(place)
            return False
        self.toast("waiting for GPS…")
        start = time.monotonic()

        def wait():
            if self.fix:
                self.show_place(place)
                self.directions(place)
                return False
            if time.monotonic() - start > 30:
                self.show_place(place)
                self.directions(place)          # shows SET START
                return False
            return True
        GLib.timeout_add(500, wait)
        return False

    def _launch(self, uri):
        Gtk.UriLauncher.new(uri).launch(self, None, None, None)

    def _copy(self, p):
        url = p.get("gmaps") or "https://www.google.com/maps/search/?api=1&query=%s,%s" % (p["lat"], p["lng"])
        self.get_clipboard().set(url)
        self.toast("link copied")

    # --- double-tap-drag zoom ------------------------------------------------------------------
    DZ_TAP_S = 0.35      # second press must come this soon after the first tap...
    DZ_TAP_PX = 60       # ...and this close to it
    DZ_PX_PER_LEVEL = 120

    def _dz_begin(self, g, x, y):
        now = time.monotonic()
        t, tx, ty = self._tap
        self._press = (now, x, y)
        if now - t < self.DZ_TAP_S and math.hypot(x - tx, y - ty) < self.DZ_TAP_PX:
            g.set_state(Gtk.EventSequenceState.CLAIMED)   # no panning / Shumate double-tap
            self._dz = {"x": x, "y": y, "z": self.viewport.get_zoom_level(), "moved": False,
                        "loc": self.viewport.widget_coords_to_location(self.map, x, y)}
            self._tap = (0.0, 0.0, 0.0)
        else:
            self._dz = None

    def _dz_zoom(self, z):
        d = self._dz
        vp = self.viewport
        vp.set_zoom_level(max(vp.get_min_zoom_level(), min(vp.get_max_zoom_level(), z)))
        # keep the spot under the finger where it was
        nx, ny = vp.location_to_widget_coords(self.map, *d["loc"])
        w, h = self.map.get_width(), self.map.get_height()
        lat, lng = vp.widget_coords_to_location(self.map, w / 2 + nx - d["x"], h / 2 + ny - d["y"])
        self.map.center_on(lat, lng)

    def _dz_update(self, g, dx, dy):
        if not self._dz:
            return
        if abs(dy) > 8:
            self._dz["moved"] = True
        if self._dz["moved"]:
            self._dz_zoom(self._dz["z"] + dy / self.DZ_PX_PER_LEVEL)

    def _dz_end(self, g, dx, dy):
        if self._dz:
            if not self._dz["moved"]:   # plain double tap: one level in, like before
                self._dz_zoom(self._dz["z"] + 1)
            self._dz = None
            return
        t, x, y = self._press
        if time.monotonic() - t < 0.3 and abs(dx) + abs(dy) < 12:
            self._tap = (time.monotonic(), x, y)

    def _long_press(self, g, x, y):
        if self._dz:   # holding the second tap of a zoom
            return
        if self.navigating:
            return
        lat, lng = self.viewport.widget_coords_to_location(self.map, x, y)
        self.show_place({"name": "Dropped pin", "address": "%.5f, %.5f" % (lat, lng), "lat": lat, "lng": lng})

    # --- panel helpers -------------------------------------------------------------------
    def _panel_begin(self, kind="list"):
        self.panel_kind = kind
        old = self.panel_body
        self.panel_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.panel_body.add_css_class("panel-body")
        self.panel_sc.set_child(self.panel_body)
        del old
        return self.panel_body

    def open_panel(self):
        self.panel_rev.set_reveal_child(True)
        self.panel_sc.get_vadjustment().set_value(0)
        self._float_fabs()

    def _float_fabs(self):
        """Keep the buttons (my location!) floating just above the bottom panel while it is open."""
        if getattr(self, "_fab_timer", 0):
            return
        def tick():
            if self.navigating:
                self._fab_timer = 0
                return False
            open_ = self.panel_rev.get_reveal_child() or self.panel_rev.get_child_revealed()
            want = max(24, self.panel_rev.get_height() + 12) if open_ else 24
            if self.fab_box.get_margin_bottom() != want:
                self.fab_box.set_margin_bottom(want)
            if not open_:
                self._fab_timer = 0
                return False
            return True
        tick()
        self._fab_timer = GLib.timeout_add(60, tick)

    def close_panel(self):
        self.panel_rev.set_reveal_child(False)
        if not self.navigating:
            self.pin.set_visible(False)
            self._clear_routes()

    # --- routes --------------------------------------------------------------------------
    def _clear_routes(self):
        for lyr in self.alt_layers + self.route_layers:
            self.map.remove_layer(lyr)
        self.alt_layers, self.route_layers = [], []

    def _path(self, pts, color, width, alpha=1.0, dash=None):
        lyr = Shumate.PathLayer.new(self.viewport)
        c = rgba(color)
        c.alpha *= alpha
        lyr.set_stroke_color(c)
        lyr.set_stroke_width(width)
        if dash:
            lyr.set_dash(dash)
        for la, lo in pts:
            lyr.add_node(Shumate.Coordinate.new_full(la, lo))
        self.map.insert_layer_behind(lyr, self.marks)
        return lyr

    def draw_route(self, routes, chosen):
        self._clear_routes()
        for i, r in enumerate(routes):
            if i != chosen:
                self.alt_layers.append(self._path(r["pts"], ALT_COLOR, 6, 0.45))
        r = routes[chosen]
        self.route_layers.append(self._path(r["pts"], "#141a33", 11))
        legs = [st for st in r.get("steps", []) if len(st.get("pts", [])) > 1]
        if r["mode"] in ("TRANSIT", "WALK") and legs:
            # walking = dashed blue, rides = solid in the line's own color (SL green line etc.),
            # so the two can be told apart (10-12: both were the same lavender)
            walk = notareal_theme.hex("blue")
            for st in legs:
                if st["mode"] == "TRANSIT":
                    col = (st.get("transit") or {}).get("color") or notareal_theme.hex("orange")
                    self.route_layers.append(self._path(st["pts"], col, 8))
                else:
                    self.route_layers.append(self._path(st["pts"], walk, 6, dash=[7, 7]))
        elif r.get("traffic"):
            for a, b, speed in r["traffic"]:
                seg = r["pts"][a:b + 1]
                if len(seg) > 1:
                    self.route_layers.append(self._path(seg, TRAFFIC_COLORS.get(speed, ROUTE_COLOR), 7))
        else:
            self.route_layers.append(self._path(r["pts"], ROUTE_COLOR, 7))

    def fit(self, pts, pad_bottom=None):
        if not pts:
            return
        la = [p[0] for p in pts]
        lo = [p[1] for p in pts]
        if pad_bottom is None:
            pad_bottom = min(360, self.map.get_height() * 0.45)   # the bottom panel
        w = max(100, self.map.get_width() - 60)
        top = 80                                                 # search bar
        h = max(100, self.map.get_height() - pad_bottom - top - 60)
        lat_c, lng_c = (min(la) + max(la)) / 2, (min(lo) + max(lo)) / 2

        def ymerc(x):
            x = math.radians(max(-85, min(85, x)))
            return math.log(math.tan(math.pi / 4 + x / 2))
        dx = max(1e-6, (max(lo) - min(lo)) / 360)
        dy = max(1e-6, (ymerc(max(la)) - ymerc(min(la))) / (2 * math.pi))
        z = min(math.log2(w / 256 / dx), math.log2(h / 256 / dy), 17)
        # shift the centre up so the panel at the bottom doesn't hide the route
        span_px = (pad_bottom - top) / 2
        lat_shift = span_px * (360 / (256 * 2 ** z)) * math.cos(math.radians(lat_c))
        self.viewport.set_rotation(0)
        self.compass.set_visible(False)
        self.map.go_to_full_with_duration(lat_c - lat_shift, lng_c, z, 500)

    def origin(self):
        """(lat, lng, heading) the route starts from: the manual start, else the GPS fix, else None."""
        if self.start_pt:
            return self.start_pt["lat"], self.start_pt["lng"], None
        if self.fix:
            return self.fix["lat"], self.fix["lng"], self.fix.get("heading")
        return None

    def _drop_pick_toast(self):
        t, self.pick_toast = getattr(self, "pick_toast", None), None
        if t:
            t.dismiss()

    def set_start(self, p):
        """Use place p as the starting point (GPS can't get a fix, or routing from elsewhere)."""
        self.picking_start = False
        self._drop_pick_toast()
        self.entry.set_placeholder_text("search Google Maps")
        self.start_pt = {"name": p.get("name") or "Dropped pin", "lat": p["lat"], "lng": p["lng"]}
        self._set_entry("")
        self.toast("starting from " + self.start_pt["name"])
        if self.dest and self.dest is not p and self.dest.get("lat") is not None:
            self.show_place(self.dest)
            self.directions(self.dest)

    def clear_start(self):
        self.start_pt = None
        self.toast("starting from GPS" if self.fix else "starting point cleared")
        if self.dest and getattr(self, "panel_kind", "") == "route":
            self.directions(self.dest)

    def pick_start(self):
        """Next searched or long-pressed place becomes the start."""
        self.picking_start = True
        self.close_panel()
        self.entry.set_placeholder_text("search your starting point")
        t = Adw.Toast(title="search, or long-press the map, for your start", timeout=8, button_label="CANCEL")
        t.connect("button-clicked", lambda *_: self._cancel_pick())
        self.toasts.add_toast(t)
        self.pick_toast = t
        self._set_entry("")
        self._show_suggestions()

    def use_gps_start(self):
        """Leave start picking and route from the GPS position again."""
        self.picking_start = False
        self._drop_pick_toast()
        self.entry.set_placeholder_text("search Google Maps")
        self._set_entry("")
        self.start_pt = None
        self.toast("starting from my location" if self.fix else "starting from GPS (waiting for a fix)")
        if self.dest and self.dest.get("lat") is not None:
            self.show_place(self.dest)
            self.directions(self.dest)

    def _cancel_pick(self):
        self.pick_toast = None
        self.picking_start = False
        self.entry.set_placeholder_text("search Google Maps")
        if self.dest:
            self.show_place(self.dest)

    def _from_row(self):
        row = Gtk.Box(spacing=8)
        row.add_css_class("from-row")
        if self.start_pt:      # buttons LEFT (left-handed)
            row.append(button("CHANGE", css="act small", cb=self.pick_start))
            row.append(button("✕", css="act small", tip="Back to GPS", cb=self.clear_start))
            text = "FROM  " + self.start_pt["name"].upper()
        else:
            row.append(button("SET START", css="act small", cb=self.pick_start))
            if not self.fix:
                text = "FROM  —  NO GPS FIX"
            elif (self.fix.get("acc") or 0) > 500:
                text = "FROM  MY LOCATION (ROUGH)"
            else:
                text = "FROM  MY LOCATION"
        lab = label(text, "section", wrap=True)
        lab.set_hexpand(True)
        lab.set_valign(Gtk.Align.CENTER)
        row.append(lab)
        return row

    def directions(self, dest):
        self.dest = dest
        body = self._panel_begin("route")
        head = Gtk.Box(spacing=10)
        head.append(button("✕ END", css="end-btn small", cb=self.end_directions))   # left: left-handed
        to = label("TO  " + dest["name"].upper(), "section")
        to.set_hexpand(True)
        to.set_valign(Gtk.Align.CENTER)
        head.append(to)
        body.append(head)
        body.append(self._from_row())
        modes = Gtk.Box(spacing=4)
        modes.add_css_class("modes")
        group = None
        for m, icon, name in MODES:
            t = Gtk.ToggleButton(label="%s" % icon, tooltip_text=name)
            t.add_css_class("mode")
            if group:
                t.set_group(group)
            else:
                group = t
            t.set_active(m == self.mode)
            t.connect("toggled", lambda tb, m=m: tb.get_active() and self._mode(m))
            modes.append(t)
        body.append(modes)
        self.route_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.append(self.route_box)
        self.open_panel()
        self._fetch_routes()

    def end_directions(self):
        self.route_gen = getattr(self, "route_gen", 0) + 1   # drop a route fetch still in flight
        self.routes = []
        self.close_panel()                                  # also clears the drawn routes + pin

    def _mode(self, m):
        self.mode = m
        self._set_pref("mode", m)
        self._fetch_routes()

    def _fetch_routes(self):
        while (c := self.route_box.get_first_child()):
            self.route_box.remove(c)
        o = self.origin()
        if o is None:
            self.route_box.append(label("GPS can't get a fix. Tap SET START, then search or long-press "
                                        "the map where you are.", "hint", wrap=True))
            self._clear_routes()
            return
        self.route_box.append(Adw.Spinner(height_request=40))
        origin, heading = (o[0], o[1]), o[2]
        mode, dest = self.mode, self.dest
        self.route_gen = getattr(self, "route_gen", 0) + 1
        gen = self.route_gen
        bg(lambda: self.g.routes(origin, dest, mode, heading),
           lambda r: gen == self.route_gen and self._show_routes(r),
           lambda e: gen == self.route_gen and self._route_fail(e))

    def _route_fail(self, e):
        while (c := self.route_box.get_first_child()):
            self.route_box.remove(c)
        self.route_box.append(label(str(e), "hint", wrap=True))

    def _show_routes(self, routes):
        self.routes = routes
        while (c := self.route_box.get_first_child()):
            self.route_box.remove(c)
        if not routes:
            self.route_box.append(label("no route found", "hint"))
            self._clear_routes()
            return
        self.route_i = 0
        for i, r in enumerate(routes):
            self.route_box.append(self._route_row(i, r))
        self.draw_route(routes, 0)
        self.fit(routes[0]["pts"])

    def _route_row(self, i, r):
        row = Gtk.Box(spacing=10)
        row.add_css_class("route-row")
        if i == self.route_i:
            row.add_css_class("chosen")
        if r["mode"] in NAV_MODES:
            go = button("START", css="primary-btn", cb=lambda: self.start_nav(i))
            go.set_valign(Gtk.Align.CENTER)
            row.append(go)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        delay = r["s"] / r["static_s"] if r.get("static_s") else 1
        tcls = "jam" if delay > 1.35 else "slow" if delay > 1.12 else "free"
        top = label(fmt_duration(r["s"]), "r-time " + tcls)
        col.append(top)
        eta = (datetime.datetime.now() + datetime.timedelta(seconds=r["s"])).strftime("%H:%M")
        sub = "%s  ·  arrive %s" % (fmt_km(r["m"]), eta)
        if r.get("via"):
            sub += "  ·  via " + r["via"]
        col.append(label(sub, "r-sub", wrap=True, lines=2))
        if delay > 1.12 and r["mode"] in ("DRIVE", "TWO_WHEELER"):
            col.append(label("+%s in traffic" % fmt_duration(r["s"] - r["static_s"]), "r-traffic " + tcls))
        if "FUEL_EFFICIENT" in r.get("labels", []):
            col.append(label("🌿 fuel efficient", "r-sub"))
        if r["mode"] == "TRANSIT":
            for st in r["steps"]:
                t = st.get("transit")
                if t:
                    # board where + which direction (headsign = the end station shown on the train)
                    ride = "🚆 %s %s  ·  %s" % (t["vehicle"], t["line"], t["dep"])
                    if t.get("from"):
                        ride += "  from " + t["from"]
                    col.append(label(ride, "r-step", wrap=True))
                    if t.get("headsign"):
                        col.append(label("     towards %s" % t["headsign"], "r-dir", wrap=True))
                    col.append(label("     get off at %s  (%d stops)" % (t["to"], t["stops"]),
                                     "r-step", wrap=True))
                elif st["mode"] == "WALK" and st["m"] > 50:
                    col.append(label("🚶 walk %s" % fmt_km(st["m"]), "r-step"))
        row.append(col)
        click = Gtk.GestureClick()
        click.connect("released", lambda *a: self._choose_route(i))
        col.add_controller(click)
        return row

    def _choose_route(self, i):
        self.route_i = i
        self.draw_route(self.routes, i)
        c = self.route_box.get_first_child()
        n = 0
        while c:
            (c.add_css_class if n == i else c.remove_css_class)("chosen")
            c = c.get_next_sibling()
            n += 1

    # --- navigation ----------------------------------------------------------------------
    def start_nav(self, i):
        route = self.routes[i]
        self.navigating = nav.Navigator(route)
        self.follow = True
        self.panel_rev.set_reveal_child(False)
        self.top_box.set_visible(False)
        self.banner_rev.set_reveal_child(True)
        self.navbar_rev.set_reveal_child(True)
        self.fab_box.set_margin_bottom(110)
        self.n_recenter.set_visible(False)
        self.draw_route([route], 0)
        self._sleep_lock()
        self.voice.prepare([s["text"] for s in route["steps"][1:4]])
        first = route["steps"][0]["text"] if route["steps"] else ""
        self.voice.say(first or "Starting route")
        if SIM:
            self.sim = Simulator(route["pts"], self._on_fix, 15.0 if route["mode"] == "DRIVE" else 4.0)
        elif self.fix:
            self._nav_fix(self.fix)
        if self.start_pt and not SIM:
            self.toast("turn-by-turn follows GPS once it gets a fix")

    def _sleep_lock(self):
        # Gtk.Application.inhibit registers nothing under Phosh; hold a logind block lock (sleepd honours it)
        if self.inhibit:
            return
        try:
            r, fds = Gio.bus_get_sync(Gio.BusType.SYSTEM).call_with_unix_fd_list_sync(
                "org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "Inhibit",
                GLib.Variant("(ssss)", ("sleep", "notreal.maps", "Navigating", "block")),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, 2000, None, None)
            self.inhibit = fds.get(r.unpack()[0])
        except GLib.Error as e:
            print("sleep lock failed:", e.message)

    def end_nav(self):
        if self.sim:
            self.sim.stop()
            self.sim = None
        self.navigating = None
        self.voice.stop()
        self.banner_rev.set_reveal_child(False)
        self.navbar_rev.set_reveal_child(False)
        self.top_box.set_visible(True)
        self.fab_box.set_margin_bottom(24)
        self._clear_routes()
        self.pin.set_visible(False)
        self.viewport.set_rotation(0)
        self.compass.set_visible(False)
        if self.inhibit:
            os.close(self.inhibit)
            self.inhibit = 0
        if self.fix:
            self.map.go_to_full_with_duration(self.fix["lat"], self.fix["lng"], 15, 500)

    def _nav_camera(self, fix):
        nv = self.navigating
        sp = fix.get("speed") or 0
        z = 17.2 if sp < 9 else 16.4 if sp < 20 else 15.6
        hd = fix.get("heading")
        if hd is None and nv:
            hd = getattr(self, "_last_route_heading", None)
        lat, lng = fix["lat"], fix["lng"]
        if nv and nv.snapped and getattr(self, "_snap_ok", False):
            lat, lng = nv.snapped
        if hd is not None:
            self.viewport.set_rotation(-math.radians(hd))
            self.compass.set_visible(True)
            # put the puck in the lower third: centre the map ahead of us
            mpp = 156543.03 * math.cos(math.radians(lat)) / (2 ** z)
            ahead = mpp * self.map.get_height() * 0.22
            lat += ahead * math.cos(math.radians(hd)) / 111320
            lng += ahead * math.sin(math.radians(hd)) / (111320 * math.cos(math.radians(lat)))
        self.viewport.set_zoom_level(z)
        self.map.center_on(lat, lng)
        self._puck_angle()

    def _nav_fix(self, fix):
        nv = self.navigating
        st = nv.update(fix["lat"], fix["lng"], fix.get("speed"), fix.get("acc") or 20)
        self._snap_ok = st.get("lateral", 99) < 25
        if st.get("heading") is not None:
            self._last_route_heading = st["heading"]
        if self._snap_ok and nv.snapped:
            self.puck_marker.set_location(*nv.snapped)
        if self.follow:
            self._nav_camera(fix)
        if st.get("event") == "reroute":
            self._reroute(fix)
        if "to_turn" in st:
            nxt = st["next"]
            self.b_icon.set_label("🏁" if not nxt else nav.maneuver_icon(nxt["maneuver"]))
            self.b_dist.set_label(nav.short_distance(st["to_turn"]))
            self.b_text.set_label(nxt["text"] if nxt else "Arrive at %s" % (self.dest or {}).get("name", "destination"))
            then = st.get("then")
            show_then = then and nxt and st["to_turn"] < 600 and then["m"] < 400 and nxt["m"] < 400
            self.b_then.set_label("then %s  %s" % (nav.maneuver_icon(then["maneuver"]), then["text"][:60])
                                  if show_then else "")
            self.b_then.set_visible(bool(show_then))
            eta = datetime.datetime.now() + datetime.timedelta(seconds=st["eta_s"])
            self.n_eta.set_label("%s  ·  %s" % (eta.strftime("%H:%M"), fmt_duration(st["eta_s"])))
            self.n_left.set_label(nav.short_distance(st["remaining"]))
            sp = fix.get("speed")
            self.n_speed.set_label("%d\nkm/h" % round(sp * 3.6) if sp is not None else "")
            k = st["step"]
            steps = nv.route["steps"]
            self.voice.prepare([s["text"] for s in steps[k + 1:k + 3]])
        if st.get("say"):
            self.voice.say(st["say"])
        if st.get("event") == "arrived":
            self.b_icon.set_label("🏁")
            self.b_dist.set_label("arrived")
            GLib.timeout_add_seconds(8, lambda: (self.navigating is nv and self.end_nav(), False)[1])

    def _reroute(self, fix):
        nv = self.navigating
        if getattr(self, "rerouting", False):
            return
        self.rerouting = True
        self.b_text.set_label("rerouting…")
        origin = (fix["lat"], fix["lng"])
        mode, dest, hd = nv.route["mode"], self.dest, fix.get("heading")

        def done(routes):
            self.rerouting = False
            if self.navigating is not nv or not routes:
                return
            nv.set_route(routes[0])
            self.draw_route([routes[0]], 0)
            self.voice.prepare([s["text"] for s in routes[0]["steps"][:3]])

        def fail(e):
            self.rerouting = False
            self.toast(e)
        bg(lambda: self.g.routes(origin, dest, mode, hd, alternatives=False), done, fail)

    # --- closing -------------------------------------------------------------------------
    def save_view(self):
        if getattr(self, "viewport", None):
            self._set_pref("view", [self.viewport.get_latitude(), self.viewport.get_longitude(),
                                    min(self.viewport.get_zoom_level(), 16)])


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        # shared look from ~/theme/theme.css, then this app's style.css (both live-reload)
        notareal_theme.apply(os.path.join(HERE, "style.css"))

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        if not self.win:
            self.win = Window(self)
            self.win.connect("close-request", lambda w: (w.save_view(), False)[1])
        self.win.present()
        for flag, fn in (("--search", self.win.voice_search), ("--navigate", self.win.voice_navigate),
                         ("--import", self.win.import_takeout)):
            if flag in args and args.index(flag) + 1 < len(args):
                q = args[args.index(flag) + 1]
                GLib.timeout_add(400, lambda fn=fn, q=q: (fn(q), False)[1])   # let the window map first
        if "--place" in args and args.index("--place") + 1 < len(args):
            # --place "lat,lng[,name]" (notarealChat location messages)
            parts = args[args.index("--place") + 1].split(",", 2)
            try:
                lat, lng = float(parts[0]), float(parts[1])
            except (ValueError, IndexError):
                return 1
            name = parts[2].strip() if len(parts) > 2 and parts[2].strip() else "Shared location"
            p = {"name": name, "address": "%.5f, %.5f" % (lat, lng), "lat": lat, "lng": lng}
            GLib.timeout_add(400, lambda: (self.win.show_place(p), False)[1])
        return 0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    sys.exit(App().run(sys.argv))
