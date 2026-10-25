"""Code-drawn parts of notarealWeather (GSK, no cairo on the phone): dot-matrix weather icons and
the Nothing-style tile graphics. Colours come from the active notareal theme on every draw, so a
theme switch only needs queue_draw().
"""
import math

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Graphene, Gsk, Gtk, Pango  # noqa: E402

import notareal_theme as T  # noqa: E402

# 9x9 dot icons, '#' = lit dot (the Nothing weather app's dot-matrix look)
DOTS = {
    "sun": """
....#....
.#.....#.
...###...
..#####..
#.#####.#
..#####..
...###...
.#.....#.
....#....""",
    "moon": """
..####...
.###.....
###......
###......
###......
###......
###......
.###.....
..####...""",
    "cloud": """
.........
.........
...###...
..#####..
.#######.
#########
#########
.#######.
.........""",
    "partly": """
......#..
.#...###.
....#####
..###.###
.#######.
#########
#########
.#######.
.........""",
    "partly-night": """
.....###.
....##...
..###....
.#####...
.#######.
#########
#########
.#######.
.........""",
    "fog": """
.........
#######..
.........
..#######
.........
#######..
.........
..#######
.........""",
    "drizzle": """
...###...
..#####..
.#######.
#########
.#######.
.........
..#...#..
.........
....#....""",
    "rain": """
...###...
..#####..
.#######.
#########
.#######.
.........
.#..#..#.
#..#..#..
.........""",
    "snow": """
....#....
.#..#..#.
..#.#.#..
...###...
#########
...###...
..#.#.#..
.#..#..#.
....#....""",
    "thunder": """
...###...
..#####..
.#######.
#########
.#######.
....##...
...##....
....##...
...#.....""",
}
GRIDS = {k: [r for r in v.strip("\n").split("\n")] for k, v in DOTS.items()}


def pt(x, y):
    return Graphene.Point().init(x, y)


def circle(x, y, r):
    pb = Gsk.PathBuilder.new()
    pb.add_circle(pt(x, y), r)
    return pb.to_path()


def stroke(snap, path, col, width=1.2, cap=Gsk.LineCap.ROUND):
    st = Gsk.Stroke.new(width)
    st.set_line_cap(cap)
    st.set_line_join(Gsk.LineJoin.ROUND)
    snap.append_stroke(path, st, col)


def fill(snap, path, col):
    snap.append_fill(path, Gsk.FillRule.WINDING, col)


def line(snap, x1, y1, x2, y2, col, width=1.2):
    pb = Gsk.PathBuilder.new()
    pb.move_to(x1, y1)
    pb.line_to(x2, y2)
    stroke(snap, pb.to_path(), col, width)


def arc_path(cx, cy, r, a0, a1, steps=48):
    """Polyline arc, angles in degrees (0 = east, clockwise on screen)."""
    pb = Gsk.PathBuilder.new()
    for i in range(steps + 1):
        a = math.radians(a0 + (a1 - a0) * i / steps)
        (pb.move_to if i == 0 else pb.line_to)(cx + r * math.cos(a), cy + r * math.sin(a))
    return pb.to_path()


def ellipse(cx, cy, rx, ry, steps=60):
    pb = Gsk.PathBuilder.new()
    for i in range(steps + 1):
        a = 2 * math.pi * i / steps
        (pb.move_to if i == 0 else pb.line_to)(cx + rx * math.cos(a), cy + ry * math.sin(a))
    pb.close()
    return pb.to_path()


def ink(a=1.0):
    return T.color("fg-bright", a)


def accent(a=1.0):
    return T.color("orange", a)


def text(widget, snap, s, x, y, size, col, center=True, bold=False):
    layout = widget.create_pango_layout(s)
    fam = T.value("font-code", "monospace").replace('"', "")
    fd = Pango.FontDescription.from_string(fam)
    fd.set_absolute_size(size * Pango.SCALE)
    if bold:
        fd.set_weight(Pango.Weight.BOLD)
    layout.set_font_description(fd)
    _ink, lg = layout.get_pixel_extents()
    snap.save()
    snap.translate(pt(x - (lg.width / 2 if center else 0), y - lg.height / 2))
    snap.append_layout(layout, col)
    snap.restore()


class Canvas(Gtk.Widget):
    """A widget drawn by paint(snap, w, h)."""
    def __init__(self, w=-1, h=-1):
        super().__init__()
        self.set_size_request(w, h)
        self.set_can_target(False)

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        if w > 0 and h > 0:
            self.paint(snap, w, h)

    def paint(self, snap, w, h):
        pass


