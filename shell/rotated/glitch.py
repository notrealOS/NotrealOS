# glitch.py - full-screen vapor glitch overlay for rotated.py.
#
# run(apply): glitch in (screen tears, RGB split, ends almost black), call
# apply() (the rotation, whose modeset blacks the screen), then glitch out
# into the new orientation. Needs gtk4-layer-shell preloaded (see ./rotated).

import random
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

IN_MS = 230      # glitch in, before the rotation
SETTLE_MS = 90   # after the rotation, let the overlay get its new size
OUT_MS = 420     # glitch out, in the new orientation
FLICKER_HZ = 40  # how often the random pattern changes

# vapor palette (same family as crtd / PhantomShell)
COLORS = [
    (0.47, 0.90, 1.00),  # cyan
    (1.00, 0.45, 0.80),  # pink
    (0.72, 0.62, 1.00),  # lavender
    (0.94, 0.98, 1.00),  # white
]
CYAN, PINK = COLORS[0], COLORS[1]


def rgba(rgb, alpha):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = rgb[0], rgb[1], rgb[2], alpha
    return c


def rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


class Noise(Gtk.Widget):
    """k = glitch intensity 0..1."""

    def __init__(self):
        super().__init__()
        self.k = 0.0

    def do_snapshot(self, s):
        W, H, k = self.get_width(), self.get_height(), self.k
        if k <= 0:
            return
        rnd = random.Random(int(time.monotonic() * FLICKER_HZ))
        # darkening; fully black at the peak so the modeset flash is hidden
        s.append_color(rgba((0.02, 0.0, 0.06), min(1.0, k * 1.15) ** 2), rect(0, 0, W, H))
        # torn horizontal slices with an RGB split
        for _ in range(int(3 + 22 * k)):
            y = rnd.uniform(0, H)
            h = rnd.uniform(2, H * (0.012 + 0.05 * k))
            dx = rnd.uniform(-1, 1) * W * 0.18 * k
            split = 4 + 14 * k
            a = rnd.uniform(0.25, 0.85) * k
            s.append_color(rgba(CYAN, a * 0.8), rect(dx - split, y, W, h))
            s.append_color(rgba(PINK, a * 0.8), rect(dx + split, y, W, h))
            s.append_color(rgba(rnd.choice(COLORS), a * 0.6), rect(dx, y, W, h * 0.6))
        # small digital blocks
        for _ in range(int(18 * k)):
            bw, bh = rnd.uniform(8, W * 0.2), rnd.uniform(4, 22)
            s.append_color(rgba(rnd.choice(COLORS), rnd.uniform(0.3, 0.9) * k),
                           rect(rnd.uniform(0, W - bw), rnd.uniform(0, H), bw, bh))
        # scanlines
        line = rgba((0, 0, 0), 0.35 * k)
        y = 0
        while y < H:
            s.append_color(line, rect(0, y, W, 1.5))
            y += 4


