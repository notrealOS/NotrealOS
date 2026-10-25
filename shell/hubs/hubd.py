"""hubd: hub picker on the way up to the overview.

Swipe up from the bottom bar: a black bar slides up with the thumb, and after ~1 cm its three
hubs (PLAYER / CHAT / MAPS) can be picked. Slide sideways onto one (or rest on it) and lift -> that app opens. Keep going up ->
the hubs fade out and Phosh opens the overview as always.

Phosh still owns the swipe: we only read the touchscreen (no grab) and draw a picture that
takes no touches. A short slow drag that ends sideways makes Phosh snap its overview back
closed by itself (tested 10-25); if a quick flick opened it anyway, we fold it after the pick.
Only while an app is shown (toplevel-watch: not the overview or the lock screen).
While the picker is up we touch $XDG_RUNTIME_DIR/hubs.armed so ~/gestures/appswitch does not
also take the sideways part for a bottom-edge app switch.
Log: $XDG_RUNTIME_DIR/hubd.log
"""
import os
import struct
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("GioUnix", "2.0")
from gi.repository import Gdk, Gio, GioUnix, GLib, GLibUnix, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

sys.path.insert(0, os.path.expanduser("~/theme"))
import notareal_theme as theme  # noqa: E402

TOUCH_DEVICE = "/dev/input/by-path/platform-a94000.spi-event"
TEST_DEVICE = os.environ.get("HUBS_TEST_DEVICE")   # tests: read ~/arc/ftouch.py's fake touchscreen
TOPLEVEL_WATCH = os.path.expanduser("~/overview-widget/toplevel-watch")
FLAG = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "hubs.armed")
HUBS = (("PLAYER", "dev.notareal.Player"),   # left to right; left-handed: the most used on the left
        ("CHAT", "dev.notareal.Chat"),
        ("MAPS", "dev.notareal.Maps"))
W, H = 1080, 2400        # touchscreen px (= physical screen px)
START_ZONE = 70          # a touch starting this close to the bottom is Phosh's home bar
ARM = 160                # 1 cm up (~400 ppi) -> show the hubs
CANCEL = 280             # this far above the arm point -> it's an overview swipe after all
SIDE = 50                # sideways travel that starts picking...
DWELL_S = 0.30           # ...or resting this long in the band
RAISE_POLL_MS = 100
RAISE_TIMEOUT = 8.0
SETTLE_MS = 250          # let Phosh snap back (or not) before we act
SLIDE_FROM = 12          # px of upward travel before the bar starts sliding in (taps, sideways swipes)
BAR_H = 112              # logical px; the bar is fully in at ARM

EVENT = struct.Struct("llHHi")
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
BTN_TOUCH = 0x14a
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x2f, 0x35, 0x36, 0x39
CLEAN_ENV = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}

CSS = """
window.hubs { background: none; }
.hubs-bar { background: #000; border-radius: 18px 18px 0 0; box-shadow: 0 -6px 18px rgba(0,0,0,0.35); }
.hub { padding: 10px 6px 6px; transition: transform 90ms; }
.hub label { font-family: var(--nr-font); font-size: 10pt; letter-spacing: 2px; color: var(--nr-fg); }
.hub .dot { min-width: 18px; min-height: 3px; border-radius: 2px; background: transparent; }
.hub.hot { transform: scale(1.15); }
.hub.hot label { color: var(--nr-accent-light); }
.hub.hot .dot { background: var(--nr-accent-light); }
"""


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def toplevels():
    try:
        out = subprocess.run(["wlrctl", "toplevel", "list"], capture_output=True, text=True,
                             timeout=2, env=CLEAN_ENV).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [ln.split(":", 1)[0].strip() for ln in out.splitlines() if ":" in ln]


