#!/usr/bin/env python3
# rotated - calmer auto-rotation for Phosh.
#
# Phosh rotates as soon as iio-sensor-proxy says so (~45 deg of tilt). This
# daemon keeps Phosh's own rotation locked, reads the raw accelerometer itself
# (ssccli, Qualcomm SSC) and rotates the screen (via
# org.gnome.Mutter.DisplayConfig, which Phosh implements) only once the phone
# is tilted ROTATE_DEG away from the current orientation's upright, and the
# new orientation has held for STABLE_MS. Going 60 deg one way and needing
# 60 deg back gives hysteresis. Nearly flat = no change. The sensor is
# stopped while the screen is off.
#
# The quick-settings Rotation button still works as an on/off switch: when it
# gets unlocked, we flip our own enabled state and lock Phosh again.
# Enabled state lives in ~/.config/rotated-enabled ("1"/"0").
#
# The rotation itself makes the screen flash black (modeset); a short glitch
# overlay (glitch.py) covers it. ROTATED_GLITCH=0 disables it.

import math
import os
import re
import subprocess

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from glitch import Glitch  # noqa: E402

STABLE_MS = int(os.environ.get("ROTATED_STABLE_MS", "400"))
ROTATE_DEG = float(os.environ.get("ROTATED_ANGLE", "60"))
FLAT_DEG = 30.0      # need at least this much tilt up from lying flat
SMOOTH = 0.5         # low-pass factor per batch (shakes don't count)
READ_MS = 200        # sensor batch interval
ALLOW_UPSIDE_DOWN = os.environ.get("ROTATED_UPSIDE_DOWN", "0") == "1"
STATE_FILE = os.path.expanduser("~/.config/rotated-enabled")
# app ids (one per line) that stay portrait while they are the active window
PORTRAIT_FILE = os.path.expanduser("~/.config/rotated-portrait-apps")
TOPLEVEL_WATCH = os.path.expanduser("~/overview-widget/toplevel-watch")

# orientation -> wl_output / Mutter transform (as Phosh maps it)
TRANSFORMS = {"normal": 0, "left-up": 1, "bottom-up": 2, "right-up": 3}
NAMES = {t: o for o, t in TRANSFORMS.items()}
# angle of gravity in the screen plane, atan2(X, Y); measured 10-10:
# upright Y=+9.6, right-up X=+9.6, left-up X=-9.9
CENTER = {"normal": 0.0, "right-up": 90.0, "left-up": -90.0, "bottom-up": 180.0}
SAMPLE = re.compile(r"X=(\S+) Y=(\S+) Z=(\S+)")

LOCK_SCHEMA = "org.gnome.settings-daemon.peripherals.touchscreen"
LOCK_KEY = "orientation-lock"


def log(*a):
    print("rotated:", *a, flush=True)


