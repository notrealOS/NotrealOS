#!/usr/bin/env python3
"""overview-widget: a tappable vapor-style widget on top of the Phosh overview.

A layer-shell surface (OVERLAY layer, anchored to the top edge below the top panel) that
is only shown while the Phosh overview is open. Phosh does not export the overview
state (org.gnome.Shell.OverviewActive is always false), so it is derived:
  - toplevel-watch reports "none" when no window is activated: overview, lock
    screen or notification drawer
  - org.gnome.ScreenSaver ActiveChanged tells the lock screen apart
  - the drawer is told apart by the touch that opened it starting in the top panel
    (read straight from the touchscreen's evdev node; the user is in group input)

Shows: weather (Open-Meteo, no API key), now playing (MPRIS, with prev/play/next),
notarealShell reminders and latest note (tap to open notarealShell).
~/.config/gtk-3.0/gtk.css pushes the Phosh overview down by HEIGHT to make room.
"""

import datetime
import fcntl
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.request

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gtk  # noqa: E402

sys.path.insert(0, os.path.expanduser("~/voice"))
import iotas_notes  # noqa: E402  notes live in the Iotas app
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402
import notareal_theme as theme  # noqa: E402  ~/theme (colors by name, live switch)

# launched helpers must not inherit the gtk4-layer-shell preload
os.environ.pop("LD_PRELOAD", None)

HERE = os.path.dirname(os.path.abspath(__file__))
TOPLEVEL_WATCH = os.path.join(HERE, "toplevel-watch")
DOCTOR = os.path.expanduser("~/doctor/nrdoctor.py")   # SYS CHECK under the face
POWER_MENU_MAX_S = 60
CIRCLE_MAX_S = 600                  # circled forgot to say it closed (crash): show again after this
TOUCH_DEVICE = "/dev/input/by-path/platform-a94000.spi-event"
PHANTOM_DIR = os.path.expanduser("~/.local/share/phantomshell")
REMINDERS_FILE = os.path.join(PHANTOM_DIR, "reminders.json")
NOTES_FILE = os.path.join(PHANTOM_DIR, "notes.json")

sys.path.insert(0, os.path.expanduser("~/arc"))
try:
    from arc import Usage                      # noqa: E402  app ranking (toplevel activations)
    from joystick import Joystick              # noqa: E402  replaces Phosh's app grid
except Exception as e:  # never let the launcher take the widget down
    print("overview-widget: no joystick:", e, flush=True)
    Joystick = Usage = None

HEIGHT = 236        # logical px; keep in sync with .phosh-overview margin in gtk.css
# The drawer is closed by an upward swipe of at least DRAWER_CLOSE of the height, or a quick
# flick (DRAWER_FLICK in < FLICK_S) starting on its bottom handle (below DRAWER_HANDLE).
# Shorter swipes are taken as scrolling the notification list (10-12: the widget came back
# over the open drawer while scrolling notifications).
DRAWER_CLOSE = 0.25
DRAWER_FLICK = 0.08
FLICK_S = 0.25
DRAWER_HANDLE = 0.75
# A close is only a guess (Phosh exposes no drawer state). If another quick upward flick comes
# within DRAWER_RECHECK_S of a guessed close, the user is still trying to close the drawer: hide
# again so the next swipe reaches it (10-12: widget + joystick got stuck over the open drawer).
DRAWER_RECHECK_S = 15
TOP_PANEL = 0.03    # a touch starting above this fraction of the height is the top panel (bar = 32/1200)
CITY = "STOCKHOLM"
MAX_REMINDERS = 4   # rows shown on the widget (+ the latest note)
LAT, LON = 59.33, 18.07   # Stockholm until GeoClue has a position
WEATHER_URL = ("https://api.open-meteo.com/v1/forecast?latitude=%.3f&longitude=%.3f"
               "&current=temperature_2m,weather_code,wind_speed_10m"
               "&daily=temperature_2m_max,temperature_2m_min&timezone=auto"
               "&forecast_days=1&wind_speed_unit=ms")
GEO_DESKTOP_ID = "dev.notareal.Maps"   # GeoClue already allows this id (notarealMaps)
CAL_EVERY = 5 * 60
CAL_DAYS = 30
UPOWER_DEV = "/org/freedesktop/UPower/devices/DisplayDevice"
WEATHER_EVERY = 15 * 60
DEBUG = bool(os.environ.get("OVW_DEBUG"))
VISIBILITY_SETTLE_MS = 200
MPRIS_PREFIX = "org.mpris.MediaPlayer2."

PHANTOM = ("org.notarealinux.PhantomShell", "/org/notarealinux/PhantomShell",
           "org.notarealinux.PhantomShell")

CSS = """
window.ovw { background: none; font-family: var(--nr-font-code); color: var(--nr-fg-bright); }
.retro { background: alpha(var(--nr-bg), 0.86); border: 2px solid var(--nr-accent); border-radius: 7px;
         box-shadow: 0 5px 0 alpha(var(--nr-glow), 0.55); }
.ghost { background: none; border: none; }  /* offset ghost frame removed 10-11 (was lavender fill + 2px var(--nr-violet) border) */
.titlebar { background: var(--nr-gradient);
            border-radius: 5px 5px 0 0; padding: 3px 6px; }
.accent { background: var(--nr-gradient);
          border-radius: 5px 5px 0 0; min-height: 4px; }
.title { color: var(--nr-bg); font-size: 9pt; font-weight: bold; letter-spacing: 1px; }
.body { padding: 8px 10px; }
.rule { background: alpha(var(--nr-accent), 0.35); min-height: 1px; }
.temp { font-size: 22pt; font-weight: bold; color: var(--nr-white); }
.cond { font-size: 9.5pt; font-weight: bold; color: var(--nr-cyan); }
.dim { font-size: 8pt; color: var(--nr-accent); }
.song { font-size: 9.5pt; font-weight: bold; color: var(--nr-white); }
.artist { font-size: 8pt; color: var(--nr-pink); }
.item { font-size: 8.5pt; color: var(--nr-fg); }
.when { font-size: 8.5pt; font-weight: bold; color: var(--nr-mint); }
.evwhen { font-size: 8.5pt; font-weight: bold; color: var(--nr-pink); }
.batt { font-size: 8pt; color: var(--nr-mint); }
.batt.low { color: var(--nr-hot-pink); }
.sys { font-size: 7pt; font-weight: bold; letter-spacing: 1px; color: var(--nr-accent); }
.sys.ok { color: var(--nr-mint); }
.sys.fixed { color: var(--nr-pink); }
.sys.bad { color: var(--nr-hot-pink); }
button.chip { background: var(--nr-mint); color: var(--nr-bg); border: none; border-radius: 4px;
              box-shadow: 0 3px 0 alpha(var(--nr-mint), 0.6); font-family: var(--nr-font-code);
              font-size: 10pt; font-weight: bold; padding: 0 8px; min-height: 30px; min-width: 34px; }
button.chip-main { background: var(--nr-pink); box-shadow: 0 3px 0 alpha(var(--nr-pink), 0.6); }
button.chip:active { box-shadow: 0 1px 0 alpha(var(--nr-mint), 0.6); margin-top: 2px; }
button.flat-row { background: none; border: none; box-shadow: none; padding: 0; }
@keyframes appear { from { opacity: 0; transform: translate(0, -16px); } to { opacity: 1; transform: none; } }
.appear { animation: appear 220ms ease-out; }
"""