class Hubs:
    def __init__(self, app):
        self.active = "none"          # toplevel-watch: app id in front, or "none"
        self.state = "idle"           # idle / track / armed / off (rest of this touch ignored)
        self.slot = 0
        self.ids = {}                 # slot -> tracking id
        self.x = self.y = 0
        self.down = False
        self.hot = None

        theme.apply()
        theme.add_css(CSS)
        self.win = Gtk.Window(application=app)
        self.win.add_css_class("hubs")
        LayerShell.init_for_window(self.win)
        LayerShell.set_namespace(self.win, "hubs")
        LayerShell.set_layer(self.win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.NONE)
        LayerShell.set_exclusive_zone(self.win, -1)
        for e in (LayerShell.Edge.BOTTOM, LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(self.win, e, True)
        self.win.set_can_target(False)

        # the bar is drawn at its full height and slid with a CSS translateY (the surface clips it)
        self.row = Gtk.Box(spacing=0, homogeneous=True, valign=Gtk.Align.END)
        self.row.add_css_class("hubs-bar")
        self.row.set_size_request(-1, BAR_H)
        self.row.set_can_target(False)
        self.tiles = []
        for name, app_id in HUBS:
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5,
                           halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            tile.add_css_class("hub")
            icon = Gtk.Image.new_from_icon_name(app_id)
            icon.set_pixel_size(48)
            tile.append(icon)
            tile.append(Gtk.Label(label=name))
            dot = Gtk.Box(halign=Gtk.Align.CENTER)
            dot.add_css_class("dot")
            tile.append(dot)
            self.row.append(tile)
            self.tiles.append(tile)
        self.win.set_child(self.row)
        self.slide = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.slide,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 100)
        self.offset = None

        self._watch_toplevels()
        self._watch_touch()
        log("hubd: ready")

    # ---- what's in front ----
    def _watch_toplevels(self):
        proc = Gio.Subprocess.new([TOPLEVEL_WATCH], Gio.SubprocessFlags.STDOUT_PIPE)
        stream = Gio.DataInputStream.new(proc.get_stdout_pipe())

        def on_line(s, res):
            line, _n = s.read_line_finish_utf8(res)
            if line is None:
                log("hubd: toplevel-watch ended")
                GLib.timeout_add_seconds(2, lambda: self._watch_toplevels() and False)
                return
            parts = line.split(None, 1)
            if parts and parts[0] == "active":
                self.active = parts[1] if len(parts) > 1 else "none"
            elif parts and parts[0] == "none":
                self.active = "none"
            s.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)

        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)

    # ---- touch ----
    def _watch_touch(self):
        path = TOUCH_DEVICE
        if TEST_DEVICE:
            path = self._find_device(TEST_DEVICE)
            if not path:
                GLib.timeout_add(100, lambda: self._watch_touch() and False)
                return
            log("hubd: test device", path)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except PermissionError:   # a new uinput node: udev has not opened it up yet
            GLib.timeout_add(100, lambda: self._watch_touch() and False)
            return

        def on_input(_fd, _cond):
            try:
                data = os.read(fd, EVENT.size * 64)
            except BlockingIOError:
                return True
            except OSError as e:   # the device went away (only the test device does)
                log("hubd: touch input gone:", e)
                os.close(fd)
                if TEST_DEVICE:
                    GLib.timeout_add(100, lambda: self._watch_touch() and False)
                return False
            for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _s, _us, etype, code, value = EVENT.unpack_from(data, off)
                if etype == EV_ABS:
                    if code == ABS_MT_SLOT:
                        self.slot = value
                    elif code == ABS_MT_TRACKING_ID:
                        if value < 0:
                            self.ids.pop(self.slot, None)
                        else:
                            self.ids[self.slot] = value
                    elif self.slot == 0 and code == ABS_MT_POSITION_X:
                        self.x = value
                    elif self.slot == 0 and code == ABS_MT_POSITION_Y:
                        self.y = value
                elif etype == EV_KEY and code == BTN_TOUCH:
                    self.down = bool(value)
                elif etype == EV_SYN and code == 0:
                    self._frame()
            return True

        GLibUnix.fd_add_full(GLib.PRIORITY_HIGH, fd, GLib.IOCondition.IN, on_input)

    @staticmethod
    def _find_device(name):
        try:
            blocks = open("/proc/bus/input/devices").read().split("\n\n")
        except OSError:
            return None
        for b in blocks:
            if 'Name="%s"' % name in b:
                for h in b.split("Handlers=")[-1].split():
                    if h.startswith("event"):
                        return "/dev/input/" + h.split()[0]
        return None

    def _frame(self):
        now = time.monotonic()
        if not self.down:
            if self.state == "armed":
                self._release()
            elif self.state == "track" and self.offset is not None:
                self._hide()
            self.state = "idle"
            return
        if len(self.ids) > 1 and self.state != "off":   # a second finger: not ours
            self._cancel()
            return
        if self.state == "idle":
            ok = self.y >= H - START_ZONE and self.active != "none"
            self.state = "track" if ok else "off"
            self.x0, self.y0 = self.x, self.y
        elif self.state == "track":
            self._slide((self.y0 - self.y - SLIDE_FROM) / (ARM - SLIDE_FROM))
            if self.y0 - self.y >= ARM:
                self.state = "armed"
                self.ax, self.ay, self.at = self.x, self.y, now
                self.hot = None
                self._flag()
                self._show()
        elif self.state == "armed":
            if self.y < self.ay - CANCEL:
                self._cancel()
                return
            if abs(self.x - self.ax) >= SIDE or now - self.at >= DWELL_S:
                self._set_hot(min(len(HUBS) - 1, max(0, self.x * len(HUBS) // W)))

    def _cancel(self):
        if self.state in ("armed", "track") and self.offset is not None:
            self._hide()
        self.state = "off"

    def _release(self):
        hot = self.hot
        self._flag()
        self._hide()
        if hot is not None:
            log("hubd: pick", HUBS[hot][0])
            GLib.timeout_add(SETTLE_MS, self._open, HUBS[hot][1])

    @staticmethod
    def _flag():
        try:
            with open(FLAG, "w"):
                pass
            os.utime(FLAG)
        except OSError:
            pass

    # ---- picture ----
    def _slide(self, p):
        """p = 0 (hidden below the screen edge) .. 1 (fully in), following the thumb."""
        if p <= 0 and self.offset is None:
            return
        off = int(BAR_H * (1 - min(1.0, max(0.0, p))))
        if off == self.offset:
            return
        self.slide.load_from_string(".hubs-bar { transform: translateY(%dpx); }" % off)
        if self.offset is None:
            for t in self.tiles:
                t.remove_css_class("hot")
            self.win.present()
        self.offset = off

    def _show(self):
        self._slide(1.0)

    def _hide(self):
        self.hot = None
        self.offset = None
        self.win.set_visible(False)
        self.slide.load_from_string(".hubs-bar { transform: translateY(%dpx); }" % BAR_H)

    def _set_hot(self, i):
        if i == self.hot:
            return
        if self.hot is not None:
            self.tiles[self.hot].remove_css_class("hot")
        self.hot = i
        self.tiles[i].add_css_class("hot")

    # ---- open ----
    def _open(self, app_id):
        overview = self.active == "none"     # a fast flick may have opened it after all
        if app_id in toplevels():
            self._focus(app_id, overview)
            return False
        try:
            ctx = Gdk.Display.get_default().get_app_launch_context()
            GioUnix.DesktopAppInfo.new(app_id + ".desktop").launch([], ctx)
        except (GLib.Error, TypeError) as e:
            log("hubd: launch failed:", app_id, e)
            return False
        deadline = time.monotonic() + RAISE_TIMEOUT

        def poll():
            if app_id in toplevels():
                # not self.active here: it reads "none" for a moment while the new window maps
                self._focus(app_id, overview)
                return False
            return time.monotonic() < deadline

        GLib.timeout_add(RAISE_POLL_MS, poll)
        return False

    @staticmethod
    def _focus(app_id, fold):
        subprocess.Popen(["wlrctl", "toplevel", "focus", "app_id:" + app_id],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=CLEAN_ENV)
        if fold:   # focusing alone leaves Phosh's overview open on top (see ~/arc/joystick.py)
            GLib.timeout_add(150, lambda: subprocess.Popen(
                ["wtype", "-M", "logo", "a", "-m", "logo"], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=CLEAN_ENV) and False)


def main():
    app = Gtk.Application(application_id="dev.notareal.Hubs", flags=Gio.ApplicationFlags.NON_UNIQUE)
    keep = []

    def activate(a):
        a.hold()
        keep.append(Hubs(a))

    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    main()
