"""Joystick launcher for the Phosh overview: replaces the app grid with one pulsing circle.

Hold the circle and your top 7 apps + an ALL slot fan out around it, with the next 10 in a
fainter outer ring; slide toward one (it grows and glows,
its name shows) and let go to open it. Let go near the middle to cancel. A quick tap leaves the
fan open for tapping; tap the middle to close. Shown/hidden by ~/overview-widget together with
the overview; usage ranking comes from arc.Usage (toplevel activations, 7-day half-life).
The ALL slot (3x3 dots, upper left for the left hand) swaps the joystick for a searchable grid
of every app, since Phosh's own grid + search are hidden under this panel.
"""
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
import notareal_theme as theme  # noqa: E402  ~/theme (colors by name, live switch)
from ram import RAM_CSS, RamPage  # noqa: E402  RAM page (tap the battery line)

N_APPS = 8          # inner ring slots, the last one is ALL
N_OUTER = 10        # outer (faded) ring: the next 10 most used apps
OUTER_ALPHA = 0.55  # outer ring opacity until one is picked
ALL = None          # the ALL slot's "app"
PANEL_H = 482        # logical px: Phosh search bar + favourites + app grid (measured 10-10 capture)
PANEL_BOTTOM = 0     # grid runs to the bottom edge in the overview
CORE = 30            # resting circle radius
ICON = 44
ICON_OUTER = 38
OPEN_MS = 190
STALL_MS = 300       # 10-17: no frame this long after a touch = stalled frame clock, remap the panel
STAGGER_MS = 50      # opening: each app leaves the core this long after the previous one...
POP_MS = 200         # ...and takes this long to reach its slot (with a small overshoot)
OUTER_AT_MS = (N_APPS - 1) * STAGGER_MS + POP_MS // 2   # then the outer ring fades in all at once, in place
OUTER_FADE_MS = 300
RAISE_POLL_MS = 100  # (300 before 10-13; wlrctl list takes <10 ms) after a launch, look for the app's window this often...
RAISE_TIMEOUT = 25   # ...for this many seconds (Karere/CEF needs ~12 s)
FOLD_AFTER_MS = 250  # (600 before 10-13, arbitrary) then fold the overview if it is still up
CLEAN_ENV = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}


def rgba(spec, a=None):
    c = Gdk.RGBA()
    c.parse(theme.resolve(spec))
    if a is not None:
        c.alpha = a
    return c


def circle(snap, x, y, r, color, stroke=None):
    pb = Gsk.PathBuilder.new()
    pb.add_circle(Graphene.Point().init(x, y), r)
    path = pb.to_path()
    if stroke:
        snap.append_stroke(path, Gsk.Stroke.new(stroke), color)
    else:
        snap.append_fill(path, Gsk.FillRule.WINDING, color)


IDLE_FPS = 20

ALL_CSS = b"""
window.joystick { background: transparent; }
.allapps { background: alpha(var(--nr-bg), 0.94); border: 2px solid var(--nr-accent); border-radius: 12px;
           margin: 8px 10px 10px 10px; padding: 10px; }
.allapps entry { background: var(--nr-bg); color: var(--nr-white); border: 2px solid var(--nr-violet); border-radius: 8px;
                 font-family: var(--nr-font-code); min-height: 36px; }
button.all-back { background: var(--nr-pink); color: var(--nr-bg); border: none; border-radius: 8px;
                  min-width: 44px; min-height: 36px; font-size: 22px; font-weight: bold; padding: 0; }
button.all-app { background: none; border: none; box-shadow: none; padding: 6px 2px; border-radius: 10px; }
button.all-app:active { background: alpha(var(--nr-accent), 0.25); }
.all-name { color: var(--nr-fg); font-family: var(--nr-font-code); font-size: 11px; }
"""