WMO = {0: "CLEAR", 1: "MOSTLY CLEAR", 2: "PARTLY CLOUDY", 3: "OVERCAST", 45: "FOG", 48: "FOG",
       51: "DRIZZLE", 53: "DRIZZLE", 55: "DRIZZLE", 56: "ICY DRIZZLE", 57: "ICY DRIZZLE",
       61: "LIGHT RAIN", 63: "RAIN", 65: "HEAVY RAIN", 66: "ICY RAIN", 67: "ICY RAIN",
       71: "LIGHT SNOW", 73: "SNOW", 75: "HEAVY SNOW", 77: "SNOW GRAINS",
       80: "SHOWERS", 81: "SHOWERS", 82: "HEAVY SHOWERS", 85: "SNOW SHOWERS", 86: "SNOW SHOWERS",
       95: "THUNDER", 96: "THUNDER + HAIL", 99: "THUNDER + HAIL"}

# evdev
EV_KEY, EV_ABS = 0x01, 0x03
BTN_TOUCH = 0x14a
ABS_MT_POSITION_Y = 0x36
EVENT = struct.Struct("llHHi")
EVIOCGABS_Y = (2 << 30) | (24 << 16) | (ord("E") << 8) | (0x40 + ABS_MT_POSITION_Y)


# --- pixel face: the phone's health (nrdoctor --check: down -> sad, minor -> meh) -----
FACE = [  # 12x12; # = face, . = empty; eyes and mouth are drawn on top per mood
    "....####....",
    "..########..",
    ".##########.",
    ".##########.",
    "############",
    "############",
    "############",
    "############",
    ".##########.",
    ".##########.",
    "..########..",
    "....####....",
]
EYES = [(4, 3), (5, 3), (4, 8), (5, 8)]            # (row, col)
MOUTHS = {
    "happy": [(7, 2), (7, 9), (8, 3), (8, 8), (9, 4), (9, 5), (9, 6), (9, 7)],
    "meh": [(8, 4), (8, 5), (8, 6), (8, 7)],
    "sad": [(8, 4), (8, 5), (8, 6), (8, 7), (9, 3), (9, 8)],
}
MOOD_COLORS = {"happy": "#f7a1e8", "meh": "#c5a0ff", "sad": "#6fd6f2"}
HEALTH_EVERY = 300   # s between health checks; since 10-16 these auto-fix (nrdoctor --auto)


def mood_for(res):
    return "sad" if res["failed"] or res["manual"] else "meh" if res.get("minor") else "happy"