class DotIcon(Canvas):
    def __init__(self, kind="cloud", size=26, dim=False):
        super().__init__(size, size)
        self.kind, self.dim = kind, dim
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)

    def set_kind(self, k):
        self.kind = k
        self.queue_draw()

    def paint(self, snap, w, h):
        g = GRIDS.get(self.kind, GRIDS["cloud"])
        n = len(g)
        step = min(w, h) / n
        r = step * 0.42
        col = ink(0.55 if self.dim else 1.0)
        pb = Gsk.PathBuilder.new()
        for y, row in enumerate(g):
            for x, c in enumerate(row[:n]):
                if c == "#":
                    pb.add_circle(pt((x + 0.5) * step, (y + 0.5) * step), r)
        fill(snap, pb.to_path(), col)


class Disc(Canvas):
    """The big filled current-temperature circle."""
    def paint(self, snap, w, h):
        r = min(w, h) / 2
        fill(snap, circle(w / 2, h / 2, r), ink())


class AqiGauge(Canvas):
    """Half dial with ticks and an arrow needle; value 0..100 (European AQI)."""
    value = 0

    def paint(self, snap, w, h):
        base = h * 0.78
        r = min(w * 0.36, h * 0.62)
        cx = w / 2
        line(snap, 0, base, w, base, ink(), 1.4)
        stroke(snap, arc_path(cx, base, r, 180, 360), ink(), 1.4)
        for i in range(21):
            a = math.radians(180 + i * 9)
            r0 = r * (0.80 if i % 5 == 0 else 0.87)
            line(snap, cx + r0 * math.cos(a), base + r0 * math.sin(a),
                 cx + r * 0.97 * math.cos(a), base + r * 0.97 * math.sin(a), ink(0.85 if i % 5 == 0 else 0.5), 1.1)
        v = max(0.0, min(100.0, float(self.value or 0)))
        a = math.radians(180 + v * 1.8)
        tx, ty = cx + r * 0.62 * math.cos(a), base + r * 0.62 * math.sin(a)
        col = accent() if v > 60 else ink()
        line(snap, cx, base, tx, ty, col, 1.6)
        for side in (-25, 25):        # arrow head
            b = a + math.pi + math.radians(side)
            line(snap, tx, ty, tx + 9 * math.cos(b), ty + 9 * math.sin(b), col, 1.6)


class HumidityDial(Canvas):
    """Outline ring with a filled disc whose size is the humidity."""
    value = 0

    def paint(self, snap, w, h):
        R = min(w, h) / 2 - 2
        stroke(snap, circle(w / 2, h / 2, R), ink(0.8), 1.0)
        f = max(0.05, min(1.0, (self.value or 0) / 100))
        fill(snap, circle(w / 2, h / 2, R * 0.92 * math.sqrt(f)), ink())


class WindCompass(Canvas):
    """Ring with N marker, filled centre and the wind arrow (blowing TOWARD)."""
    direction = 0
    speed = 0

    def paint(self, snap, w, h):
        cx, cy = w / 2, h / 2
        R = min(w, h) / 2 - 6
        stroke(snap, circle(cx, cy, R), ink(), 1.3)
        stroke(snap, circle(cx, cy, R * 0.86), ink(0.35), 1.0)
        pb = Gsk.PathBuilder.new()        # N tick
        pb.move_to(cx, cy - R - 6)
        pb.line_to(cx - 4, cy - R)
        pb.line_to(cx + 4, cy - R)
        pb.close()
        fill(snap, pb.to_path(), ink())
        text(self, snap, "N", cx, cy - R * 0.72, R * 0.16, ink(), bold=True)
        fill(snap, circle(cx, cy, R * 0.55), ink())
        a = math.radians((self.direction or 0) + 180 - 90)   # meteorological "from" -> screen "to"
        L = R * 0.62
        x1, y1 = cx - L * math.cos(a), cy - L * math.sin(a)
        x2, y2 = cx + L * math.cos(a), cy + L * math.sin(a)
        bg = T.color("surface")
        line(snap, x1, y1, x2, y2, bg, 4.5)
        line(snap, x1, y1, x2, y2, accent() if (self.speed or 0) >= 39 else ink(), 1.6)
        for side in (-28, 28):
            b = a + math.pi + math.radians(side)
            line(snap, x2, y2, x2 + 10 * math.cos(b), y2 + 10 * math.sin(b),
                 accent() if (self.speed or 0) >= 39 else ink(), 1.6)