class JoystickView(Gtk.Widget):
    def __init__(self, on_launch):
        super().__init__()
        self.on_launch = on_launch
        self.apps = []           # (app, paintable)
        self.t0 = time.monotonic()
        self.fan = 0.0           # 0 closed .. 1 open (eased)
        self.fan_target = 0.0
        self.open_t = None       # when the fan started opening (staggered pop-out)
        self.sel = None
        self.sel_glow = 0.0
        self.sticky = False
        self.outside_touch = False   # a finger is down (set by overview-widget's evdev reader)
        self.held = False
        self.tick = None
        self.press = None
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.add_controller(drag)
        self.connect("map", lambda *_: self._start())
        self.connect("unmap", lambda *_: self._stop())

    # --- data ------------------------------------------------------------------------------
    def set_apps(self, apps):
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        self.apps = []
        for k, app in enumerate(apps[:N_APPS + N_OUTER]):
            if app is ALL:
                self.apps.append((ALL, None))
                continue
            gicon = app.get_icon() or Gio.ThemedIcon.new("application-x-executable")
            self.apps.append((app, theme.lookup_by_gicon(gicon, ICON if k < N_APPS else ICON_OUTER, 2, Gtk.TextDirection.NONE,
                                                         Gtk.IconLookupFlags(0))))

    def reset(self):
        self.fan = self.fan_target = 0.0
        self.open_t = None
        self.sel, self.sticky, self.held = None, False, False
        self.queue_draw()

    # --- geometry -------------------------------------------------------------------------
    def center(self):
        return self.get_width() / 2, self.get_height() / 2 + 8

    def radius(self):
        """Inner ring radius (pulled in closer since the outer ring arrived)."""
        return min(self.get_width() * 0.235, self.get_height() * 0.26)

    def radius_outer(self):
        # stays inside the panel: the bottom icon just touches the screen edge
        return min(self.get_width() * 0.40, self.get_height() / 2 - 8 - ICON_OUTER / 2 - 2)

    @staticmethod
    def ring(k):
        """(ring index, slots in that ring, position in it)."""
        return (0, N_APPS, k) if k < N_APPS else (1, N_OUTER, k - N_APPS)

    def progress(self, k):
        """(raw 0..1, eased) opening progress of app k: they pop out one by one, in slot order.
        Closing (or a fan already open) uses the shared fan value."""
        if self.fan_target < 1 or self.open_t is None:
            return self.fan, self.fan
        ms = (time.monotonic() - self.open_t) * 1000
        if k >= N_APPS:                            # outer ring: one shared fade, no stagger
            p = min(1.0, max(0.0, (ms - OUTER_AT_MS) / OUTER_FADE_MS))
            return p, p * p * (3 - 2 * p)
        p = min(1.0, max(0.0, (ms - k * STAGGER_MS) / POP_MS))
        c = 1.9                                    # ease-out-back: ~8% overshoot, then settle
        q = p - 1
        return p, 1 + (c + 1) * q ** 3 + c * q ** 2

    def opening(self):
        return (self.open_t is not None and self.fan_target == 1 and
                (time.monotonic() - self.open_t) * 1000 < max((min(len(self.apps), N_APPS) - 1) * STAGGER_MS + POP_MS,
                                                              OUTER_AT_MS + OUTER_FADE_MS if len(self.apps) > N_APPS else 0))

    def slot(self, k):
        cx, cy = self.center()
        ring, n, i = self.ring(k)
        a = -math.pi / 2 + i * 2 * math.pi / n                          # first app straight up
        r0 = self.radius()
        if ring == 0:
            r = r0 * (0.25 + 0.75 * self.progress(k)[1])
        elif self.fan_target == 1:                                      # outer: fades in where it sits
            r = self.radius_outer()
        else:                                                           # ...and folds back in on close
            r = r0 + (self.radius_outer() - r0) * self.fan
        return cx + r * math.cos(a), cy + r * math.sin(a), a

    def pick(self, x, y):
        cx, cy = self.center()
        dx, dy = x - cx, y - cy
        d = math.hypot(dx, dy)
        if d < self.radius() * 0.5 or not self.apps:
            return None
        outer = d > (self.radius() + self.radius_outer()) / 2 and len(self.apps) > N_APPS
        n = len(self.apps) - N_APPS if outer else min(N_APPS, len(self.apps))
        ang = math.atan2(dy, dx)
        i = round((ang + math.pi / 2) / (2 * math.pi / N_OUTER if outer else 2 * math.pi / N_APPS))
        i %= N_OUTER if outer else N_APPS
        if i >= n:
            return None
        return N_APPS + i if outer else i

    # --- input -----------------------------------------------------------------------------
    def _begin(self, _g, x, y):
        self.press = (x, y)
        cx, cy = self.center()
        near_core = math.hypot(x - cx, y - cy) < CORE * 1.8
        if self.sticky:
            if near_core:
                self.press = None            # tap the middle: close
                self.reset()
                return
            self._select(self.pick(x, y))
            return
        if not near_core:
            self.press = None
            return
        self.held = True
        self._open()

    def _update(self, _g, dx, dy):
        if self.press is None:
            return
        x, y = self.press
        self._select(self.pick(x + dx, y + dy))

    def _end(self, _g, dx, dy):
        if self.press is None:
            return
        self.press = None
        moved = math.hypot(dx, dy) > 12
        if self.sel is not None:
            app = self.apps[self.sel][0]
            self.reset()
            self.on_launch(app)
        elif not moved and not self.sticky:
            self.sticky = True                # a tap: keep the fan open
        else:
            self.reset()
        self.held = False

    def _open(self):
        if self.fan_target != 1.0:
            self.open_t = time.monotonic()
        self.fan_target = 1.0
        self._start()

    def _select(self, k):
        self.sel = k

    # --- animation ---------------------------------------------------------------------------
    def _start(self):
        if self.tick is None:
            self.last = self.last_draw = time.monotonic()
            self.tick = self.add_tick_callback(self._on_tick)

    def _stop(self):
        if self.tick is not None:
            self.remove_tick_callback(self.tick)
            self.tick = None

    def _on_tick(self, _w, _clock):
        now = time.monotonic()
        dt, self.last = now - self.last, now
        k = min(1.0, dt * 1000 / OPEN_MS * 3)
        self.fan += (self.fan_target - self.fan) * k
        target = 1.0 if self.sel is not None else 0.0
        self.sel_glow += (target - self.sel_glow) * min(1.0, dt * 14)
        idle = not self.opening() and self.fan_target == 0 and self.fan < 0.01 and self.sel is None and self.sel_glow < 0.01
        if idle and (self.outside_touch or now - self.last_draw < 1 / IDLE_FPS):
            return True  # the idle breathing pulse only needs ~20 fps; leave the GPU to Phosh's swipes
        self.last_draw = now
        self.queue_draw()
        return True

    # --- drawing ---------------------------------------------------------------------------------
    def do_snapshot(self, snap):
        if self.get_width() <= 0:
            return
        cx, cy = self.center()
        t = time.monotonic() - self.t0
        pulse = (t % 1.8) / 1.8                       # 0..1 breathing wave
        f = self.fan
        # backdrop disc that grows with the fan
        if f > 0.01:
            R = self.radius()
            Ro = self.radius_outer() if len(self.apps) > N_APPS else R * 1.2
            for frac, a in ((1.16, 0.10), (1.08, 0.22), (1.0, 0.45)):
                circle(snap, cx, cy, Ro * frac * (0.5 + 0.5 * f), rgba("#1c2340", a * f))
            circle(snap, cx, cy, R * 1.45 * (0.5 + 0.5 * f), rgba("#1c2340", 0.25 * f))   # inner ring disc
            circle(snap, cx, cy, R * (0.25 + 0.75 * f), rgba("#c5a0ff", 0.18 * f), stroke=1.5)
            if len(self.apps) > N_APPS:
                circle(snap, cx, cy, Ro * (0.5 + 0.5 * f), rgba("#c5a0ff", 0.09 * f), stroke=1)
        # the joystick: expanding pulse rings + glowing core
        for ph in (pulse, (pulse + 0.5) % 1.0):
            circle(snap, cx, cy, CORE * (1.0 + 0.9 * ph), rgba("#c5a0ff", 0.45 * (1 - ph) * (1 - 0.6 * f)), stroke=2)
        breathe = 0.5 + 0.5 * math.sin(t * 2 * math.pi / 1.8)
        for rr, col, a in ((CORE * 1.35, "#f7a1e8", 0.10 + 0.08 * breathe), (CORE * 1.1, "#c5a0ff", 0.22),
                           (CORE, "#1c2340", 0.85)):
            circle(snap, cx, cy, rr, rgba(col, a))
        circle(snap, cx, cy, CORE, rgba("#c5a0ff", 0.9), stroke=2)
        circle(snap, cx, cy, CORE * (0.28 + 0.06 * breathe), rgba("#6fd6f2", 0.85))
        # apps
        if f > 0.02:
            for k, (app, paint) in enumerate(self.apps):
                x, y, _a = self.slot(k)
                pk, ek = self.progress(k)
                if pk <= 0:
                    continue                          # still waiting for its turn
                selected = k == self.sel
                size = ICON if k < N_APPS else ICON_OUTER
                fade = 1.0 if k < N_APPS or selected else OUTER_ALPHA
                grow = 1.0 + (0.4 * self.sel_glow if selected else 0.0)
                if selected:
                    g = size * 1.2 * grow
                    for rr, col, a in ((g, "#f7a1e8", 0.18), (g * 0.75, "#c5a0ff", 0.30), (g * 0.5, "#ffffff", 0.10)):
                        circle(snap, x, y, rr, rgba(col, a * self.sel_glow))
                d = size * grow * ((0.6 + 0.4 * ek) if k < N_APPS else (0.85 + 0.15 * ek))
                snap.push_opacity(fade * min(1.0, pk * 1.6) * (0.5 if self.sel is not None and not selected else 1.0))
                if app is ALL:
                    for i in range(3):
                        for j in range(3):
                            circle(snap, x + (i - 1) * d * 0.3, y + (j - 1) * d * 0.3, d * 0.09,
                                   rgba(("#f7a1e8", "#c5a0ff", "#6fd6f2")[(i + j) % 3]))
                    circle(snap, x, y, d * 0.55, rgba("#c5a0ff", 0.7), stroke=2)
                elif paint is not None:
                    snap.save()
                    snap.translate(Graphene.Point().init(x - d / 2, y - d / 2))
                    paint.snapshot(snap, d, d)
                    snap.restore()
                snap.pop()
            if self.sel is not None:
                app = self.apps[self.sel][0]
                # above the core: the outer ring now reaches the panel top, and the thumb comes from below
                self._title(snap, "ALL APPS" if app is ALL else app.get_display_name())

    def _title(self, snap, text):
        """The selected app's name in the top-right corner, away from the (left) thumb."""
        layout = self.create_pango_layout(text)
        layout.set_font_description(Pango.FontDescription.from_string("JetBrains Mono Bold 17px"))
        _ink, lg = layout.get_pixel_extents()
        self._label(snap, text, max(lg.width / 2 + 6, self.get_width() - lg.width / 2 - 14), 10)

    def _label(self, snap, text, x, y):
        layout = self.create_pango_layout(text)
        layout.set_font_description(Pango.FontDescription.from_string("JetBrains Mono Bold 17px"))
        _ink, lg = layout.get_pixel_extents()
        ox, oy = x - lg.width / 2, y
        for dx, dy, col, a in ((0, 0, "#c5a0ff", 0.6), (1.5, 0, "#f7a1e8", 0.4), (-1.5, 0, "#6fd6f2", 0.4),
                               (0, 1.5, "#c5a0ff", 0.4), (0, -1.5, "#c5a0ff", 0.4)):
            snap.save()
            snap.translate(Graphene.Point().init(ox + dx, oy + dy))
            snap.append_layout(layout, rgba(col, a))
            snap.restore()
        snap.save()
        snap.translate(Graphene.Point().init(ox, oy))
        snap.append_layout(layout, rgba("#ffffff"))
        snap.restore()