class PixelFace(Gtk.Widget):
    PX = 5   # logical px per pixel -> 60x60

    def __init__(self):
        super().__init__(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.set_size_request(12 * self.PX, 12 * self.PX)
        self.mood = "meh"
        self.blink = False
        GLib.timeout_add(4200, self._blink)

    def set_mood(self, mood):
        if mood != self.mood:
            self.mood = mood
            self.queue_draw()

    def _blink(self):
        if self.get_mapped():           # only while the overview shows it
            self.blink = True
            self.queue_draw()
            GLib.timeout_add(140, self._unblink)
        return True

    def _unblink(self):
        self.blink = False
        self.queue_draw()
        return False

    def do_snapshot(self, snap):
        px = self.PX
        col = Gdk.RGBA()

        def put(r, c, spec, alpha=1.0):
            col.parse(theme.resolve(spec))
            col.alpha = alpha
            snap.append_color(col, Graphene.Rect().init(c * px, r * px, px, px))
        for r, row in enumerate(FACE):            # 1-px-ish shadow, then the face
            for c, ch in enumerate(row):
                if ch == "#" and r + 1 < 12:
                    snap.append_color(theme.color("glow", 0.55),
                                      Graphene.Rect().init(c * px + 2, r * px + 3, px, px))
        for r, row in enumerate(FACE):
            for c, ch in enumerate(row):
                if ch == "#":
                    put(r, c, MOOD_COLORS[self.mood])
        dark = "#1c2340"
        for r, c in EYES:
            if self.blink and r == 4:
                continue                          # top half of the eyes gone = blink
            put(r, c, dark)
        for r, c in MOUTHS[self.mood]:
            put(r, c, dark)
        if self.mood == "happy":                  # rosy cheeks
            put(6, 1, "#ff8fb3", 0.8)
            put(6, 10, "#ff8fb3", 0.8)
        elif self.mood == "sad":                  # a tear
            put(6, 2, "#ffffff", 0.9)
            put(7, 2, "#ffffff", 0.7)


def label(text="", css=None, **kw):
    lbl = Gtk.Label(label=text, xalign=0, **kw)
    if css:
        lbl.add_css_class(css)
    return lbl


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def next_event():
    """The next calendar event (all enabled EDS calendars), or None. Runs in a worker thread."""
    gi.require_version("ECal", "2.0")
    gi.require_version("EDataServer", "1.2")
    from gi.repository import ECal, EDataServer
    reg = EDataServer.SourceRegistry.new_sync(None)
    now = int(time.time())
    found = []
    for src in reg.list_enabled(EDataServer.SOURCE_EXTENSION_CALENDAR):
        try:
            client = ECal.Client.connect_sync(src, ECal.ClientSourceType.EVENTS, 5, None)
        except GLib.Error:
            continue

        def cb(comp, start, stop, *_a):
            def t(x):
                return x.as_timet_with_zone(x.get_timezone()) if x.get_timezone() else x.as_timet()
            allday = start.is_date()
            if allday:   # date-only: local midnight, not UTC
                s = time.mktime((start.get_year(), start.get_month(), start.get_day(), 0, 0, 0, 0, 0, -1))
                e = s + 86400
            else:
                s, e = t(start), t(stop)
            if e > now:
                found.append({"start": s, "end": e, "allday": allday, "title": comp.get_summary() or "(no title)",
                              "where": (comp.get_location() or "").split(",")[0].strip()})
            return True
        client.generate_instances_sync(now - 86400, now + CAL_DAYS * 86400, None, cb)
    # soonest day first; on the same day a timed event beats an all-day one (birthdays, holidays)
    day = lambda e: datetime.date.fromtimestamp(e["start"])
    return min(found, key=lambda e: (day(e), e["allday"], e["start"])) if found else None


def event_when(ev):
    d = datetime.datetime.fromtimestamp(ev["start"])
    today = datetime.date.today()
    day = ("TODAY" if d.date() == today else "TMRW" if d.date() == today + datetime.timedelta(days=1)
           else d.strftime("%a %d").upper())
    if ev["allday"]:
        return day
    return d.strftime("%H:%M") if d.date() == today else f"{day} {d:%H:%M}"


class OverviewWidget(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.notarealinux.OverviewWidget",
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None
        self.next_event = None   # set before _panel() builds the lists
        self.no_window_active = False
        self.locked = False
        self.drawer_open = False
        self.drawer_guess_t = 0.0  # monotonic time of the last swipe-guessed drawer close
        self.touch_from_top = False
        self.touch_y = None
        self.touch_last_y = None
        self.player = None
        self._pick_gen = 0
        self._settle_id = 0
        self.power_menu = False
        self.phantom_summoned = False
        self._power_menu_id = 0
        self.circle_up = False
        self._circle_id = 0

    # --- UI --------------------------------------------------------------

    def _themed(self):
        """Widgets that draw colors in code (redrawn after a theme switch)."""
        return [getattr(self, "face", None), getattr(getattr(self, "joy", None), "view", None)]

    def do_activate(self):
        if self.window:
            return
        theme.add_css(CSS)
        # code-drawn parts (face, joystick) redraw on a theme switch; CSS updates by itself
        theme.on_change(lambda: [w.queue_draw() for w in self._themed() if w])

        win = Gtk.ApplicationWindow(application=self, default_height=HEIGHT)
        win.add_css_class("ovw")
        LayerShell.init_for_window(win)
        LayerShell.set_namespace(win, "overview-widget")
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)  # Phosh home covers the TOP layer
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)
        for edge in (LayerShell.Edge.TOP, LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(win, edge, True)
        self.window = win

        win.set_child(self._panel())
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.usage = Usage() if Usage else None
        self.joy = Joystick(self, self.usage) if Joystick else None
        self._last_active = None
        self._watch_toplevels()
        self._watch_lock()
        self._watch_phantom_summon()
        self._watch_touch()
        self._watch_shade()
        self._watch_players()
        self._watch_phantom_files()
        self.lat, self.lon = LAT, LON
        self._watch_location()
        self._fetch_weather()
        GLib.timeout_add_seconds(WEATHER_EVERY, self._fetch_weather)
        self._watch_battery()
        self._update_ram()
        GLib.timeout_add_seconds(10, self._update_ram)
        self._fetch_calendar()
        GLib.timeout_add_seconds(CAL_EVERY, self._fetch_calendar)
        GLib.timeout_add_seconds(30, self._refresh_lists)  # relative times, due reminders
        # first check only looks: autostart daemons may still be starting at login
        GLib.timeout_add_seconds(20, lambda: (self._check_health(auto=False), False)[1])
        GLib.timeout_add_seconds(HEALTH_EVERY, self._check_health)
        doc = Gio.SimpleAction.new("doctor", None)  # debug: same as tapping the face
        doc.connect("activate", lambda *_: self._run_doctor())
        self.add_action(doc)
        act = Gio.SimpleAction.new("power-menu", None)
        act.connect("activate", lambda *_: self._on_power_menu())
        self.add_action(act)
        circ = Gio.SimpleAction.new("circle", GLib.VariantType.new("b"))
        circ.connect("activate", lambda _a, v: self._on_circle(v.unpack()))
        self.add_action(circ)
        if self.joy:  # debug: launch a desktop id like the joystick does
            t = Gio.SimpleAction.new("test-launch", GLib.VariantType.new("s"))
            t.connect("activate", lambda _a, v: self.joy.launch(Gio.DesktopAppInfo.new(v.unpack())))
            self.add_action(t)
            ram = Gio.SimpleAction.new("ram", None)  # debug: same as tapping the battery line
            ram.connect("activate", lambda *_: self.joy.toggle_ram())
            self.add_action(ram)
        self._update_visibility()

    def _panel(self):
        outer = Gtk.Overlay(margin_top=6, margin_start=12, margin_end=12)
        ghost = Gtk.Box(margin_start=10, margin_top=10)
        ghost.add_css_class("ghost")
        outer.set_child(ghost)
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_end=10, margin_bottom=10)
        frame.add_css_class("retro")
        outer.add_overlay(frame)
        outer.set_measure_overlay(frame, True)
        self.panel = outer

        # the old title bar (city // temp, clock) is gone to make room for reminders;
        # a thin gradient strip keeps the look. title/clock still exist, unshown.
        bar = Gtk.Box()
        bar.add_css_class("accent")
        frame.append(bar)
        self.title = label(f"{CITY} // --", "title")
        self.clock = label("", "title")

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.add_css_class("body")
        frame.append(body)

        # weather + now playing side by side
        top = Gtk.Box(spacing=12)
        wx = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.temp = label("--°", "temp")
        self.cond = label("LOADING", "cond")
        self.range = label("", "dim")
        self.batt = label("", "batt")
        self.ram_free = ""
        temp_row = Gtk.Box(spacing=8)
        temp_row.append(self.temp)
        temp_row.append(self._theme_button())
        wx.append(temp_row)
        for w in (self.cond, self.range):
            wx.append(w)
        batt_btn = Gtk.Button(child=self.batt, halign=Gtk.Align.START)  # tap: RAM page
        batt_btn.add_css_class("flat-row")
        batt_btn.connect("clicked", lambda *a: self.joy and self.joy.toggle_ram())
        wx.append(batt_btn)
        top.append(wx)

        # the face doubles as the CHECK button: tap -> ~/doctor/nrdoctor.py restarts what's down
        self.face = PixelFace()
        face_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        face_box.append(self.face)
        self.sys_status = label("SYS CHECK", "sys")
        self.sys_status.set_xalign(0.5)
        face_box.append(self.sys_status)
        face_btn = Gtk.Button(child=face_box, hexpand=True, halign=Gtk.Align.CENTER)
        face_btn.add_css_class("flat-row")
        face_btn.connect("clicked", lambda *a: self._run_doctor())
        top.append(face_btn)
        self._doctor_busy = False
        self._doctor_reset = 0
        self.health = None                # last nrdoctor result (sets the face's mood)

        music = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER,
                        halign=Gtk.Align.END, width_request=168)
        self.song = label("NOTHING PLAYING", "song", ellipsize=3, max_width_chars=1, hexpand=True)
        self.artist = label("", "artist", ellipsize=3, max_width_chars=1, hexpand=True)
        self.song.set_xalign(1)
        self.artist.set_xalign(1)
        music.append(self.song)
        music.append(self.artist)
        controls = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        self.btn_play = None
        for text, method, main in (("⏮", "Previous", False), ("⏯", "PlayPause", True),
                                   ("⏭", "Next", False)):
            btn = Gtk.Button(label=text)
            btn.add_css_class("chip")
            if main:
                btn.add_css_class("chip-main")
                self.btn_play = btn
            btn.connect("clicked", lambda _b, m=method: self._player_call(m))
            controls.append(btn)
        music.append(controls)
        top.append(music)
        body.append(top)

        rule = Gtk.Box()
        rule.add_css_class("rule")
        body.append(rule)

        # reminders + latest note; tap opens PhantomShell
        lists_btn = Gtk.Button()
        lists_btn.add_css_class("flat-row")
        lists_btn.connect("clicked", lambda *a: self._phantom("Summon"))
        self.lists = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        lists_btn.set_child(self.lists)
        body.append(lists_btn)
        self._refresh_lists()
        return outer

    # --- theme picker (next to the temperature, left side for the left thumb) ---------------

    def _theme_button(self):
        btn = Gtk.MenuButton(label="◐", valign=Gtk.Align.CENTER, tooltip_text="Theme")
        btn.add_css_class("chip")
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        pop.set_child(box)
        btn.set_popover(pop)

        def fill(*_):
            while (c := box.get_first_child()):
                box.remove(c)
            d = os.path.expanduser("~/theme/themes")
            names = sorted(f[:-4] for f in os.listdir(d)
                           if f.endswith(".css") and f.count(".") == 1)
            try:
                with open(os.path.expanduser("~/theme/active")) as f:
                    cur = f.read().strip()
            except OSError:
                cur = ""
            for n in names:
                b = Gtk.Button(label=("● " if n == cur else "  ") + n.upper())
                b.add_css_class("chip")
                b.connect("clicked", lambda _b, n=n: (pop.popdown(), self._set_theme(n)))
                box.append(b)
        pop.connect("show", fill)
        return btn

    def _set_theme(self, name):
        try:
            subprocess.Popen([os.path.expanduser("~/theme/nrtheme"), name], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            print("theme switch failed:", e, flush=True)

    # --- system check ---------------------------------------------------

    def _set_sys(self, text, css=None):
        for c in ("ok", "fixed", "bad"):
            self.sys_status.remove_css_class(c)
        if css:
            self.sys_status.add_css_class(css)
        self.sys_status.set_text(text)

    def _doctor(self, args, done):
        def worker():
            try:
                out = subprocess.run(["python3", DOCTOR, "--json", *args], capture_output=True,
                                     text=True, timeout=120).stdout
                res = json.loads(out)
            except Exception as e:
                print("overview-widget: doctor failed:", e, flush=True)
                res = None
            GLib.idle_add(done, res)
        threading.Thread(target=worker, daemon=True).start()

    def _check_health(self, auto=True):
        if not self._doctor_busy:
            self._doctor_busy = True
            self._doctor(["--auto"] if auto else ["--check"], self._health_done)
        return True

    def _health_done(self, res):
        self._doctor_busy = False
        if res is not None:
            self.health = res
            self.face.set_mood(mood_for(res))
            if not self._doctor_reset:
                self._doctor_idle()
            if res["fixed"]:
                subprocess.Popen(["notify-send", "-a", "System check", "Fixed automatically",
                                  "\n".join(res["fixed"])])
        return False

    def _run_doctor(self):
        if self._doctor_busy:
            return
        self._doctor_busy = True
        self._set_sys("CHECKING…")
        if self._doctor_reset:
            GLib.source_remove(self._doctor_reset)
            self._doctor_reset = 0
        self._doctor([], self._doctor_done)

    def _doctor_done(self, res):
        self._doctor_busy = False
        if res is None:
            self._set_sys("CHECK FAILED", "bad")
        else:
            self.health = res
            self.face.set_mood(mood_for(res))
            bad = res["failed"] + res["manual"]
            if bad:
                self._set_sys(f"{len(bad)} DOWN", "bad")
            elif res["fixed"]:
                self._set_sys(f"FIXED {len(res['fixed'])}", "fixed")
            else:
                self._set_sys("ALL OK", "ok")
            lines = ([f"fixed: {n}" for n in res["fixed"]] + [f"FAILED: {n}" for n in res["failed"]]
                     + [f"down (needs you): {n}" for n in res["manual"]]
                     + [f"minor: {n}" for n in res.get("minor", [])])
            if lines:
                subprocess.Popen(["notify-send", "-a", "System check", "System check",
                                  "\n".join(lines)])
        self._doctor_reset = GLib.timeout_add_seconds(60, self._doctor_idle)
        return False

    def _doctor_idle(self):
        """Resting label = the last health check (tap the face to fix)."""
        self._doctor_reset = 0
        res = self.health
        if res is None:
            self._set_sys("SYS CHECK")
        elif res["failed"] or res["manual"]:
            n = len(res["failed"]) + len(res["manual"])
            self._set_sys(f"{n} ISSUE{'S' if n > 1 else ''} · TAP", "bad")
        elif res.get("minor"):
            self._set_sys("MINOR · TAP", "fixed")
        else:
            self._set_sys("ALL GOOD", "ok")
        return False

    # --- visibility -----------------------------------------------------

    def _update_visibility(self):
        # Several inputs can flip within a few ms (e.g. PhantomShell dismissing itself while
        # Phosh opens the overview). Mapping and unmapping a layer surface that fast leaves
        # GTK and phoc disagreeing (widget gone from the overview, or stuck over an app),
        # so only act once the decision has been stable for VISIBILITY_SETTLE_MS.
        if self._settle_id:
            GLib.source_remove(self._settle_id)
        self._settle_id = GLib.timeout_add(VISIBILITY_SETTLE_MS, self._apply_visibility)

    def _apply_visibility(self):
        self._settle_id = 0
        show = (self.no_window_active and not self.locked and not self.drawer_open and not self.power_menu
                and not self.circle_up)
        if DEBUG:
            print(time.strftime("%H:%M:%S"), f"none={self.no_window_active} locked={self.locked} drawer={self.drawer_open}"
                  f" -> show={show} (visible={self.window.get_visible()}"
                  f" size={self.window.get_width()}x{self.window.get_height()})", flush=True)
        if show == self.window.get_visible():
            return False
        if show:
            self.clock.set_label(datetime.datetime.now().strftime("%H:%M"))
            self._update_ram()
            self._refresh_lists()
            self.panel.remove_css_class("appear")
            self.panel.add_css_class("appear")
            self.window.present()
            if self.joy and not self.phantom_summoned:
                self.joy.show()
        else:
            self.window.set_visible(False)
            if self.joy:
                self.joy.hide()
        return False

    def _watch_toplevels(self):
        proc = Gio.Subprocess.new([TOPLEVEL_WATCH], Gio.SubprocessFlags.STDOUT_PIPE)
        stream = Gio.DataInputStream.new(proc.get_stdout_pipe())

        def on_line(src, res):
            line, _ = src.read_line_finish_utf8(res)
            if line is None:  # watcher died (compositor restart?): stay hidden
                self.no_window_active = False
                self._update_visibility()
                return
            if DEBUG:
                print(time.strftime("%H:%M:%S"), "watcher:", line.strip(), flush=True)
            self.no_window_active = line.strip() == "none"
            if self.usage and line.startswith("active "):
                app_id = line.strip()[7:]
                if app_id != self._last_active:
                    try:  # never let usage tracking kill the watcher loop
                        did = self.usage.desktop_for(app_id)
                        if did:
                            self.usage.bump(did)
                    except Exception as e:
                        print("usage:", app_id, e, file=sys.stderr, flush=True)
                self._last_active = app_id
            if not self.no_window_active:
                self.drawer_open = False
                self.drawer_guess_t = 0.0
                self._clear_power_menu()
            self._update_visibility()
            src.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)

        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)
        self.toplevel_proc = proc

    # crtd activates "power-menu" when a long power press reaches Phosh. Phosh's power menu
    # takes focus from the app, so toplevel-watch says "none" just like the overview; keep
    # hidden until an app is active again (or POWER_MENU_MAX_S passes).
    def _on_power_menu(self):
        if self.no_window_active and self.window.get_visible():
            return  # already on the overview: nothing to hide
        self.power_menu = True
        if self._power_menu_id:
            GLib.source_remove(self._power_menu_id)
        self._power_menu_id = GLib.timeout_add_seconds(POWER_MENU_MAX_S, self._clear_power_menu)
        self._update_visibility()

    def _clear_power_menu(self):
        if self._power_menu_id:
            GLib.source_remove(self._power_menu_id)
            self._power_menu_id = 0
        if self.power_menu:
            self.power_menu = False
            self._update_visibility()
        return False

    # circled (circle to search) activates "circle" true/false. Its ASK entry takes the keyboard,
    # so toplevel-watch says "none" and we'd pop up over it (joystick on OVERLAY covering the sheet).
    def _on_circle(self, up):
        self.circle_up = up
        if self._circle_id:
            GLib.source_remove(self._circle_id)
            self._circle_id = 0
        if up:
            self._circle_id = GLib.timeout_add_seconds(CIRCLE_MAX_S, lambda: (self._on_circle(False), False)[1])
        if DEBUG:
            print(time.strftime("%H:%M:%S"), "circle:", up, flush=True)
        self._update_visibility()

    # notarealShell is summoned over the overview (toplevel-watch says "none"), and the
    # joystick would cover its app suggestion chips: hide just the joystick meanwhile.
    def _watch_phantom_summon(self):
        def on_signal(conn, sender, path, iface, signal, params):
            self.phantom_summoned = params.unpack()[0]
            if DEBUG:
                print(time.strftime("%H:%M:%S"), "phantom summoned:", self.phantom_summoned, flush=True)
            if self.joy:
                if self.phantom_summoned:
                    self.joy.hide()
                elif self.window.get_visible():
                    self.joy.show()
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.signal_subscribe(None, "org.notarealinux.PhantomShell", "SummonedChanged",
                             "/org/notarealinux/PhantomShell", None, Gio.DBusSignalFlags.NONE, on_signal)

    def _watch_lock(self):
        self._session = None

        def on_active(_c, _s, _p, _i, _sig, params):
            # Phosh's ScreenSaver state is wrong (it can say "not active" while the lock
            # screen is up, e.g. when a notification wakes the display), so it only counts
            # when logind's LockedHint isn't available.
            if self._session is None:
                self.locked = params.unpack()[0]
                self._update_visibility()

        self.bus.signal_subscribe("org.gnome.ScreenSaver", "org.gnome.ScreenSaver", "ActiveChanged",
                                  "/org/gnome/ScreenSaver", None, Gio.DBusSignalFlags.NONE, on_active)
        try:
            res = self.bus.call_sync("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver",
                                     "org.gnome.ScreenSaver", "GetActive", None, None,
                                     Gio.DBusCallFlags.NONE, 2000, None)
            if self._session is None:
                self.locked = res.unpack()[0]
        except GLib.Error:
            pass
        # Phosh never answers GetActive/ActiveChanged truthfully, but it does set logind's
        # LockedHint on the session, so follow that.
        try:
            sysbus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            path = sysbus.call_sync("org.freedesktop.login1", "/org/freedesktop/login1",
                                    "org.freedesktop.login1.Manager", "GetSessionByPID",
                                    GLib.Variant("(u)", (os.getpid(),)), None,
                                    Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
            session = Gio.DBusProxy.new_sync(sysbus, Gio.DBusProxyFlags.NONE, None,
                                                   "org.freedesktop.login1", path,
                                                   "org.freedesktop.login1.Session", None)

            def on_props(proxy, changed, _inv):
                hint = changed.unpack().get("LockedHint")
                if hint is not None:
                    self.locked = hint
                    self._update_visibility()

            session.connect("g-properties-changed", on_props)
            hint = session.get_cached_property("LockedHint")
            if hint is not None:
                self.locked = hint.unpack()
                self._session = session
        except GLib.Error as e:
            print("overview-widget: no logind LockedHint:", e, flush=True)

    def _watch_touch(self):
        """Notice the notification drawer: it is opened by a touch starting in the top panel."""
        try:
            fd = os.open(TOUCH_DEVICE, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            print("overview-widget: no touch input:", e)
            return
        absinfo = bytearray(24)
        fcntl.ioctl(fd, EVIOCGABS_Y, absinfo)
        _value, ymin, ymax = struct.unpack_from("iii", absinfo)
        top_limit = ymin + (ymax - ymin) * TOP_PANEL
        span = ymax - ymin
        handle_y = ymin + span * DRAWER_HANDLE
        touch_t = [0.0]

        def on_input(_fd, _cond):
            try:
                data = os.read(fd, EVENT.size * 64)
            except BlockingIOError:
                return True
            for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _s, _us, etype, code, value = EVENT.unpack_from(data, off)
                if etype == EV_KEY and code == BTN_TOUCH:
                    if self.joy:  # freeze the idle pulse while a finger swipes the overview
                        self.joy.view.outside_touch = bool(value)
                    if not value:
                        up = (0 if self.touch_y is None or self.touch_last_y is None
                              else self.touch_y - self.touch_last_y)
                        quick = _s + _us / 1e6 - touch_t[0] < FLICK_S
                        swiped_up = (up > span * DRAWER_CLOSE
                                     or (quick and up > span * DRAWER_FLICK and self.touch_y > handle_y))
                        # taps and notification scrolling inside the drawer leave it open;
                        # a long upward swipe (or a flick from the handle) closes it
                        if self.drawer_open and DEBUG and not self.touch_from_top and up > 0:
                            print(time.strftime("%H:%M:%S"), f"drawer swipe up={up / span:.2f}"
                                  f" quick={quick} from={(self.touch_y - ymin) / span:.2f} close={swiped_up}", flush=True)
                        if getattr(self, "shade_real", False):
                            pass   # Phosh's own log says when the shade really closes (_watch_shade)
                        elif not self.touch_from_top and self.drawer_open and swiped_up:
                            self.drawer_open = False
                            self.drawer_guess_t = time.monotonic()
                            GLib.timeout_add(350, self._update_visibility_once)
                        elif (not self.touch_from_top and not self.drawer_open and quick
                              and up > span * DRAWER_FLICK
                              and time.monotonic() - self.drawer_guess_t < DRAWER_RECHECK_S):
                            if DEBUG:
                                print(time.strftime("%H:%M:%S"), "drawer still open? flick after a guessed close"
                                      " -> hide again", flush=True)
                            self.drawer_open = True
                            self.drawer_guess_t = 0.0
                            self._update_visibility()
                        if self.touch_from_top:
                            self.top_touch_t = time.monotonic()
                            self.last_top_pull = (0 if self.touch_y is None or self.touch_last_y is None
                                                  else (self.touch_last_y - self.touch_y) / span)
                        self.touch_from_top = False
                        self.touch_y = None  # the first Y of the next touch decides
                        self.touch_last_y = None
                elif etype == EV_ABS and code == ABS_MT_POSITION_Y:
                    self.touch_last_y = value
                    if self.touch_y is not None:
                        continue
                    self.touch_y = value
                    touch_t[0] = _s + _us / 1e6
                    if value < top_limit:
                        self.touch_from_top = True
                        self.top_touch_t = time.monotonic()
                        self.drawer_open = True
                        self._update_visibility()
                        self._dismiss_osk()
                        if getattr(self, "shade_real", False):
                            # just a tap on the bar? the log says nothing then: undo after 1.5 s
                            GLib.timeout_add(2500, self._shade_confirm)
            return True

        GLib.io_add_watch(fd, GLib.PRIORITY_DEFAULT, GLib.IO_IN, on_input)

    # --- the shade's REAL state, from Phosh's debug log (10-17) ---------------------------------
    # Phosh exposes no drawer state, so this widget used to guess it from swipes - and guessed
    # "closed" too early (joystick over the open shade). With the phosh-top-panel log domain on,
    # phosh writes "top-panel configured: WxH" and "on_drag_state_changed: ... visible: X" to its
    # stderr (= phoc.log via ~/.phoshdebug): X == top-bar means folded, arrow means open/dragged.
    PHOC_LOG = os.path.expanduser("~/crashlog/phoc.log")

    def _watch_shade(self):
        self.shade_real = False
        self.shade_open_real = False
        self.panel_h = None
        try:   # the panel is as tall as the screen; phosh logs it again on every rotation
            mons = Gdk.Display.get_default().get_monitors()
            if mons.get_n_items():
                self.panel_h = mons.get_item(0).get_geometry().height
        except Exception:
            pass
        try:
            Gio.bus_get_sync(Gio.BusType.SESSION).call_sync(
                "mobi.phosh.Shell.DebugControl", "/mobi/phosh/Shell/DebugControl",
                "org.freedesktop.DBus.Properties", "Set",
                GLib.Variant("(ssv)", ("mobi.phosh.Shell.DebugControl", "LogDomains",
                                       GLib.Variant("as", ["phosh-top-panel"]))),
                None, Gio.DBusCallFlags.NONE, 2000, None)
            self._shade_f = open(self.PHOC_LOG, errors="replace")
            self._shade_f.seek(0, 2)
        except (GLib.Error, OSError) as e:
            print("overview-widget: no real shade state (guessing):", e, flush=True)
            return
        self._shade_ino = os.fstat(self._shade_f.fileno()).st_ino
        GLib.timeout_add(150, self._read_shade)

    def _read_shade(self):
        try:   # phoc-wrap rotates phoc.log at a new login: reopen when the file changes
            if os.stat(self.PHOC_LOG).st_ino != self._shade_ino:
                self._shade_f = open(self.PHOC_LOG, errors="replace")
                self._shade_ino = os.fstat(self._shade_f.fileno()).st_ino
        except OSError:
            return True
        for line in self._shade_f.readlines():
            if "phosh-top-panel" not in line:
                continue
            m = re.search(r"top-panel configured: (\d+)x(\d+)", line)
            if m:
                self.panel_h = int(m.group(2))
                self.closed_h = self.panel_h - 32
                continue
            # "Drag Handle: N" depends on the shade's content (902 both folded and unfolded, 10-17),
            # so it can't tell open from closed. The drag state can: phosh logs
            # "on_drag_state_changed: ... visible: top-bar" only when the shade is folded and
            # "visible: arrow" while it is dragged or unfolded.
            m = re.search(r"on_drag_state_changed: .*visible: ([\w-]+)", line)
            if m:
                open_now = m.group(1) != "top-bar"
                self.shade_real = True
                if open_now != self.shade_open_real or open_now != self.drawer_open:
                    self.shade_open_real = open_now
                    self.drawer_open = open_now
                    if not open_now:
                        self._dismiss_osk("Restore")
                    if DEBUG:
                        print(time.strftime("%H:%M:%S"), f"shade (phosh log): {'OPEN' if open_now else 'closed'}"
                              f" visible={m.group(1)}", flush=True)
                    self._update_visibility()
        return True

    def _shade_confirm(self):
        # 2.5 s after a top-bar touch and Phosh never reported the shade open
        if self.drawer_open and not self.shade_open_real:
            self.drawer_open = False
            self._update_visibility()
            # a real PULL that failed = the keyboard was still up when Phosh started the drag
            # (it hides a moment too late): keep it hidden so the next pull works. Only a short
            # tap on the bar brings it back here; otherwise the real shade close does (10-17).
            if getattr(self, "last_top_pull", 0) < 0.12:
                self._dismiss_osk("Restore")
        return False

    def _dismiss_osk(self, method="Dismiss"):
        """A pull from the top bar: get the swipe keyboard out of the way first. Its exclusive
        zone kept Phosh's shade from unfolding (10-16: 'opened' but never drawn).
        method="Restore" brings it back once the shade is closed again."""
        try:
            Gio.bus_get_sync(Gio.BusType.SESSION).call(
                "sm.puri.OSK0", "/sm/puri/OSK0", "sm.puri.OSK0", method, None, None,
                Gio.DBusCallFlags.NO_AUTO_START, 500, None, None, None)
        except GLib.Error:
            pass

    def _update_visibility_once(self):
        self._update_visibility()
        return False

    # --- weather --------------------------------------------------------

    def _fetch_weather(self):
        def worker():
            try:
                with urllib.request.urlopen(WEATHER_URL % (self.lat, self.lon), timeout=15) as r:
                    data = json.load(r)
                GLib.idle_add(self._show_weather, data)
            except Exception as e:  # offline etc.: keep the last values
                print("overview-widget: weather:", e)
        threading.Thread(target=worker, daemon=True).start()
        return True

    def _show_weather(self, data):
        cur, day = data["current"], data["daily"]
        t = round(cur["temperature_2m"])
        self.temp.set_label(f"{t}°")
        self.cond.set_label(WMO.get(cur["weather_code"], "?"))
        self.range.set_label(f"H {round(day['temperature_2m_max'][0])}°  L {round(day['temperature_2m_min'][0])}°"
                             f"  {round(cur['wind_speed_10m'])} M/S")
        self.title.set_label(f"{CITY} // {t}°")
        return False

    # --- location (GeoClue, city-level: wifi/cell, no GPS power) -----------

    def _watch_location(self):
        try:
            gi.require_version("Geoclue", "2.0")
            from gi.repository import Geoclue
        except (ValueError, ImportError) as e:
            print("overview-widget: no geoclue:", e)
            return

        def ready(_src, res):
            try:
                self.geo = Geoclue.Simple.new_finish(res)
            except GLib.Error as e:
                print("overview-widget: geoclue:", e.message)
                return
            self.geo.connect("notify::location", lambda *a: self._on_location())
            self._on_location()
        Geoclue.Simple.new(GEO_DESKTOP_ID, Geoclue.AccuracyLevel.CITY, None, ready)

    def _on_location(self):
        loc = self.geo.get_location()
        if not loc:
            return
        lat, lon = loc.props.latitude, loc.props.longitude
        moved = abs(lat - self.lat) + abs(lon - self.lon) > 0.05   # ~5 km: worth a new forecast
        self.lat, self.lon = lat, lon
        if moved:
            self._fetch_weather()

    # --- battery (UPower) ---------------------------------------------------

    def _watch_battery(self):
        def ready(_src, res):
            try:
                self.upower = Gio.DBusProxy.new_for_bus_finish(res)
            except GLib.Error as e:
                print("overview-widget: upower:", e.message)
                return
            self.upower.connect("g-properties-changed", lambda *a: self._show_battery())
            self._show_battery()
        Gio.DBusProxy.new_for_bus(Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None, "org.freedesktop.UPower",
                                  UPOWER_DEV, "org.freedesktop.UPower.Device", None, ready)

    def _update_ram(self):
        try:
            with open("/proc/meminfo") as f:
                avail = next(int(l.split()[1]) for l in f if l.startswith("MemAvailable:"))
            self.ram_free = f"{avail / 1048576:.1f}G"
        except (OSError, StopIteration, ValueError):
            self.ram_free = ""
        if getattr(self, "upower", None):
            self._show_battery()
        return True

    def _show_battery(self):
        def prop(name, default=0):
            v = self.upower.get_cached_property(name)
            return v.unpack() if v is not None else default
        pct, state = round(prop("Percentage")), prop("State")
        empty, full = prop("TimeToEmpty"), prop("TimeToFull")
        dur = lambda s: f"{s // 3600}H {s % 3600 // 60:02d}M"
        if state in (1, 4):      # charging / fully charged
            text = f"⚡ {pct}%" + (f" · FULL IN {dur(full)}" if state == 1 and full > 0 else "")
        else:
            text = f"▮ {pct}%" + (f" · {dur(empty)} LEFT" if empty > 0 else "")
        self.batt.set_label(text + (f" · RAM {self.ram_free}" if self.ram_free else ""))
        (self.batt.add_css_class if pct <= 20 and state not in (1, 4) else self.batt.remove_css_class)("low")

    # --- calendar (Evolution Data Server: the phone's Google calendars) -----

    def _fetch_calendar(self):
        def worker():
            try:
                ev = next_event()
            except Exception as e:  # noqa: BLE001  EDS down etc.: keep the last one
                print("overview-widget: calendar:", e)
                return
            GLib.idle_add(self._set_event, ev)
        threading.Thread(target=worker, daemon=True).start()
        return True

    def _set_event(self, ev):
        if ev != self.next_event:
            self.next_event = ev
            self._refresh_lists()
        return False

    # --- music (MPRIS) ----------------------------------------------------

    def _watch_players(self):
        # Only MPRIS names matter; NameOwnerChanged fires for every bus client.
        def on_owner(_c, _s, _p, _i, _sig, params):
            if params.unpack()[0].startswith(MPRIS_PREFIX):
                self._pick_player()

        self.bus.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
                                  "/org/freedesktop/DBus", None, Gio.DBusSignalFlags.NONE, on_owner)
        self._pick_player()

    # Everything here is async: a player that never answers (the Epiphany web app's
    # MPRIS did this) used to block the main loop for the 25 s D-Bus default per call,
    # leaving the widget stuck over apps or missing from the overview.
    def _pick_player(self):
        self._pick_gen += 1
        gen = self._pick_gen

        def on_names(bus, res):
            try:
                names = bus.call_finish(res).unpack()[0]
            except GLib.Error:
                return
            players = [n for n in names if n.startswith(MPRIS_PREFIX)]
            if not players:
                self._set_player(None)
            for name in players:
                Gio.DBusProxy.new(self.bus, Gio.DBusProxyFlags.NONE, None, name, "/org/mpris/MediaPlayer2",
                                  "org.mpris.MediaPlayer2.Player", None, on_proxy, None)

        def on_proxy(_src, res, _data):
            try:
                proxy = Gio.DBusProxy.new_finish(res)
            except GLib.Error:
                return
            if gen != self._pick_gen:
                return
            status = proxy.get_cached_property("PlaybackStatus")
            playing = bool(status and status.unpack() == "Playing")
            cur = self.player
            cur_status = cur.get_cached_property("PlaybackStatus") if cur is not None else None
            cur_alive = cur is not None and cur.get_name_owner() is not None
            cur_playing = bool(cur_status and cur_status.unpack() == "Playing")
            if not cur_alive or (playing and not cur_playing):
                self._set_player(proxy)

        self.bus.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames",
                      None, None, Gio.DBusCallFlags.NONE, 2000, None, on_names)

    def _set_player(self, proxy):
        if self.player is not None and proxy is not None and self.player.get_name() == proxy.get_name():
            return
        self.player = proxy
        if proxy is not None:
            proxy.connect("g-properties-changed", lambda *a: self._show_player())
        self._show_player()

    def _show_player(self):
        p = self.player
        meta = p.get_cached_property("Metadata").unpack() if p and p.get_cached_property("Metadata") else {}
        title = meta.get("xesam:title") or ""
        artist = ", ".join(meta.get("xesam:artist") or [])
        status = p.get_cached_property("PlaybackStatus").unpack() if p and p.get_cached_property("PlaybackStatus") else ""
        self.song.set_label(title.upper() if title else "NOTHING PLAYING")
        self.artist.set_label(artist if title else "")
        self.btn_play.set_label("⏸" if status == "Playing" else "▶")

    def _player_call(self, method):
        if self.player is None:
            return
        self.player.call(method, None, Gio.DBusCallFlags.NONE, 2000, None, None)

    # --- PhantomShell reminders / notes ---------------------------------------

    def _watch_phantom_files(self):
        self.monitors = []
        for path in (REMINDERS_FILE, NOTES_FILE, iotas_notes.DB):
            mon = Gio.File.new_for_path(path).monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
            mon.connect("changed", lambda *a: self._refresh_lists())
            self.monitors.append(mon)

    def _refresh_lists(self):
        while (child := self.lists.get_first_child()) is not None:
            self.lists.remove(child)
        now = time.time()
        ev = self.next_event if self.next_event and self.next_event["end"] > now else None
        reminders = sorted((r for r in load_json(REMINDERS_FILE) if r.get("due", 0) > now - 3600),
                           key=lambda r: r["due"])[:MAX_REMINDERS - (1 if ev else 0)]
        if ev:
            self.lists.append(self._row(event_when(ev), ev["title"] + (" · " + ev["where"] if ev["where"] else ""),
                                        "evwhen"))
        if iotas_notes.available():
            notes = [{"text": n["title"] or n["text"]} for n in iotas_notes.recent(1)]
        else:
            notes = load_json(NOTES_FILE)[-1:]
        for r in reminders:
            due = datetime.datetime.fromtimestamp(r["due"])
            when = due.strftime("%H:%M") if due.date() == datetime.date.today() else due.strftime("%d %b %H:%M")
            self.lists.append(self._row(when.upper(), r["text"]))
        for n in notes:
            self.lists.append(self._row("NOTE", n["text"]))
        if not reminders and not notes:
            self.lists.append(self._row("--", "NO REMINDERS. TAP TO OPEN NOTAREALSHELL"))
        return True

    def _row(self, when, text, when_css="when"):
        row = Gtk.Box(spacing=8)
        row.append(label(when, when_css))
        row.append(label(text, "item", ellipsize=3, hexpand=True, max_width_chars=1))
        return row

    def _phantom(self, method):
        self.bus.call(PHANTOM[0], PHANTOM[1], PHANTOM[2], method, None, None,
                      Gio.DBusCallFlags.NONE, 2000, None, None)


if __name__ == "__main__":
    OverviewWidget().run(None)