class UvStack(Canvas):
    """Sphere slices sitting on a base ellipse; brighter slices = higher UV."""
    value = 0

    def paint(self, snap, w, h):
        cx = w / 2
        R = min(w * 0.42, h * 0.5 - 6)
        base = h - 6
        n = 7
        lit = max(0, min(11, round(self.value or 0)))
        for i in range(n, 0, -1):          # nested spheres squashed onto the base
            ry = R * i / n
            on = lit and i <= max(1, round(lit / 11 * n))
            stroke(snap, ellipse(cx, base - ry, R, ry),
                   (accent() if lit >= 8 else ink()) if on else ink(0.3), 1.0)
        stroke(snap, ellipse(cx, base, R, 4), ink(), 2.0)


class SunArc(Canvas):
    """Day arc from sunrise to sunset with the sun dot at `pos` (0..1, None = night)."""
    pos = None

    def paint(self, snap, w, h):
        base = h * 0.82
        r = min(w * 0.42, h * 0.72)
        cx = w / 2
        line(snap, 0, base, w, base, ink(0.6), 1.0)
        pb = Gsk.PathBuilder.new()
        dashes = 30
        for i in range(dashes):
            if i % 2 == 0:
                a0 = math.radians(180 + 180 * i / dashes)
                a1 = math.radians(180 + 180 * (i + 1) / dashes)
                pb.move_to(cx + r * math.cos(a0), base + r * math.sin(a0))
                pb.line_to(cx + r * math.cos(a1), base + r * math.sin(a1))
        stroke(snap, pb.to_path(), ink(0.7), 1.2)
        if self.pos is not None:
            p = max(0.0, min(1.0, self.pos))
            stroke(snap, arc_path(cx, base, r, 180, 180 + 180 * p), ink(), 2.0)
            a = math.radians(180 + 180 * p)
            fill(snap, circle(cx + r * math.cos(a), base + r * math.sin(a), 8), accent())


class MoonDisc(Canvas):
    """Moon phase: lit part filled, dark part outlined. phase 0 new .. .5 full .. 1 new."""
    phase = 0.5

    def paint(self, snap, w, h):
        cx, cy = w / 2, h / 2
        R = min(w, h) / 2 - 3
        stroke(snap, circle(cx, cy, R), ink(0.6), 1.0)
        p = self.phase % 1.0
        waxing = p < 0.5
        k = math.cos(2 * math.pi * p)          # 1 new, -1 full: terminator x-scale
        pb = Gsk.PathBuilder.new()
        steps = 40
        side = 1 if waxing else -1             # lit limb on the right while waxing
        for i in range(steps + 1):
            t = math.pi * (-0.5 + i / steps)
            pb.line_to(cx + side * R * math.cos(t), cy + R * math.sin(t)) if i else \
                pb.move_to(cx + side * R * math.cos(t), cy + R * math.sin(t))
        for i in range(steps + 1):
            t = math.pi * (0.5 - i / steps)
            pb.line_to(cx + side * R * k * math.cos(t), cy + R * math.sin(t))
        pb.close()
        fill(snap, pb.to_path(), ink())


class PressureGauge(Canvas):
    """Arc 960..1060 hPa with ticks and a dot."""
    value = 1013

    def paint(self, snap, w, h):
        cx, cy = w / 2, h * 0.58
        R = min(w * 0.42, h * 0.5)
        for i in range(41):
            a = math.radians(135 + i * 270 / 40)
            r0 = R * (0.80 if i % 10 == 0 else 0.88)
            line(snap, cx + r0 * math.cos(a), cy + r0 * math.sin(a),
                 cx + R * math.cos(a), cy + R * math.sin(a), ink(0.9 if i % 10 == 0 else 0.4), 1.1)
        v = max(0.0, min(1.0, ((self.value or 1013) - 960) / 100))
        a = math.radians(135 + v * 270)
        fill(snap, circle(cx + R * 0.66 * math.cos(a), cy + R * 0.66 * math.sin(a), 6), ink())
        line(snap, cx, cy, cx + R * 0.5 * math.cos(a), cy + R * 0.5 * math.sin(a), ink(), 1.4)
        fill(snap, circle(cx, cy, 3), ink())