MODE_FILE = os.path.expanduser("~/.config/joystick-mode")   # "float" (10-13 trial) or "ring"
FLOAT_SPRING = 380.0     # pull of a ball toward its ring slot (1/s^2)...
FLOAT_DAMP = 17.0        # ...underdamped, so balls overshoot a little and settle
FLOAT_ITERS = 12         # collision passes per frame (6 left corner balls overlapping ~7 px)
FLOAT_GAP = 3            # px between touching balls
CORE_POP_MS = 180


def float_mode():
    try:
        with open(MODE_FILE) as f:
            return f.read().strip() != "ring"
    except OSError:
        return True


class FloatJoystickView(JoystickView):
    """10-13 variant: nothing shows until a thumb touches the panel; the core appears under the
    thumb and the apps fan out around it as balls. Their ring slots are only spring targets: the
    panel edges, the core and each other are hard limits, so near an edge the balls get squeezed
    together between the thumb and the edge. Slide to the nearest ball,
    let go to open it; let go on the core to cancel; a tap leaves the balls up (tap empty space
    to move them there, tap the core to close)."""

    def __init__(self, on_launch):
        super().__init__(on_launch)
        self.c = (0.0, 0.0)      # where the thumb came down
        self.ticked, self.on_stall = True, None
        self.pos, self.vel = [], []

    # --- geometry -------------------------------------------------------------------------
    def center(self):
        return self.c

    def radius(self):
        return self.get_width() * 0.235

    def radius_outer(self):
        return self.get_width() * 0.40

    def ball_r(self, k):
        grow = 1.0 + (0.4 * self.sel_glow if k == self.sel else 0.0)
        return ((ICON / 2 + 5) if k < N_APPS else (ICON_OUTER / 2 + 4)) * grow

    def target(self, k):
        cx, cy = self.c
        ring, n, i = self.ring(k)
        a = -math.pi / 2 + i * 2 * math.pi / n
        r = self.radius() if ring == 0 else self.radius_outer()
        return cx + r * math.cos(a), cy + r * math.sin(a)

    def active(self, k):
        """Has ball k left the core yet (staggered opening; all of them once the fan is open)."""
        if self.open_t is None:
            return False
        ms = (time.monotonic() - self.open_t) * 1000
        return ms >= (k * STAGGER_MS if k < N_APPS else OUTER_AT_MS)

    def pick(self, x, y):
        cx, cy = self.c
        if math.hypot(x - cx, y - cy) < max(CORE * 1.6, self.radius() * 0.4):
            return None
        best, bd = None, 1e9
        for k in range(len(self.pos)):
            if self.active(k):
                d = math.hypot(x - self.pos[k][0], y - self.pos[k][1])
                if d < bd:
                    best, bd = k, d
        return best

    def ball_at(self, x, y):
        for k in range(len(self.pos)):
            if self.active(k) and math.hypot(x - self.pos[k][0], y - self.pos[k][1]) < self.ball_r(k) * 1.3:
                return k
        return None

    # --- input -----------------------------------------------------------------------------
    def _begin(self, _g, x, y):
        self.press = (x, y)
        if self.sticky:
            if math.hypot(x - self.c[0], y - self.c[1]) < CORE * 1.8:
                self.press = None            # tap the core: close
                self.reset()
                return
            k = self.ball_at(x, y)
            if k is not None:
                self._select(k)
                return
            self.reset()                     # empty space: move the fan here
        self.held = True
        self._open_at(x, y)

    def _open_at(self, x, y):
        self.c = (x, y)
        self.pos = [list(self.target(k)) if k >= N_APPS else [x, y] for k in range(len(self.apps))]
        self.vel = [[0.0, 0.0] for _ in self.apps]
        self.fan_target = 0.0                # force a fresh opening
        self.ticked = False
        self._open()
        GLib.timeout_add(STALL_MS, self._check_stall)

    def _check_stall(self):
        """10-17: sometimes the panel's frame clock stops (no frame callbacks from phoc), so touches
        still pick apps but nothing is drawn. If no frame came since the touch, remap the window and
        leave the fan up (sticky) so the next tap works."""
        if self.ticked or not (self.held or self.sticky):
            return False
        print("joystick: frame clock stalled (tick %s, mapped %s), remapping" % (self.tick is not None, self.get_mapped()), flush=True)
        self.sticky, self.held, self.press = True, False, None
        self._stop()
        if self.on_stall:
            self.on_stall()
        self._start()
        return False

    # --- physics -----------------------------------------------------------------------------
    def _on_tick(self, _w, _clock):
        self.ticked = True
        now = time.monotonic()
        dt, self.last = min(now - self.last, 1 / 30), now
        self.fan += (self.fan_target - self.fan) * min(1.0, dt * 1000 / OPEN_MS * 3)
        target = 1.0 if self.sel is not None else 0.0
        self.sel_glow += (target - self.sel_glow) * min(1.0, dt * 14)
        if self.fan_target == 1 and dt > 0:
            self._step(dt)
        if self.fan_target == 0 and self.fan < 0.01 and not self.sticky:
            self.fan = 0.0
            self.queue_draw()
            self.tick = None                 # nothing to draw until the next touch: stop ticking
            return False
        self.queue_draw()
        return True

    def _step(self, dt):
        n = len(self.pos)
        act = [self.active(k) for k in range(n)]
        cx, cy = self.c
        prev = [p[:] for p in self.pos]
        for k in range(n):
            if not act[k]:
                self.pos[k] = [cx, cy]
                continue
            tx, ty = self.target(k)
            v, p = self.vel[k], self.pos[k]
            v[0] += (FLOAT_SPRING * (tx - p[0]) - FLOAT_DAMP * v[0]) * dt
            v[1] += (FLOAT_SPRING * (ty - p[1]) - FLOAT_DAMP * v[1]) * dt
            p[0] += v[0] * dt
            p[1] += v[1] * dt
        W, H = self.get_width(), self.get_height()
        rad = [self.ball_r(k) for k in range(n)]
        core = CORE * 1.5
        for _ in range(FLOAT_ITERS):
            for i in range(n):               # balls push each other apart (a picked ball pushes harder)
                if not act[i]:
                    continue
                for j in range(i + 1, n):
                    if not act[j]:
                        continue
                    dx, dy = self.pos[j][0] - self.pos[i][0], self.pos[j][1] - self.pos[i][1]
                    d = math.hypot(dx, dy) or 0.01
                    over = rad[i] + rad[j] + FLOAT_GAP - d
                    if over > 0:
                        wi = 0.1 if i == self.sel else 0.9 if j == self.sel else 0.5
                        ux, uy = dx / d, dy / d
                        self.pos[i][0] -= ux * over * wi
                        self.pos[i][1] -= uy * over * wi
                        self.pos[j][0] += ux * over * (1 - wi)
                        self.pos[j][1] += uy * over * (1 - wi)
            for k in range(n):               # ...stay off the core and inside the panel
                if not act[k]:
                    continue
                p, r = self.pos[k], rad[k]
                dx, dy = p[0] - cx, p[1] - cy
                d = math.hypot(dx, dy)
                if d < core + r:
                    if d < 0.01:
                        dx, dy, d = 0.0, -1.0, 1.0
                    p[0], p[1] = cx + dx / d * (core + r), cy + dy / d * (core + r)
                p[0] = min(max(p[0], r + 2), W - r - 2)
                p[1] = min(max(p[1], r + 2), H - r - 2)
        for k in range(n):
            if not act[k]:
                continue
            # velocity = what actually happened (contacts eat the momentum: balls pile up, no bouncing apart)
            self.vel[k] = [(self.pos[k][0] - prev[k][0]) / dt, (self.pos[k][1] - prev[k][1]) / dt]

    # --- drawing ---------------------------------------------------------------------------------
    def do_snapshot(self, snap):
        f = self.fan
        if self.get_width() <= 0:
            return
        # an all-empty frame lets touches fall through to Phosh's hidden grid: keep a ~invisible layer
        snap.append_color(rgba("#000000", 0.004), Graphene.Rect().init(0, 0, self.get_width(), self.get_height()))
        if f <= 0.01 or not self.pos:
            return
        cx, cy = self.c
        t = time.monotonic() - self.t0
        ms = (time.monotonic() - self.open_t) * 1000 if self.open_t else 1e9
        p = min(1.0, ms / CORE_POP_MS) - 1
        pop = (1 + 2.9 * p ** 3 + 1.9 * p ** 2) if self.fan_target == 1 else f
        # soft backdrop under the thumb (the panel edges clip it)
        R = self.radius_outer()
        for frac, a in ((1.16, 0.10), (1.08, 0.22), (1.0, 0.40)):
            circle(snap, cx, cy, R * frac * (0.5 + 0.5 * f), rgba("#1c2340", a * f))
        # core under the thumb
        pulse = (t % 1.8) / 1.8
        for ph in (pulse, (pulse + 0.5) % 1.0):
            circle(snap, cx, cy, CORE * pop * (1.0 + 0.9 * ph), rgba("#c5a0ff", 0.35 * (1 - ph) * f), stroke=2)
        breathe = 0.5 + 0.5 * math.sin(t * 2 * math.pi / 1.8)
        for rr, col, a in ((CORE * 1.35, "#f7a1e8", 0.10 + 0.08 * breathe), (CORE * 1.1, "#c5a0ff", 0.22),
                           (CORE, "#1c2340", 0.85)):
            circle(snap, cx, cy, rr * pop, rgba(col, a * f))
        circle(snap, cx, cy, CORE * pop, rgba("#c5a0ff", 0.9 * f), stroke=2)
        circle(snap, cx, cy, CORE * pop * (0.28 + 0.06 * breathe), rgba("#6fd6f2", 0.85 * f))
        # balls
        for k, (app, paint) in enumerate(self.apps):
            if k >= len(self.pos) or not self.active(k):
                continue
            x, y = self.pos[k]
            if self.fan_target == 0:                       # closing: fall back into the thumb
                x, y = cx + (x - cx) * f, cy + (y - cy) * f
            selected = k == self.sel
            inner = k < N_APPS
            r = self.ball_r(k)
            size = (ICON if inner else ICON_OUTER) * (1.0 + (0.4 * self.sel_glow if selected else 0.0))
            fade = 1.0 if inner or selected else OUTER_ALPHA
            if not inner and self.fan_target == 1:
                fade *= min(1.0, (ms - OUTER_AT_MS) / OUTER_FADE_MS)   # outer balls fade in where they land
            if selected:
                for rr, col, a in ((r * 1.5, "#f7a1e8", 0.18), (r * 1.2, "#c5a0ff", 0.30)):
                    circle(snap, x, y, rr, rgba(col, a * self.sel_glow))
            snap.push_opacity(fade * f * (0.5 if self.sel is not None and not selected else 1.0))
            snap.save()
            snap.translate(Graphene.Point().init(x, y))
            circle(snap, 0, 0, r, rgba("#1c2340", 0.75))
            circle(snap, 0, 0, r, rgba("#c5a0ff", 0.55), stroke=1.5)
            if app is ALL:
                for i in range(3):
                    for j in range(3):
                        circle(snap, (i - 1) * size * 0.25, (j - 1) * size * 0.25, size * 0.08,
                               rgba(("#f7a1e8", "#c5a0ff", "#6fd6f2")[(i + j) % 3]))
            elif paint is not None:
                d = size * 0.86
                snap.translate(Graphene.Point().init(-d / 2, -d / 2))
                paint.snapshot(snap, d, d)
            snap.restore()
            snap.pop()
        if self.sel is not None and self.sel < len(self.apps):
            app = self.apps[self.sel][0]
            self._title(snap, "ALL APPS" if app is ALL else app.get_display_name())

    def _label(self, snap, text, x, y):
        layout = self.create_pango_layout(text)
        layout.set_font_description(Pango.FontDescription.from_string("JetBrains Mono Bold 17px"))
        _ink, lg = layout.get_pixel_extents()
        x = min(max(x, lg.width / 2 + 6), self.get_width() - lg.width / 2 - 6)   # keep it on screen
        super()._label(snap, text, x, y)


