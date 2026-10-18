#!/usr/bin/env python3
"""crtd - CRT power-off/power-on animation and a double-press-power camera shortcut.

Takes over the power key (EVIOCGRAB on pmic_pwrkey) and passes presses on to Phosh
through a uinput clone, so Phosh still does the actual blanking, waking and locking:
  short press, screen on   CRT power-off animation, then the press goes to Phosh (blank)
  short press, screen off  the press goes to Phosh (wake), then the power-on animation
  double press             notarealCamera; when locked, the phone is unlocked once the camera
                           window is up, so it opens straight over the lock screen, and
                           locked again as soon as the camera is left
  long press               passed straight through (Phosh power menu)
The overlay is a fullscreen layer-shell surface on the OVERLAY layer. It stays black
while the screen is off, so whatever wakes the screen (key, call, idle blank) the
power-on animation plays.

/dev/uinput must be writable (udev rule from install.sh). Without it the power key is
left alone and only the power-on animation runs.
"""

import fcntl
import getpass
import os
import struct
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, Gio, GLib, GLibUnix, Graphene, Gsk, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

DOUBLE = 0.40        # s between release and next press for a double press
LONG_MS = 500        # held longer than this = long press, passed through
OFF_MS = 450         # power-off animation
ON_MS = 520          # power-on animation
WAKE_HOLD_MS = 180   # black after the screen reports on, before the power-on animation
CAMERA_APP = "dev.notareal.Camera"   # notarealCamera since 10-16 (was org.gnome.Snapshot)

EV_SYN, EV_KEY, KEY_POWER = 0, 1, 116
EVENT = struct.Struct("qqHHi")
EVIOCGRAB = 0x40044590
UI_SET_EVBIT, UI_SET_KEYBIT = 0x40045564, 0x40045565
UI_DEV_SETUP, UI_DEV_CREATE = 0x405C5503, 0x5501

LINE = (0.94, 0.98, 1.0)     # phosphor white, slightly blue
GLOW = (0.47, 0.90, 1.0)     # vapor cyan


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


PRESSLOG = os.path.expanduser("~/crashlog/power.log")