class Thermo(Canvas):
    """Feels-like: vertical dotted column, lit up to the value on a -30..40 scale."""
    value = 0
    lo, hi = -30, 40

    def paint(self, snap, w, h):
        n = 12
        step = (h - 8) / n
        r = step * 0.32
        lit = round((max(self.lo, min(self.hi, self.value or 0)) - self.lo) / (self.hi - self.lo) * n)
        pb_on, pb_off = Gsk.PathBuilder.new(), Gsk.PathBuilder.new()
        for col in range(3):
            x = w / 2 + (col - 1) * step
            for i in range(n):
                y = h - 4 - (i + 0.5) * step
                (pb_on if i < lit else pb_off).add_circle(pt(x, y), r)
        fill(snap, pb_off.to_path(), ink(0.18))
        fill(snap, pb_on.to_path(), accent() if (self.value or 0) >= 30 else ink())


class Bucket(Canvas):
    """Precipitation: a dot grid filled from the bottom (0..20 mm full)."""
    value = 0
    full = 20.0

    def paint(self, snap, w, h):
        cols, rows = 7, 7
        step = min(w / cols, h / rows)
        r = step * 0.34
        ox = (w - step * cols) / 2
        lit = math.ceil(min(1.0, (self.value or 0) / self.full) * cols * rows)
        on, off = Gsk.PathBuilder.new(), Gsk.PathBuilder.new()
        k = 0
        for row in range(rows - 1, -1, -1):
            for c in range(cols):
                (on if k < lit else off).add_circle(pt(ox + (c + 0.5) * step, (row + 0.5) * step), r)
                k += 1
        fill(snap, off.to_path(), ink(0.18))
        fill(snap, on.to_path(), ink())


class EyeLines(Canvas):
    """Visibility: horizon lines, more of them lit the further you can see (0..20 km)."""
    value = 0

    def paint(self, snap, w, h):
        n = 7
        lit = round(min(1.0, (self.value or 0) / 20000) * n)
        for i in range(n):
            y = h * (0.15 + 0.7 * i / (n - 1))
            inset = w * 0.06 * (n - 1 - i)
            line(snap, inset, y, w - inset, y, ink() if (n - 1 - i) < lit else ink(0.2), 1.4)


class Bars(Canvas):
    """Dot bars, e.g. the 2-hour rain nowcast or pollen levels. values 0..1."""
    def __init__(self, h=60):
        super().__init__(-1, h)
        self.values = []

    def paint(self, snap, w, h):
        if not self.values:
            return
        n = len(self.values)
        rows = 6
        step = min(w / n, h / rows)
        r = step * 0.3
        on, off = Gsk.PathBuilder.new(), Gsk.PathBuilder.new()
        for i, v in enumerate(self.values):
            lit = math.ceil(max(0.0, min(1.0, v)) * rows) if v else 0
            x = (i + 0.5) * w / n
            for j in range(rows):
                (on if j < lit else off).add_circle(pt(x, h - (j + 0.5) * step), r)
        fill(snap, off.to_path(), ink(0.15))
        fill(snap, on.to_path(), ink())


class TempChart(Canvas):
    """Day page: temperature line + precipitation-chance bars over the hours of one day."""
    def __init__(self, h=180):
        super().__init__(-1, h)
        self.temps, self.pops, self.labels = [], [], []

    def paint(self, snap, w, h):
        if len(self.temps) < 2:
            return
        top, bottom = 26, h - 26
        lo, hi = min(self.temps), max(self.temps)
        span = max(hi - lo, 1)
        n = len(self.temps)
        xs = [12 + (w - 24) * i / (n - 1) for i in range(n)]
        for x, p in zip(xs, self.pops):
            if p:
                bh = (bottom - top) * 0.5 * p / 100
                snap.append_color(ink(0.18), Graphene.Rect().init(x - 3, bottom - bh, 6, bh))
        pb = Gsk.PathBuilder.new()
        ys = [bottom - 10 - (bottom - top - 20) * (t - lo) / span for t in self.temps]
        for i, (x, y) in enumerate(zip(xs, ys)):
            (pb.move_to if i == 0 else pb.line_to)(x, y)
        stroke(snap, pb.to_path(), ink(), 2.0)
        for i in range(0, n, 3):
            fill(snap, circle(xs[i], ys[i], 3), ink())
            text(self, snap, f"{round(self.temps[i])}°", xs[i], ys[i] - 14, 11, ink())
            if i < len(self.labels):
                text(self, snap, self.labels[i], xs[i], h - 10, 10, ink(0.6))
        i_hi = self.temps.index(hi)
        fill(snap, circle(xs[i_hi], ys[i_hi], 5), accent())
