"""Cassette: the vaporwave now-playing tape (alternative to the vinyl).

A clear shell drawn in Gsk, turned sideways for the tall screen. The reels spin like a real
deck (tape at 47.6 mm/s, so the full reel turns slower than the empty one) and the tape winds
from the left reel to the right one as the song plays. Title + artist sit on the label.
Same interface as Vinyl: set_spinning / set_progress / rpm, plus set_text.
"""
import math

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Graphene, Gsk, Gtk, Pango  # noqa: E402
import notareal_theme  # noqa: E402

W, H = 100.0, 64.0                  # shell, in "mm"
HUBS = ((28.75, 30.0), (71.25, 30.0))
CORE, PACK = 10.0, 16.0             # tape radius on an empty / full reel (full pack kept small so the tape runs down at an angle)
SPOOL = 7.5                         # the white spool drawn on top
TAPE_SPEED = 22.0                   # mm/s, about half a real deck: calmer reels
FPS = 30                            # reel redraw cap (static parts are cached render nodes)
SCALE = 0.80                        # share of the widget the tape fills

# micro theme: the same tape as a technical drawing (paper shell, ink hairlines, no glow/gradients).
# Vapor hexes used below -> palette names of the active theme for this look.
MICRO = {"#1c2340": "fg-bright", "#2a1838": "fg-bright", "#0b0e1c": "fg-bright", "#d9c2ff": "fg-bright",
         "#b38cf5": "fg-bright", "#e9e6fb": "surface", "#f4f1ff": "surface", "#f7a1e8": "orange",
         "#ffffff": "bg"}
# other themes (nasa, alien, ...): the vapor tape with its hexes swapped for palette names
TINT = {"#1c2340": "fg-bright", "#2a1838": "fg", "#0b0e1c": "fg-bright", "#d9c2ff": "pink",
        "#b38cf5": "bg-deep", "#6fd6f2": "surface", "#e9e6fb": "accent", "#f4f1ff": "surface",
        "#f7a1e8": "surface", "#ffffff": "glow"}
TINT_ROLES = {"signal": "primary", "ring": "bg", "plate": "bg-deep"}
# roles that differ per look: name -> (vapor hex, micro palette name, micro alpha or None = as given)
ROLES = {"signal": ("#f7a1e8", "orange", None), "ring": ("#f7a1e8", "bg", None),
         "plate": ("#1c2340", "bg-deep", 1.0)}


def rgba(spec, a=1.0):
    c = Gdk.RGBA()
    c.parse(spec)
    c.alpha = a
    return c


def stops(*pairs, col=rgba):
    out = []
    for off, c, a in pairs:
        s = Gsk.ColorStop()
        s.offset = off
        s.color = col(c, a)
        out.append(s)
    return out