def presslog(msg):
    """10-17: every physical power-key press, kept across reboots (phantom-press hunt)."""
    try:
        with open(PRESSLOG, "a") as f:
            f.write(time.strftime("%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def rgba(rgb, alpha):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = rgb[0], rgb[1], rgb[2], alpha
    return c


def rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


def clamp(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


BLACK = rgba((0, 0, 0), 1)


class Screen(Gtk.Widget):
    """Draws the current animation frame. mode: off / black / on."""

    def __init__(self):
        super().__init__()
        self.mode = "black"
        self.p = 0.0

    def line(self, s, cx, cy, w, h, alpha):
        if alpha <= 0:
            return
        r = rect(cx - w / 2, cy - h / 2, w, h)
        outline = Gsk.RoundedRect().init_from_rect(r, h / 2)
        s.append_outset_shadow(outline, rgba(GLOW, 0.9 * alpha), 0, 0, 2, 18)
        s.append_outset_shadow(outline, rgba(LINE, alpha), 0, 0, 0, 4)
        s.append_color(rgba(LINE, alpha), r)

    def scanlines(self, s, y0, y1, W, alpha):
        if alpha <= 0.01:
            return
        c = rgba((0, 0, 0), alpha)
        y = y0 - (y0 % 4)
        while y < y1:
            s.append_color(c, rect(0, max(y, y0), W, 1.5))
            y += 4

    def bars(self, s, W, H, gap):
        cy = H / 2
        s.append_color(BLACK, rect(0, 0, W, cy - gap / 2))
        s.append_color(BLACK, rect(0, cy + gap / 2, W, H - cy - gap / 2 + 1))

    def do_snapshot(self, s):
        W, H = self.get_width(), self.get_height()
        cx, cy, p = W / 2, H / 2, self.p
        if self.mode == "black":
            s.append_color(BLACK, rect(0, 0, W, H))
        elif self.mode == "off":
            a = clamp(p / 0.5)          # picture collapses onto the centre line
            b = clamp((p - 0.5) / 0.3)  # the line shrinks to a dot
            c = clamp((p - 0.8) / 0.2)  # the dot fades out
            if a < 1:
                gap = max(H * (1 - a) ** 2, 3)
                self.bars(s, W, H, gap)
                self.scanlines(s, cy - gap / 2, cy + gap / 2, W, 0.35 * a)
                s.append_color(rgba(LINE, 0.9 * a ** 1.5), rect(0, cy - gap / 2, W, gap))
            else:
                s.append_color(BLACK, rect(0, 0, W, H))
                w = max(W * (1 - b) ** 3, 5)
                self.line(s, cx, cy, w, 3 if b < 1 else 5, 1 - c)
        elif self.mode == "on":
            a = clamp(p / 0.25)          # dot stretches into a line
            b = clamp((p - 0.25) / 0.5)  # the line opens up into the picture
            c = clamp((p - 0.6) / 0.4)   # scanlines and flash fade
            if b <= 0:
                s.append_color(BLACK, rect(0, 0, W, H))
                self.line(s, cx, cy, max(W * (1 - (1 - a) ** 3), 5), 3, min(1, 3 * a))
            else:
                gap = max(H * (1 - (1 - b) ** 3), 3)
                self.bars(s, W, H, gap)
                s.append_color(rgba(LINE, 0.85 * (1 - b) ** 2), rect(0, cy - gap / 2, W, gap))
                self.scanlines(s, cy - gap / 2, cy + gap / 2, W, 0.3 * (1 - c))
                if b < 1:  # bright edges where the picture is opening
                    for y in (cy - gap / 2, cy + gap / 2):
                        self.line(s, cx, y, W, 2, 0.7 * (1 - b))


def touch_through(win):
    """Empty input region on the overlay's surface (no pycairo here: libcairo + libgtk via ctypes)."""
    try:
        import ctypes
        surface = win.get_surface()
        get_ptr = ctypes.pythonapi.PyCapsule_GetPointer
        get_ptr.restype, get_ptr.argtypes = ctypes.c_void_p, [ctypes.py_object, ctypes.c_char_p]
        sptr = get_ptr(surface.__gpointer__, None)
        cairo = ctypes.CDLL("libcairo.so.2")
        cairo.cairo_region_create.restype = ctypes.c_void_p
        region = cairo.cairo_region_create()                 # empty = no input anywhere
        gtk = ctypes.CDLL("libgtk-4.so.1")
        gtk.gdk_surface_set_input_region.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gtk.gdk_surface_set_input_region(sptr, region)
        cairo.cairo_region_destroy.argtypes = [ctypes.c_void_p]
        cairo.cairo_region_destroy(region)
        log("overlay is touch-through")
    except Exception as e:      # never break the power key over this
        log("touch-through failed:", e)


class Overlay:
    def __init__(self, app):
        self.win = Gtk.ApplicationWindow(application=app)
        self.win.add_css_class("crt")
        LayerShell.init_for_window(self.win)
        LayerShell.set_namespace(self.win, "crt")
        LayerShell.set_layer(self.win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.NONE)
        LayerShell.set_exclusive_zone(self.win, -1)
        for e in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM, LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(self.win, e, True)
        # 10-17: the animation must not take touches: a wake animation mapping under a finger
        # cancelled Phosh's shade drag and left the shade stuck (no pulls until a lock).
        self.win.connect("realize", lambda w: touch_through(w))
        css = Gtk.CssProvider()
        css.load_from_data(b"window.crt { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, 800)
        self.screen = Screen()
        self.win.set_child(self.screen)
        self.tick = None
        self.done = None

    @property
    def shown(self):
        return self.win.get_visible()

    def stop(self):
        if self.tick is not None:
            self.screen.remove_tick_callback(self.tick)
            self.tick = None
        self.done = None

    def black(self):
        self.stop()
        self.screen.mode, self.screen.p = "black", 0
        self.screen.queue_draw()
        self.win.present()

    def hide(self):
        self.stop()
        self.win.set_visible(False)

    def play(self, mode, ms, done):
        self.stop()
        self.screen.mode, self.screen.p = mode, 0
        self.done = done
        self.win.present()
        self.screen.queue_draw()
        t0 = []

        def step(widget, clock):
            now = clock.get_frame_time()
            if not t0:
                t0.append(now)
            self.screen.p = min(1.0, (now - t0[0]) / 1000 / ms)
            self.screen.queue_draw()
            if self.screen.p >= 1:
                self.tick = None
                cb, self.done = self.done, None
                if cb:
                    GLib.idle_add(cb)
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        self.tick = self.screen.add_tick_callback(step)
        gen = self.gen = getattr(self, "gen", 0) + 1

        # Frame callbacks stall when the panel isn't presenting (seen after an aborted idle
        # suspend: Phosh said "on", the panel was dark, the off animation never ended and the
        # power press never reached Phosh for minutes). Finish on a timer if the frames don't.
        def watchdog():
            if self.gen == gen and self.done is not None:
                log("animation", mode, "stalled, finishing it")
                if self.tick is not None:
                    self.screen.remove_tick_callback(self.tick)
                    self.tick = None
                self.screen.p = 1.0
                cb, self.done = self.done, None
                cb()
            return GLib.SOURCE_REMOVE
        GLib.timeout_add(ms + 400, watchdog)


class Crtd:
    def __init__(self, app):
        self.app = app
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.ov = Overlay(app)
        self.key_fd = self.ui_fd = None
        self.held = self.long_fwd = self.swallow = False
        self.long_timer = None
        self.last_release = 0.0
        self.last_kind = None
        self.state = "awake"         # awake / blanking (waiting for the screen to go off) / asleep
        self.blank_deadline = 0.0
        self.setup_keys()
        self.poll_id = None
        self.repoll()
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 10, self.test)  # SIGUSR1: play off + on

    def test(self):
        log("test animation")
        self.ov.play("off", OFF_MS, lambda: GLib.timeout_add(600, lambda: self.ov.play("on", ON_MS, self.ov.hide) and False) and False)
        return GLib.SOURCE_CONTINUE

    # -- D-Bus -------------------------------------------------------------
    def call(self, dest, path, iface, method, args=None, rtype=None):
        try:
            return self.bus.call_sync(dest, path, iface, method, args, rtype, 0, 500, None).unpack()
        except GLib.Error as e:
            log("dbus", method, e.message)
            return None

    def screen_on(self):
        r = self.call("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                      "org.freedesktop.DBus.Properties", "Get",
                      GLib.Variant("(ss)", ("org.gnome.Mutter.DisplayConfig", "PowerSaveMode")))
        return r is None or r[0] == 0

    # Phosh's ScreenSaver GetActive/SetActive don't follow the lock screen; it sets
    # logind's LockedHint and unlocks on logind's Unlock signal.
    def session(self):
        out = subprocess.run(["loginctl", "list-sessions", "--no-legend"], capture_output=True, text=True).stdout
        for l in out.splitlines():
            f = l.split()
            if len(f) > 3 and f[2] == getpass.getuser() and f[3].startswith("seat"):
                return f[0]
        return None

    def locked(self):
        s = self.session()
        out = subprocess.run(["loginctl", "show-session", s, "-p", "LockedHint"], capture_output=True, text=True).stdout
        return "LockedHint=yes" in out

    def unlock(self):
        subprocess.run(["loginctl", "unlock-session", self.session()])

    # -- screen state watcher ------------------------------------------------
    def repoll(self):
        if self.poll_id:
            GLib.source_remove(self.poll_id)
        ms = 1000 if self.state == "awake" else 100
        self.poll_id = GLib.timeout_add(ms, self.poll)

    def poll(self):
        on = self.screen_on()
        old = self.state
        if self.state == "awake" and not on:
            self.state = "asleep"          # blanked by something else (idle timeout)
            self.ov.black()
        elif self.state == "blanking":
            if not on:
                self.state = "asleep"
            elif time.monotonic() > self.blank_deadline:
                log("screen never went off, uncovering it")
                self.wake_anim()
        elif self.state == "asleep" and on:
            self.wake_anim()
        if self.state != old:
            log("state", old, "->", self.state)
            self.repoll()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def wake_anim(self):
        # The panel lights up a little after PowerSaveMode reads 0: hold black until it has.
        self.state = "awake"
        self.ov.black()
        GLib.timeout_add(WAKE_HOLD_MS, lambda: self.state == "awake" and self.ov.play("on", ON_MS, self.ov.hide) and False)
        self.repoll()

    # -- power key -------------------------------------------------------------
    def setup_keys(self):
        try:
            fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
            fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
            fcntl.ioctl(fd, UI_SET_KEYBIT, KEY_POWER)
            fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack("HHHH80sI", 0x19, 0x1, 0x1, 1, b"crtd power key", 0))
            fcntl.ioctl(fd, UI_DEV_CREATE)
            self.ui_fd = fd
        except OSError as e:
            log("no uinput (%s): power key left to Phosh, wake animation only" % e)
            return
        for d in sorted(os.listdir("/sys/class/input")):
            try:
                with open("/sys/class/input/%s/device/name" % d) as f:
                    name = f.read().strip()
            except OSError:
                continue
            if d.startswith("event") and name == "pmic_pwrkey":
                self.key_fd = os.open("/dev/input/" + d, os.O_RDONLY | os.O_NONBLOCK)
                time.sleep(0.3)  # let libinput pick up the uinput clone first
                fcntl.ioctl(self.key_fd, EVIOCGRAB, 1)
                GLibUnix.fd_add_full(GLib.PRIORITY_HIGH, self.key_fd, GLib.IOCondition.IN, self.on_key_fd)
                log("power key grabbed:", d)
                return
        log("pmic_pwrkey not found")

    def emit(self, value):
        os.write(self.ui_fd, EVENT.pack(0, 0, EV_KEY, KEY_POWER, value) + EVENT.pack(0, 0, EV_SYN, 0, 0))

    def tap(self):
        self.emit(1)
        self.emit(0)

    def on_key_fd(self, fd, cond):
        try:
            data = os.read(fd, EVENT.size * 32)
        except BlockingIOError:
            return GLib.SOURCE_CONTINUE
        for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
            _, _, typ, code, val = EVENT.unpack_from(data, i)
            if typ == EV_KEY and code == KEY_POWER:
                if val == 1:
                    self.press_t = time.monotonic()
                    presslog("down")
                    self.press()
                elif val == 0:
                    presslog("up after %d ms%s" % ((time.monotonic() - getattr(self, "press_t", time.monotonic())) * 1000,
                                                   " (LONG = power menu)" if self.long_fwd else ""))
                    self.release()
        return GLib.SOURCE_CONTINUE

    def press(self):
        now = time.monotonic()
        if now - self.last_release < DOUBLE and self.last_kind:
            if self.last_kind == "off":   # first press started the power-off: cancel it
                self.ov.hide()
            self.last_kind = None
            self.swallow = True
            self.camera()
            return
        self.held = True
        self.long_timer = GLib.timeout_add(LONG_MS, self.long_press)

    def long_press(self):
        self.long_timer = None
        if self.held:
            self.long_fwd = True
            self.emit(1)
            # Phosh's power menu takes focus like the overview does: tell the overview
            # widget (+ joystick) not to show up over it
            self.bus.call("org.notarealinux.OverviewWidget", "/org/notarealinux/OverviewWidget",
                          "org.gtk.Actions", "Activate", GLib.Variant("(sava{sv})", ("power-menu", [], {})),
                          None, Gio.DBusCallFlags.NO_AUTO_START, 1000, None, None, None)
        return GLib.SOURCE_REMOVE

    def release(self):
        if self.swallow:
            self.swallow = False
            return
        self.held = False
        if self.long_timer:
            GLib.source_remove(self.long_timer)
            self.long_timer = None
        if self.long_fwd:
            self.long_fwd = False
            self.emit(0)
            return
        self.last_release = time.monotonic()
        if self.state == "awake" and self.screen_on():
            self.last_kind = "off"
            self.ov.play("off", OFF_MS, self.off_done)
        else:
            self.last_kind = "wake"
            self.ov.black()
            self.state = "asleep"
            self.repoll()
            self.tap()

    def off_done(self):
        self.ov.black()
        self.state = "blanking"
        self.blank_deadline = time.monotonic() + 1.5
        self.repoll()
        self.tap()
        return GLib.SOURCE_REMOVE

    # -- camera ---------------------------------------------------------------
    def camera(self):
        locked = self.locked()
        log("camera, locked:", locked)
        subprocess.Popen(["gtk-launch", CAMERA_APP], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # gtk-launch doesn't raise an already-running Snapshot, and Phosh shows its home
        # screen after an unlock: activate the window, and again once the unlock settles.
        def focus():
            subprocess.run(["wlrctl", "toplevel", "focus", "app_id:" + CAMERA_APP],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return GLib.SOURCE_REMOVE
        if not locked:
            for ms in (300, 1000):
                GLib.timeout_add(ms, focus)
            return
        deadline = time.monotonic() + 8

        def wait():
            found = subprocess.run(["wlrctl", "toplevel", "find", "app_id:" + CAMERA_APP],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            if found:
                focus()

                def unlock():
                    self.unlock()
                    log("unlocked for camera")
                    for ms in (150, 500, 1200):
                        GLib.timeout_add(ms, focus)
                    self.guard_camera()
                    return GLib.SOURCE_REMOVE
                GLib.timeout_add(250, unlock)
                return GLib.SOURCE_REMOVE
            if time.monotonic() > deadline:
                log("camera window never appeared, staying locked")
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(100, wait)

    def guard_camera(self):
        """The camera was opened from the lock screen: lock again as soon as it is left
        (swiped away, closed, or another app/the home screen comes up)."""
        armed = []
        deadline = time.monotonic() + 6

        def check():
            if self.locked():   # locked by other means (power key): done
                return GLib.SOURCE_REMOVE
            active = subprocess.run(["wlrctl", "toplevel", "find", "app_id:" + CAMERA_APP, "state:active"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            if active:
                armed.append(1)
            elif armed or time.monotonic() > deadline:
                log("left the camera, locking")
                subprocess.run(["loginctl", "lock-session", self.session()])
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(400, check)


def main():
    app = Gtk.Application(application_id="org.notarealinux.Crtd")
    keep = []

    def activate(a):
        if not keep:
            keep.append(Crtd(a))
            a.hold()

    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    main()