class Glitch:
    def __init__(self, app):
        self.win = Gtk.ApplicationWindow(application=app)
        self.win.add_css_class("glitch")
        LayerShell.init_for_window(self.win)
        LayerShell.set_namespace(self.win, "glitch")
        LayerShell.set_layer(self.win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.NONE)
        LayerShell.set_exclusive_zone(self.win, -1)
        for e in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM,
                  LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(self.win, e, True)
        css = Gtk.CssProvider()
        css.load_from_data(b"window.glitch { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, 800)
        self.noise = Noise()
        self.win.set_child(self.noise)
        self.tick = None
        self.busy = False

    def _animate(self, ms, k_of_p, done):
        t0 = []

        def step(widget, clock):
            now = clock.get_frame_time()
            if not t0:
                t0.append(now)
            p = min(1.0, (now - t0[0]) / 1000 / ms)
            self.noise.k = k_of_p(p)
            self.noise.queue_draw()
            if p >= 1:
                self.tick = None
                GLib.idle_add(done)
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        self.tick = self.noise.add_tick_callback(step)

    def run(self, apply):
        if self.busy:
            apply()
            return
        self.busy = True
        self.noise.k = 0
        self.win.present()

        def rotate():
            apply()
            GLib.timeout_add(SETTLE_MS, glitch_out)
            return False

        def glitch_out():
            self.noise.k = 1
            self._animate(OUT_MS, lambda p: (1 - p) ** 1.6, finish)
            return False

        def finish():
            self.win.set_visible(False)
            self.busy = False
            return False

        self._animate(IN_MS, lambda p: p ** 0.8, rotate)


# --- ambient "thinking" glitches (voiced) -----------------------------------------

def _click_through(win):
    """Empty input region: touches go to whatever is below the overlay."""
    import ctypes
    cairo = ctypes.CDLL("libcairo.so.2")
    gtk = ctypes.CDLL("libgtk-4.so.1")
    cairo.cairo_region_create.restype = ctypes.c_void_p
    gtk.gdk_surface_set_input_region.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    get = ctypes.pythonapi.PyCapsule_GetPointer
    get.restype, get.argtypes = ctypes.c_void_p, [ctypes.py_object, ctypes.c_char_p]
    surface = get(win.get_surface().__gpointer__, None)
    gtk.gdk_surface_set_input_region(surface, cairo.cairo_region_create())


class Sparks(Gtk.Widget):
    """Occasional faint tears: a burst of a few slices every BURST_GAP, else nothing."""

    BURST_MS = (60, 160)
    GAP_MS = (500, 1600)

    def __init__(self):
        super().__init__()
        self.burst_until = 0.0
        self.next_burst = 0.0
        self.seed = 0
        self.nyan = Nyan()

    def step(self):
        now = time.monotonic()
        if now >= self.next_burst:
            r = random.Random()
            self.burst_until = now + r.uniform(*self.BURST_MS) / 1000
            self.next_burst = self.burst_until + r.uniform(*self.GAP_MS) / 1000
            self.seed = r.random()
        self.queue_draw()

    def do_snapshot(self, s):
        now = time.monotonic()
        W, H = self.get_width(), self.get_height()
        self.nyan.draw(s, W, H)
        if now >= self.burst_until:
            return
        rnd = random.Random(self.seed + int(now * FLICKER_HZ))
        band = rnd.uniform(0, H)  # slices cluster around one band, like a real tear
        for _ in range(rnd.randint(1, 4)):
            y = band + rnd.uniform(-60, 60)
            h = rnd.uniform(1.5, 9)
            dx = rnd.uniform(-1, 1) * W * 0.06
            a = rnd.uniform(0.12, 0.32)
            s.append_color(rgba(CYAN, a), rect(dx - 5, y, W, h))
            s.append_color(rgba(PINK, a), rect(dx + 5, y, W, h))
        if rnd.random() < 0.4:  # a small digital block now and then
            bw = rnd.uniform(10, W * 0.15)
            s.append_color(rgba(rnd.choice(COLORS), rnd.uniform(0.15, 0.35)),
                           rect(rnd.uniform(0, W - bw), band, bw, rnd.uniform(3, 10)))


class Nyan:
    """Pixel-art poptart cat with a rainbow trail, flying left -> right at a random height.
    Drawn from rectangles in units of U logical px (no image files)."""

    U = 4
    FLIGHT_S = 3.6          # time to cross the screen
    GAP_S = (1.0, 4.0)      # pause between flights
    FRAME_S = 0.07          # leg / tail / rainbow animation step
    RAINBOW = [(1.0, 0.0, 0.0), (1.0, 0.6, 0.0), (1.0, 1.0, 0.0),
               (0.2, 1.0, 0.0), (0.0, 0.6, 1.0), (0.4, 0.2, 1.0)]
    BLACK, TAN, PINK = (0, 0, 0), (1.0, 0.8, 0.6), (1.0, 0.6, 1.0)
    SPRINKLE, GRAY, CHEEK, WHITE = (1.0, 0.2, 0.6), (0.6, 0.6, 0.6), (1.0, 0.6, 0.6), (1, 1, 1)
    SPRINKLES = [(4, 3), (8, 2), (12, 4), (5, 6), (10, 7), (3, 9), (7, 10), (11, 10), (14, 3)]

    def __init__(self, on_flight=None):
        self.t0 = None       # start of the current flight
        self.next = 0.0
        self.y = 0.0
        self.stars = []
        self.on_flight = on_flight  # called when a flight starts (moves the band)

    def schedule(self, delay):
        self.t0 = None
        self.next = time.monotonic() + delay

    def _px(self, s, ox, oy, x, y, w, h, rgb, a=1.0):
        u = self.U
        s.append_color(rgba(rgb, a), rect(ox + x * u, oy + y * u, w * u, h * u))

    def draw(self, s, W, H):
        now = time.monotonic()
        if self.t0 is None:
            if now < self.next:
                return
            self.t0 = now
            if self.on_flight:
                self.on_flight()
            self.y = (H - 16 * self.U) / 2  # centred in the band
            self.stars = [(random.uniform(0, 1), random.uniform(-0.4, 0.4), random.random())
                          for _ in range(7)]
        p = (now - self.t0) / self.FLIGHT_S
        u = self.U
        catw = 22 * u
        trail = W * 0.55
        x = -catw + p * (W + catw + trail)  # left edge of the poptart
        if x - trail > W:
            self.schedule(random.uniform(*self.GAP_S))
            return
        f = int((now - self.t0) / self.FRAME_S)
        bob = u if (f // 2) % 2 else 0
        oy = self.y + bob
        # stars drifting left around the flight path
        for sx, sy, ph in self.stars:
            px = (sx * W * 1.4 - p * W * 0.6) % (W * 1.2) - W * 0.1
            py = H / 2 + sy * H
            on = (f + int(ph * 8)) % 6
            if on < 4:
                self._px(s, px, py, 0, -on // 2, 1, 1 + on, self.WHITE, 0.9)
                self._px(s, px, py, -on // 2, 0, 1 + on, 1, self.WHITE, 0.9)
        # rainbow: 6 stripes, 8u segments alternating up/down by 1u
        seg = 8 * u
        sx = x + 2 * u
        i = 0
        while sx > x - trail and sx > -seg:
            wave = u if (i + f // 2) % 2 else 0
            for k, c in enumerate(self.RAINBOW):
                s.append_color(rgba(c, 0.95), rect(sx - seg, self.y + (1 + 2 * k) * u + wave,
                                                    seg + 1, 2 * u))
            sx -= seg
            i += 1
        ox = x
        # tail (wiggles) and legs (paddle)
        tail_y = 8 + (f % 2)
        self._px(s, ox, oy, -4, tail_y, 5, 2, self.BLACK)
        self._px(s, ox, oy, -3, tail_y, 3, 1, self.GRAY)
        legs = [1, 4, 12, 15]
        for j, lx in enumerate(legs):
            dx = (1 if (f + j) % 2 else 0)
            self._px(s, ox, oy, lx + dx, 13, 3, 3, self.BLACK)
            self._px(s, ox, oy, lx + dx + 1, 13, 1, 2, self.GRAY)
        # poptart: outline, crust, frosting, sprinkles
        self._px(s, ox, oy, 0, 0, 18, 14, self.BLACK)
        self._px(s, ox, oy, 1, 1, 16, 12, self.TAN)
        self._px(s, ox, oy, 2, 2, 14, 10, self.PINK)
        for spx, spy in self.SPRINKLES:
            self._px(s, ox, oy, spx, spy, 1, 1, self.SPRINKLE)
        # head with ears, eyes, cheeks, mouth
        self._px(s, ox, oy, 12, 5, 10, 8, self.BLACK)
        self._px(s, ox, oy, 13, 6, 8, 6, self.GRAY)
        for ex in (12, 19):
            self._px(s, ox, oy, ex, 3, 3, 3, self.BLACK)
            self._px(s, ox, oy, ex + 1, 4, 1, 2, self.GRAY)
        for ex in (14, 19):
            self._px(s, ox, oy, ex, 8, 1, 1, self.BLACK)
            self._px(s, ox, oy, ex, 8, 0.5, 0.5, self.WHITE)
        for cx in (13, 20):
            self._px(s, ox, oy, cx, 10, 1, 1, self.CHEEK)
        self._px(s, ox, oy, 16, 10, 2, 0.5, self.BLACK)


class Thinking:
    """Click-through overlay with subtle glitches + Nyan while start()ed.

    Only a horizontal band (BAND px tall, moved to a random height for every flight), not
    fullscreen: under a fullscreen overlay phoc stops drawing the app windows, and the
    home screen behind them showed through."""

    BAND = 150

    def __init__(self, app):
        self.win = Gtk.ApplicationWindow(application=app)
        self.win.add_css_class("glitch")
        LayerShell.init_for_window(self.win)
        LayerShell.set_namespace(self.win, "thinking")
        LayerShell.set_layer(self.win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.NONE)
        LayerShell.set_exclusive_zone(self.win, -1)
        for e in (LayerShell.Edge.TOP, LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(self.win, e, True)
        self.win.set_default_size(540, self.BAND)  # width: stretched by the left+right anchors
        css = Gtk.CssProvider()
        css.load_from_data(b"window.glitch { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, 800)
        self.sparks = Sparks()
        self.sparks.nyan.on_flight = self._move_band
        self.win.set_child(self.sparks)
        self.win.connect("map", lambda w: _click_through(w))
        self.tick = None

    def _move_band(self):
        mons = self.win.get_display().get_monitors()
        H = mons.get_item(0).get_geometry().height if mons.get_n_items() else 1200
        LayerShell.set_margin(self.win, LayerShell.Edge.TOP,
                              int(random.uniform(0.08, 0.82) * max(H - self.BAND, 1)))

    def start(self):
        if self.tick is not None:
            return
        self._move_band()
        self.sparks.next_burst = time.monotonic() + 0.15
        self.sparks.nyan.schedule(0.8)
        self.win.present()
        self.tick = self.sparks.add_tick_callback(lambda w, c: (w.step(), True)[1])

    def stop(self):
        if self.tick is not None:
            self.sparks.remove_tick_callback(self.tick)
            self.tick = None
        self.win.set_visible(False)


class Listening:
    """Click-through 'SPEAK NOW' band while the voice assistant records (the chirp can be inaudible,
    e.g. when notarealPlayer owns the DAC)."""

    CSS = b"""
    window.listening { background: transparent; }
    .listen-title { font-family: "JetBrains Mono", monospace; font-weight: 800; font-size: 19pt;
                    letter-spacing: 6px; color: #ffffff;
                    text-shadow: 0 0 6px rgba(197, 160, 255, 0.95), 0 0 16px rgba(247, 161, 232, 0.80),
                                 0 0 30px rgba(111, 214, 242, 0.55);
                    animation: listen-glow 1.1s ease-in-out infinite alternate; }
    @keyframes listen-glow { from { opacity: 0.75; } to { opacity: 1.0; } }
    .listen-sub { font-family: "JetBrains Mono", monospace; font-size: 8pt; letter-spacing: 2px;
                  color: #e9e6fb; text-shadow: 0 0 8px rgba(28, 35, 64, 0.95), 0 0 4px rgba(197, 160, 255, 0.6); }
    .ready .listen-title { color: #c5a0ff; animation: none; opacity: 0.85;
                           text-shadow: 0 0 10px rgba(197, 160, 255, 0.55); }
    """

    def __init__(self, app):
        self.win = Gtk.ApplicationWindow(application=app)
        self.win.add_css_class("listening")
        LayerShell.init_for_window(self.win)
        LayerShell.set_namespace(self.win, "listening")
        LayerShell.set_layer(self.win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.NONE)
        LayerShell.set_exclusive_zone(self.win, -1)
        for e in (LayerShell.Edge.TOP, LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(self.win, e, True)
        LayerShell.set_margin(self.win, LayerShell.Edge.TOP, 70)
        css = Gtk.CssProvider()
        css.load_from_data(self.CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, 800)
        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, halign=Gtk.Align.CENTER,
                            margin_top=8, margin_bottom=8)
        self.title = Gtk.Label(xalign=0.5, css_classes=["listen-title"])
        self.sub = Gtk.Label(xalign=0.5, css_classes=["listen-sub"])
        self.card.append(self.title)
        self.card.append(self.sub)
        self.win.set_child(self.card)
        self.win.connect("map", lambda w: _click_through(w))

    def ready(self):
        """Pressed: the cue comes in a moment."""
        self.card.add_css_class("ready")
        self.title.set_label("GET READY…")
        self.sub.set_label("keep holding volume up")
        self.win.present()

    def listen(self):
        self.card.remove_css_class("ready")
        self.title.set_label("SPEAK NOW")
        self.sub.set_label("release volume up when done")
        self.win.present()

    def hide(self):
        self.win.set_visible(False)