class Cassette(Gtk.Widget):
    def __init__(self, natural=480):
        super().__init__()
        self.natural = natural
        self.title, self.artist = "", ""
        self.progress = 0.0
        self.playing = False
        self.rpm = 8.0              # only "0 = don't spin" matters here
        self.motion = 0.0           # 0..1 deck speed, eased (motor spin-up / down)
        self.angles = [0.0, 0.0]
        self.tick = None
        self.last = None
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.micro = False
        self.tint = False
        self.cache = {}             # name -> (key, render node): shell/label/packs, spool, front parts
        self.gen = 0                # bumped on theme change, part of every cache key
        self.drawn = 0.0
        notareal_theme.on_change(self._theme_changed)

    def _theme_changed(self, *_):
        self.gen += 1
        self.queue_draw()

    # --- interface shared with Vinyl ---
    def set_paintable(self, p):
        pass

    def set_text(self, title, artist):
        self.title, self.artist = title or "", artist or ""
        self.queue_draw()

    def set_spinning(self, on):
        self.playing = on
        self._ensure_tick()

    def set_progress(self, frac):
        self.progress = max(0.0, min(1.0, frac))
        self.queue_draw()

    def _ensure_tick(self):
        if self.tick is None:
            self.last = None
            self.tick = self.add_tick_callback(self._on_tick)

    def radii(self):
        """Tape pack radii (left, right); the tape's area moves from one reel to the other."""
        a = PACK * PACK - CORE * CORE
        return (math.sqrt(CORE * CORE + (1 - self.progress) * a), math.sqrt(CORE * CORE + self.progress * a))

    def _on_tick(self, _w, clock):
        t = clock.get_frame_time() / 1e6
        dt = 0 if self.last is None else min(0.1, t - self.last)
        self.last = t
        goal = 1.0 if self.playing and self.rpm > 0 else 0.0
        self.motion += (goal - self.motion) * min(1.0, dt * 3.0)
        for i, r in enumerate(self.radii()):
            self.angles[i] = (self.angles[i] - math.degrees(TAPE_SPEED / r) * self.motion * dt) % 360  # counter-clockwise on screen
        if t - self.drawn >= 1.0 / FPS - 0.004:
            self.drawn = t
            self.queue_draw()
        if goal == 0 and self.motion < 0.01:
            self.motion = 0.0
            self.tick = None
            return False
        return True

    def do_measure(self, orientation, for_size):
        return min(64, self.natural), self.natural, -1, -1

    # --- drawing (landscape units, k = px per mm, the whole thing turned 90°) ---
    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        k = min(w / H, h / W) * SCALE
        if k <= 0.5:
            return
        self.k = k
        look = notareal_theme.value("wallpaper")
        self.micro = look == "micro"
        self.tint = look not in ("vapor", "vaporlight", "micro", "")
        snap.save()
        snap.translate(Graphene.Point().init(w / 2, h / 2))
        snap.rotate(90)
        snap.translate(Graphene.Point().init(-W / 2 * k, -H / 2 * k))
        # only the two spools change per frame; everything else is a cached node
        base = (w, h, self.micro, self.tint, self.gen)
        nodes = [self._cached("back", base + (self.title, self.artist, round(self.progress * 300)), self._draw_back)]
        spool = self._cached("spool", base, self._spool)
        self._append(snap, nodes[0])
        for i, (x, y) in enumerate(HUBS):
            snap.save()
            snap.translate(self.pt(x, y))
            snap.rotate(self.angles[i])
            self._append(snap, spool)
            snap.restore()
        self._append(snap, self._cached("front", base, self._draw_front))
        snap.restore()

    @staticmethod
    def _append(snap, node):
        if node is not None:
            snap.append_node(node)

    def _cached(self, name, key, draw):
        hit = self.cache.get(name)
        if hit and hit[0] == key:
            return hit[1]
        self.snap = Gtk.Snapshot.new()
        draw()
        node = self.snap.to_node()
        self.snap = None
        self.cache[name] = (key, node)
        return node

    def _draw_back(self):
        self._shell()
        self._packs()
        self._label()
        self._window()

    def _draw_front(self):
        self._bottom()
        self._screws()
        if self.micro:
            self._dimensions()
        else:
            self._glint()

    def col(self, spec, a=1.0):
        if self.tint:
            return rgba(notareal_theme.hex(TINT_ROLES.get(spec) or TINT.get(spec, "fg")), a)
        if spec in ROLES:
            vap, name, ma = ROLES[spec]
            return rgba(notareal_theme.hex(name), a if ma is None else ma) if self.micro else rgba(vap, a)
        if self.micro:
            return rgba(notareal_theme.hex(MICRO.get(spec, "fg-bright")), a)
        return rgba(spec, a)

    def rect(self, x, y, ww, hh):
        k = self.k
        return Graphene.Rect().init(x * k, y * k, ww * k, hh * k)

    def rr(self, x, y, ww, hh, rad):
        r = Gsk.RoundedRect()
        sz = Graphene.Size().init(rad * self.k, rad * self.k)
        r.init(self.rect(x, y, ww, hh), sz, sz, sz, sz)
        return r

    def pt(self, x, y):
        return Graphene.Point().init(x * self.k, y * self.k)

    def fill(self, path, col, rule=Gsk.FillRule.WINDING):
        self.snap.append_fill(path, rule, col)

    def stroke(self, path, col, width=0.4):
        st = Gsk.Stroke.new(max(1.0, width * self.k))
        st.set_line_join(Gsk.LineJoin.ROUND)
        st.set_line_cap(Gsk.LineCap.ROUND)
        self.snap.append_stroke(path, st, col)

    def circle(self, x, y, r):
        pb = Gsk.PathBuilder.new()
        pb.add_circle(self.pt(x, y), r * self.k)
        return pb.to_path()

    def rrpath(self, x, y, ww, hh, rad):
        pb = Gsk.PathBuilder.new()
        pb.add_rounded_rect(self.rr(x, y, ww, hh, rad))
        return pb.to_path()

    def _shell(self):
        k, snap = self.k, self.snap
        if self.micro:
            self.fill(self.rrpath(0, 0, W, H, 1.2), self.col("#e9e6fb"))
            self.stroke(self.rrpath(0, 0, W, H, 1.2), self.col("#d9c2ff"), 0.45)
            self.stroke(self.rrpath(1.6, 1.6, W - 3.2, H - 3.2, 0.8), self.col("#d9c2ff", 0.35), 0.18)
            return
        if self.tint:   # soft shadow under a solid body, no glow
            snap.append_outset_shadow(self.rr(0, 0, W, H, 3.2), rgba("#000000", 0.16), 0, 1.5 * k, 0, 5 * k)
            self.fill(self.rrpath(0, 0, W, H, 3.2), self.col("#f4f1ff"))
        else:
            snap.append_outset_shadow(self.rr(0, 0, W, H, 3.2), self.col("#f7a1e8", 0.30), 0, 0, 0, 4 * k)
        snap.push_rounded_clip(self.rr(0, 0, W, H, 3.2))
        snap.append_linear_gradient(self.rect(0, 0, W, H), self.pt(0, 0), self.pt(W, H),
                                    stops((0, "#f7a1e8", 0.22), (0.5, "#b38cf5", 0.16), (1, "#6fd6f2", 0.22), col=self.col))
        snap.pop()
        self.stroke(self.rrpath(0, 0, W, H, 3.2), self.col("#d9c2ff", 0.85), 0.5)
        self.stroke(self.rrpath(1.6, 1.6, W - 3.2, H - 3.2, 2.2), self.col("#d9c2ff", 0.25), 0.3)

    def _packs(self):
        rl, rr_ = self.radii()
        for (x, y), r in zip(HUBS, (rl, rr_)):
            self.fill(self.circle(x, y, r), self.col("#2a1838", 0.82))
            for f in (0.45, 0.7, 0.9):  # faint winding rings
                self.stroke(self.circle(x, y, CORE + (r - CORE) * f), self.col("ring", 0.10), 0.2)
            self.stroke(self.circle(x, y, r), self.col("ring", 0.35), 0.25)
        # tape path: off the OUTER side of the left pack, around the outside of the bottom-left guide,
        # along the heads, around the bottom-right guide and up the outer side of the right pack
        gl, gr, gy = (11.0, 57.0), (89.0, 57.0), 1.6
        pl, pr = (gl[0] - gy, gl[1]), (gr[0] + gy, gr[1])      # outer points of the guides
        pb = Gsk.PathBuilder.new()
        pb.move_to(*self._xy(*self._tangent(HUBS[0], rl, pl, outer_left=True)))
        pb.line_to(*self._xy(*pl))
        pb.quad_to(*self._xy(gl[0] - gy, gl[1] + gy), *self._xy(gl[0], gl[1] + gy))
        pb.line_to(*self._xy(gr[0], gr[1] + gy))
        pb.quad_to(*self._xy(gr[0] + gy, gr[1] + gy), *self._xy(*pr))
        pb.line_to(*self._xy(*self._tangent(HUBS[1], rr_, pr, outer_left=False)))
        self.stroke(pb.to_path(), self.col("#2a1838", 0.9), 0.7)
        for g in (gl, gr):
            self.fill(self.circle(g[0], g[1], gy), self.col("#e9e6fb", 0.75))

    def _xy(self, x, y):
        return x * self.k, y * self.k

    @staticmethod
    def _tangent(c, r, p, outer_left):
        """Tangent point on circle (c, r) for a line to p, on the outer side (left or right of the pack)."""
        dx, dy = p[0] - c[0], p[1] - c[1]
        d = math.hypot(dx, dy)
        if d <= r:
            return c[0] + (-r if outer_left else r), c[1]
        base = math.atan2(dy, dx)
        off = math.acos(r / d)
        cands = [(c[0] + r * math.cos(base + o), c[1] + r * math.sin(base + o)) for o in (off, -off)]
        return min(cands, key=lambda q: q[0]) if outer_left else max(cands, key=lambda q: q[0])

    def _label(self):
        # frosted vapor label with the window cut out
        pb = Gsk.PathBuilder.new()
        pb.add_rounded_rect(self.rr(7, 4, 86, 37, 2))
        pb.add_rounded_rect(self.rr(25.5, 22.5, 49, 15, 2.5))
        path = pb.to_path()
        snap = self.snap
        snap.push_fill(path, Gsk.FillRule.EVEN_ODD)
        if self.micro:   # paper label with the one olive block of the sheet as its top band
            snap.append_color(rgba(notareal_theme.hex("bg")), self.rect(7, 4, 86, 37))
            snap.append_color(rgba(notareal_theme.hex("cyan"), 0.85), self.rect(16.5, 4, 76.5, 2.2))
        else:
            snap.append_linear_gradient(self.rect(7, 4, 86, 37), self.pt(7, 4), self.pt(93, 4),
                                        stops((0, "#f7a1e8", 0.74), (0.5, "#b38cf5", 0.70), (1, "#6fd6f2", 0.74), col=self.col))
            snap.append_color(self.col("#ffffff", 0.10), self.rect(7, 4, 86, 6))
            if self.tint:   # one splash of the theme's main colour
                snap.append_color(rgba(notareal_theme.hex("primary"), 0.9), self.rect(16.5, 4, 76.5, 2.2))
        snap.pop()
        self.stroke(path, self.col("#1c2340", 0.35), 0.25)
        # writing lines + the side letter
        for y in (14.5, 20.0):
            pb = Gsk.PathBuilder.new()
            pb.move_to(*self._xy(16, y))
            pb.line_to(*self._xy(89, y))
            self.stroke(pb.to_path(), self.col("#1c2340", 0.30), 0.18)
        self.fill(self.rrpath(9.5, 6.5, 5, 6.5, 0.8), self.col("#1c2340", 0.85))
        self._text("A", 12.0, 9.75, 4.6, "signal", center=True, bold=True)
        self._text(self.title or "nothing playing", 16.5, 9.4, 4.4, "#1c2340", width=72, bold=True)
        self._text(self.artist, 16.5, 17.3, 3.4, "#1c2340", width=72)
        self._text("notarealPlayer · C-90", 50, 39.2, 2.0, "#1c2340", center=True, alpha=0.6)

    def _text(self, text, x, y, size, col, width=None, center=False, bold=False, alpha=1.0):
        if not text:
            return
        k = self.k
        layout = self.create_pango_layout(text)
        fd = Pango.FontDescription.from_string("JetBrains Mono, monospace" if self.micro
                                               else "CozetteVector, JetBrains Mono, monospace")
        fd.set_absolute_size(size * k * Pango.SCALE)
        if bold:
            fd.set_weight(Pango.Weight.BOLD)
        layout.set_font_description(fd)
        if width:
            layout.set_width(int(width * k * Pango.SCALE))
            layout.set_ellipsize(Pango.EllipsizeMode.END)
        _ink, logical = layout.get_pixel_extents()
        px = x * k - (logical.width / 2 if center else 0)
        py = y * k - logical.height / 2
        self.snap.save()
        self.snap.translate(Graphene.Point().init(px, py))
        self.snap.append_layout(layout, self.col(col, alpha))
        self.snap.restore()

    def _window(self):
        if self.micro:
            self.stroke(self.rrpath(25.5, 22.5, 49, 15, 1.0), self.col("#1c2340"), 0.3)
        else:
            self.fill(self.rrpath(25.5, 22.5, 49, 15, 2.5), self.col("#1c2340", 0.22))
            self.stroke(self.rrpath(25.5, 22.5, 49, 15, 2.5), self.col("#e9e6fb", 0.55), 0.35)
        for i in range(9):  # the little counter scale between the reels
            x = 42 + i * 2
            pb = Gsk.PathBuilder.new()
            pb.move_to(*self._xy(x, 28.5 if i % 4 else 27.5))
            pb.line_to(*self._xy(x, 31.5))
            self.stroke(pb.to_path(), self.col("#e9e6fb", 0.45), 0.18)

    def _spool(self):
        """One spool centred on (0, 0); drawn once, then placed + turned per frame."""
        self.fill(self.circle(0, 0, SPOOL), self.col("#f4f1ff", 0.93))
        self.stroke(self.circle(0, 0, SPOOL), self.col("#b38cf5", 0.8), 0.3)
        snap = self.snap
        snap.save()
        k = self.k
        for i in range(3):  # windows in the spool, so the turning shows
            a = math.radians(i * 120 + 30)
            pb = Gsk.PathBuilder.new()
            pb.add_circle(Graphene.Point().init(5.6 * k * math.cos(a), 5.6 * k * math.sin(a)), 1.15 * k)
            snap.append_fill(pb.to_path(), Gsk.FillRule.WINDING, self.col("#2a1838", 0.75))
        pb = Gsk.PathBuilder.new()
        pb.add_circle(Graphene.Point().init(0, 0), 4.2 * k)
        snap.append_fill(pb.to_path(), Gsk.FillRule.WINDING, self.col("#1c2340", 0.95))
        for i in range(6):  # drive teeth
            snap.save()
            snap.rotate(i * 60)
            pb = Gsk.PathBuilder.new()
            r = Gsk.RoundedRect()
            sz = Graphene.Size().init(0.3 * k, 0.3 * k)
            r.init(Graphene.Rect().init(2.7 * k, -0.6 * k, 1.6 * k, 1.2 * k), sz, sz, sz, sz)
            pb.add_rounded_rect(r)
            snap.append_fill(pb.to_path(), Gsk.FillRule.WINDING, self.col("#f4f1ff", 0.93))
            snap.restore()
        snap.restore()

    def _bottom(self):
        pb = Gsk.PathBuilder.new()
        for i, (x, y) in enumerate(((16.5, 64), (20.5, 46.5), (79.5, 46.5), (83.5, 64))):
            (pb.move_to if i == 0 else pb.line_to)(*self._xy(x, y))
        path = pb.to_path()
        self.fill(path, self.col("plate", 0.28))
        self.stroke(path, self.col("#d9c2ff", 0.55), 0.35)
        for x in (27.0, 73.0):
            self.fill(self.circle(x, 57.5, 2.4), self.col("#0b0e1c", 0.75))
            self.stroke(self.circle(x, 57.5, 2.4), self.col("#d9c2ff", 0.5), 0.25)
        for x, ww in ((37.5, 5.0), (47.0, 6.0), (57.5, 5.0)):
            self.fill(self.rrpath(x, 52.5, ww, 4.5, 0.6), self.col("#0b0e1c", 0.7))

    def _screws(self):
        for x, y in ((4.5, 4.5), (95.5, 4.5), (4.5, 59.5), (95.5, 59.5), (50, 49.5)):
            self.fill(self.circle(x, y, 1.6), self.col("#e9e6fb", 0.55))
            pb = Gsk.PathBuilder.new()
            pb.move_to(*self._xy(x - 1.0, y))
            pb.line_to(*self._xy(x + 1.0, y))
            pb.move_to(*self._xy(x, y - 1.0))
            pb.line_to(*self._xy(x, y + 1.0))
            self.stroke(pb.to_path(), self.col("#1c2340", 0.7), 0.25)

    def _glint(self):
        snap = self.snap
        snap.push_rounded_clip(self.rr(0, 0, W, H, 3.2))
        snap.append_linear_gradient(self.rect(0, 0, W, H), self.pt(20, 0), self.pt(60, H),
                                    stops((0, "#ffffff", 0.0), (0.42, "#ffffff", 0.0), (0.5, "#ffffff", 0.10),
                                          (0.58, "#ffffff", 0.0), (1, "#ffffff", 0.0)))
        snap.pop()

    def _dimensions(self):
        """micro: drawing annotations on the shell: hub centre marks, a dimension line between the hubs."""
        ink = self.col("#1c2340", 0.8)
        for x, y in HUBS:
            for a, b in (((x - 9.5, y), (x - 8.2, y)), ((x + 8.2, y), (x + 9.5, y))):
                pb = Gsk.PathBuilder.new()
                pb.move_to(*self._xy(*a))
                pb.line_to(*self._xy(*b))
                self.stroke(pb.to_path(), ink, 0.15)
        y = 44.2   # between the label and the head area
        pb = Gsk.PathBuilder.new()
        for xa in (HUBS[0][0], HUBS[1][0]):
            pb.move_to(*self._xy(xa, y - 1.4))
            pb.line_to(*self._xy(xa, y + 1.4))
        pb.move_to(*self._xy(HUBS[0][0], y))
        pb.line_to(*self._xy(40.0, y))
        pb.move_to(*self._xy(60.0, y))
        pb.line_to(*self._xy(HUBS[1][0], y))
        self.stroke(pb.to_path(), ink, 0.15)
        self._text(f"{HUBS[1][0] - HUBS[0][0]:.2f}", 50, y, 2.0, "#1c2340", center=True, alpha=0.8)