class Joystick:
    """The layer-shell panel over Phosh's (hidden) app grid."""

    def __init__(self, app, usage):
        self.usage = usage
        self.win = Gtk.ApplicationWindow(application=app)
        self.win.add_css_class("joystick")
        LayerShell.init_for_window(self.win)
        LayerShell.set_namespace(self.win, "joystick")
        LayerShell.set_layer(self.win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.NONE)
        LayerShell.set_exclusive_zone(self.win, -1)
        for e in (LayerShell.Edge.BOTTOM, LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(self.win, e, True)
        LayerShell.set_margin(self.win, LayerShell.Edge.BOTTOM, PANEL_BOTTOM)
        self.win.set_default_size(540, PANEL_H)
        theme.add_css((ALL_CSS + RAM_CSS).decode())
        self.view = (FloatJoystickView if float_mode() else JoystickView)(self.launch)
        self.view.on_stall = self._remap
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=150)
        self.stack.add_named(self.view, "joy")
        self.stack.add_named(self._build_all(), "all")
        self.ram = RamPage(self._show_joy)
        self.stack.add_named(self.ram, "ram")
        self.win.set_child(self.stack)

    def _check_mode(self):
        """~/.config/joystick-mode switches between the float and ring joysticks, live."""
        cls = FloatJoystickView if float_mode() else JoystickView
        if type(self.view) is cls:
            return
        self.stack.remove(self.view)
        self.view = cls(self.launch)
        self.view.on_stall = self._remap
        self.stack.add_named(self.view, "joy")

    def show(self):
        self._check_mode()
        ranked = self.usage.ranked(N_APPS - 1 + N_OUTER)
        self.view.set_apps(ranked[:N_APPS - 1] + [ALL] + ranked[N_APPS - 1:])
        self.view.reset()
        self._show_joy()
        self.win.present()

    def _remap(self):
        """Unmap + map gives the panel a fresh surface and frame clock (see _check_stall)."""
        self.win.set_visible(False)
        self.win.present()

    def hide(self):
        self._show_joy()
        self.win.set_visible(False)

    # --- ALL page ----------------------------------------------------------------------------
    def _build_all(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["allapps"])
        top = Gtk.Box(spacing=8)
        back = Gtk.Button(label="\u2039", css_classes=["chip", "all-back"])   # left: left-handed
        back.connect("clicked", lambda *_: self._show_joy())
        self.search = Gtk.SearchEntry(hexpand=True, placeholder_text="search apps")
        self.search.connect("search-changed", lambda *_: self.flow.invalidate_filter())
        self.search.connect("activate", lambda *_: self._launch_first())
        top.append(back)
        top.append(self.search)
        box.append(top)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                min_children_per_line=4, max_children_per_line=4,
                                row_spacing=4, column_spacing=4, valign=Gtk.Align.START)
        self.flow.set_filter_func(self._filter)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.flow)
        box.append(scroll)
        return box

    def _fill_all(self):
        while (c := self.flow.get_first_child()) is not None:
            self.flow.remove(c)
        apps = sorted((a for a in Gio.AppInfo.get_all() if a.should_show()),
                      key=lambda a: a.get_display_name().lower())
        for app in apps:
            b = Gtk.Button(css_classes=["all-app"])
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            img = Gtk.Image(pixel_size=ICON)
            img.set_from_gicon(app.get_icon() or Gio.ThemedIcon.new("application-x-executable"))
            lbl = Gtk.Label(label=app.get_display_name(), ellipsize=Pango.EllipsizeMode.END,
                            max_width_chars=9, css_classes=["all-name"])
            col.append(img)
            col.append(lbl)
            b.set_child(col)
            b.connect("clicked", lambda _b, a=app: self.launch(a))
            child = Gtk.FlowBoxChild(child=b)
            child.app = app
            self.flow.append(child)

    def _filter(self, child):
        q = self.search.get_text().strip().lower()
        if not q:
            return True
        a = child.app
        hay = " ".join([a.get_display_name(), a.get_id(), *(a.get_keywords() if isinstance(a, Gio.DesktopAppInfo) else [])])
        return q in hay.lower()

    def _launch_first(self):
        c = self.flow.get_first_child()
        while c is not None:
            if c.get_child_visible() and self._filter(c):
                self.launch(c.app)
                return
            c = c.get_next_sibling()

    def _show_all(self):
        self._fill_all()
        self.search.set_text("")
        self.stack.set_visible_child_name("all")
        LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.ON_DEMAND)

    def toggle_ram(self):
        if self.stack.get_visible_child_name() == "ram":
            self._show_joy()
        else:
            LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.NONE)
            self.stack.set_visible_child_name("ram")
            self.ram.start()

    def _show_joy(self):
        self.ram.stop()
        LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.NONE)
        self.stack.set_visible_child_name("joy")

    def launch(self, app):
        if app is ALL:
            self._show_all()
            return
        self._show_joy()
        self.usage.bump(app.get_id())
        try:
            ctx = Gdk.Display.get_default().get_app_launch_context()
            ctx.unsetenv("GSK_RENDERER")   # the widget renders with cairo; launched apps keep the GPU
            app.launch([], ctx)
        except GLib.Error as e:
            print("joystick: launch failed:", e.message, flush=True)
            return
        self._raise(app)

    # Launching from here does not fold Phosh's overview (only its own grid does), so wait
    # for the app's window and focus it: activating a toplevel makes Phosh fold the home.
    def _raise(self, app):
        ids = {app.get_id().removesuffix(".desktop").lower()}
        wm = app.get_startup_wm_class() if isinstance(app, Gio.DesktopAppInfo) else None
        if wm:
            ids.add(wm.lower())
        exe = os.path.basename(app.get_executable() or "")
        if exe:
            ids.add(exe.lower())
        before = set(self._toplevels())
        deadline = time.monotonic() + RAISE_TIMEOUT

        def poll():
            now = self._toplevels()
            match = [a for a in now if a.lower() in ids] or [a for a in now if a not in before]
            print("joystick: raise", sorted(ids), now, "->", match, flush=True)
            if match:
                subprocess.Popen(["wlrctl", "toplevel", "focus", "app_id:" + match[0]],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=CLEAN_ENV)
                GLib.timeout_add(FOLD_AFTER_MS, self._fold)
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE if time.monotonic() < deadline else GLib.SOURCE_REMOVE

        GLib.timeout_add(RAISE_POLL_MS, poll)

    # Focusing alone leaves Phosh's overview open on top; Super+A (toggle-application-view)
    # folds it. Only send it while we are still shown, i.e. the overview is still up.
    def _fold(self):
        if self.win.get_visible():
            subprocess.Popen(["wtype", "-M", "logo", "a", "-m", "logo"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=CLEAN_ENV)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _toplevels():
        try:
            out = subprocess.run(["wlrctl", "toplevel", "list"], capture_output=True, text=True,
                                 timeout=2, env=CLEAN_ENV).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        return [ln.split(":", 1)[0].strip() for ln in out.splitlines() if ":" in ln]