class Rotated:
    def __init__(self, glitch=None):
        self.glitch = glitch
        self.enabled = self._load_enabled()
        self.pending = None
        self.timer = 0

        self.session = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.system = Gio.bus_get_sync(Gio.BusType.SYSTEM)

        self.settings = Gio.Settings.new(LOCK_SCHEMA)
        self.settings.connect("changed::" + LOCK_KEY, self._on_lock_changed)
        if not self.settings.get_boolean(LOCK_KEY):
            self.settings.set_boolean(LOCK_KEY, True)

        state = self._state()
        self.current = NAMES.get(state[2][0][3], "normal") if state else "normal"
        self.orientation = self.current
        self.fx = self.fy = None
        self.proc = None
        self.screen_on = True
        self.session.signal_subscribe(
            None, "org.freedesktop.DBus.Properties", "PropertiesChanged",
            "/org/gnome/Mutter/DisplayConfig", None, 0, self._on_display_props)
        self.active_app = None
        self._portrait_cache = (None, set())
        self._watch_apps()
        log("started, enabled =", self.enabled, "angle =", ROTATE_DEG,
            "stable_ms =", STABLE_MS, "current =", self.current)
        self._start_sensor()

    # --- enabled state / Phosh toggle -------------------------------------

    def _load_enabled(self):
        try:
            with open(STATE_FILE) as f:
                return f.read().strip() != "0"
        except OSError:
            return True

    def _save_enabled(self):
        try:
            with open(STATE_FILE, "w") as f:
                f.write("1\n" if self.enabled else "0\n")
        except OSError as e:
            log("save failed:", e)

    def _on_lock_changed(self, settings, key):
        if settings.get_boolean(LOCK_KEY):
            return  # our own re-lock, or already locked
        # User tapped the Rotation button: flip our state, keep Phosh locked.
        self.enabled = not self.enabled
        self._save_enabled()
        settings.set_boolean(LOCK_KEY, True)
        log("toggled, enabled =", self.enabled)
        subprocess.Popen(["notify-send", "-a", "Rotation", "-t", "1500",
                          "Auto-rotate " + ("on" if self.enabled else "off")])
        # Phosh may have rotated during the brief unlock; resync.
        if self.enabled:
            self._schedule(self._orientation(), delay=0)
        else:
            self._cancel()
            self._apply(0)

    # --- sensor -------------------------------------------------------------

    def _portrait_apps(self):
        try:
            mtime = os.path.getmtime(PORTRAIT_FILE)
        except OSError:
            return set()
        if self._portrait_cache[0] != mtime:
            with open(PORTRAIT_FILE) as f:
                apps = {line.strip() for line in f if line.strip() and not line.startswith("#")}
            self._portrait_cache = (mtime, apps)
        return self._portrait_cache[1]

    def _orientation(self):
        if self.active_app and self.active_app in self._portrait_apps():
            return "normal"
        return self.orientation

    def _watch_apps(self):
        """Follow the active window so portrait-only apps can pin the screen upright."""
        if not os.path.exists(TOPLEVEL_WATCH):
            return
        proc = Gio.Subprocess.new([TOPLEVEL_WATCH], Gio.SubprocessFlags.STDOUT_PIPE)
        stream = Gio.DataInputStream.new(proc.get_stdout_pipe())

        def on_line(src, res):
            line, _ = src.read_line_finish_utf8(res)
            if line is None:
                self.active_app = None
                return
            line = line.strip()
            app = line.split(" ", 1)[1] if line.startswith("active ") else None
            if app != self.active_app:
                self.active_app = app
                want = self._orientation()
                if want != self.current:
                    self._schedule(want, delay=0 if want == "normal" else STABLE_MS)
            src.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)
        self._toplevel_proc = proc

    def _on_display_props(self, conn, sender, path, iface, signal, params):
        _iface, changed, _inv = params.unpack()
        if "PowerSaveMode" not in changed:
            return
        on = changed["PowerSaveMode"] == 0
        if on == self.screen_on:
            return
        self.screen_on = on
        if on:
            self._start_sensor()
        else:
            self._stop_sensor()

    def _start_sensor(self):
        if self.proc or not self.screen_on:
            return False
        self.fx = self.fy = None
        self.buf = b""
        try:
            self.proc = subprocess.Popen(
                ["ssccli", "--sensor", "accelerometer", "--timeout", "86400"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            log("ssccli failed:", e)
            GLib.timeout_add_seconds(5, self._start_sensor)
            return False
        os.set_blocking(self.proc.stdout.fileno(), False)
        # ~25 samples/s arrive; read them in batches (READ_MS) instead of waking per line
        GLib.timeout_add(READ_MS, self._read_batch, self.proc)
        return False

    def _stop_sensor(self):
        proc, self.proc = self.proc, None
        if proc:
            proc.kill()
            proc.wait()
        self._cancel()

    def _read_batch(self, proc):
        if proc is not self.proc:
            return False  # stopped / replaced
        try:
            chunk = os.read(proc.stdout.fileno(), 65536)
        except BlockingIOError:
            return True
        if not chunk:  # EOF
            log("ssccli exited, restarting")
            self.proc = None
            proc.wait()
            GLib.timeout_add_seconds(2, self._start_sensor)
            return False
        lines = (self.buf + chunk).split(b"\n")
        self.buf = lines.pop()
        xs, ys = [], []
        for line in lines:
            m = SAMPLE.search(line.decode(errors="replace"))
            if m:
                xs.append(float(m.group(1)))
                ys.append(float(m.group(2)))
        if xs:
            self._on_sample(sum(xs) / len(xs), sum(ys) / len(ys))
        return True

    def _on_sample(self, x, y):
        if self.fx is None:
            self.fx, self.fy = x, y
        else:
            self.fx += SMOOTH * (x - self.fx)
            self.fy += SMOOTH * (y - self.fy)
        if math.hypot(self.fx, self.fy) < 9.81 * math.sin(math.radians(FLAT_DEG)):
            o = "undefined"  # (nearly) flat: keep whatever we have
        else:
            ang = math.degrees(math.atan2(self.fx, self.fy))
            off = lambda c: abs((ang - c + 180) % 360 - 180)  # noqa: E731
            nearest = min(CENTER, key=lambda k: off(CENTER[k]))
            if nearest != self.current and off(CENTER[self.current]) < ROTATE_DEG:
                o = self.current  # not tilted far enough yet
            else:
                o = nearest
        if o != self.orientation:
            self.orientation = o
            self._schedule(o)

    def _cancel(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        self.pending = None

    def _schedule(self, orientation, delay=STABLE_MS):
        if not self.enabled:
            return
        if orientation not in TRANSFORMS or (
                orientation == "bottom-up" and not ALLOW_UPSIDE_DOWN):
            self._cancel()  # flat / unknown / upside down: keep current
            return
        if orientation == self.pending and self.timer:
            return
        self._cancel()
        self.pending = orientation
        self.timer = GLib.timeout_add(delay, self._fire)

    def _fire(self):
        self.timer = 0
        o, self.pending = self.pending, None
        if not (self.enabled and o and self._orientation() == o):
            return False
        t = TRANSFORMS[o]
        state = self._state()
        if not state or not state[2]:   # screen off / locked: no logical monitor
            return False
        if state[2][0][3] == t:
            return False
        if self.glitch:
            self.glitch.run(lambda: self._apply(t))
        else:
            self._apply(t)
        return False

    # --- display ------------------------------------------------------------

    def _state(self):
        try:
            return self.session.call_sync(
                "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                "org.gnome.Mutter.DisplayConfig", "GetCurrentState",
                None, None, 0, 2000, None).unpack()
        except GLib.Error as e:
            log("GetCurrentState failed:", e.message)
            return None

    def _apply(self, transform):
        state = self._state()
        if not state:
            return
        serial, monitors, logical, _props = state
        modes = {}
        for (conn, _v, _p, _s), mlist, _mp in monitors:
            for m in mlist:
                if m[6].get("is-current"):
                    modes[conn] = m[0]
        new = []
        changed = False
        for x, y, scale, cur_t, primary, mons, _lp in logical:
            if cur_t != transform:
                changed = True
            new.append((x, y, scale, transform, primary,
                        [(c[0], modes[c[0]], {}) for c in mons if c[0] in modes]))
        if not changed:
            return
        try:
            self.session.call_sync(
                "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                "org.gnome.Mutter.DisplayConfig", "ApplyMonitorsConfig",
                GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})", (serial, 2, new, {})),  # method 2: Phosh ignores 1 (temporary)
                None, 0, 3000, None)
            self.current = NAMES[transform]
            log("rotated to", self.current)
        except GLib.Error as e:
            log("ApplyMonitorsConfig failed:", e.message)


def main():
    app = Gtk.Application(application_id="org.notarealinux.Rotated",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(a):
        a.hold()
        glitch = Glitch(a) if os.environ.get("ROTATED_GLITCH", "1") != "0" else None
        a.rotated = Rotated(glitch)
        if glitch:  # kill -USR1 <pid>: play the glitch without rotating
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 10,
                                 lambda: glitch.run(lambda: None) or True)

    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    main()
