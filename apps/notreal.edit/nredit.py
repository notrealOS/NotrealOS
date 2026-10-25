#!/usr/bin/env python3
"""notarealEdit: a small photo editor - crop, curves, saturation, blur, vignette, then save a copy or share.

Edits preview live on a downscaled copy; SAVE renders the full image in the background and
writes <name>_edit.jpg next to the original (the original is never overwritten).
Left-handed: tool tabs and SAVE / SHARE sit on the left.
"""
import math
import os
import subprocess
import sys
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango  # noqa: E402

import numpy as np  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.expanduser("~/theme"))
import notareal_theme as theme  # noqa: E402

APP_ID = "dev.notareal.Edit"
PREVIEW_PX = 1400
CURVE_X = [0.0, 0.25, 0.5, 0.75, 1.0]
RATIOS = [("FREE", None), ("1:1", 1.0), ("4:3", 4 / 3), ("3:4", 3 / 4), ("16:9", 16 / 9)]
MARK_TOOLS = [("PEN", "pen"), ("HILITE", "hl"), ("ARROW", "arrow"), ("BOX", "box"), ("TEXT", "text")]
MARK_COLORS = ["#ff3b30", "#ffcc00", "#ffffff", "#111111", "#2f80ff", "#34c759"]
MARK_SIZES = [("S", 0.004), ("M", 0.008), ("L", 0.016)]     # line width, share of the long side


# --- image maths ---------------------------------------------------------------------------------

def to_array(pb):
    pb = pb.apply_embedded_orientation() or pb
    w, h, rs, nc = pb.get_width(), pb.get_height(), pb.get_rowstride(), pb.get_n_channels()
    a = np.frombuffer(pb.get_pixels(), np.uint8)
    return np.lib.stride_tricks.as_strided(a, (h, w, 3), (rs, nc, 1)).copy()


def to_texture(a):
    h, w = a.shape[:2]
    return Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(np.ascontiguousarray(a).tobytes()), w * 3)


def monotone_lut(ys):
    """256-entry LUT through (CURVE_X, ys), monotone cubic (Fritsch-Carlson): no overshoot."""
    x = np.array(CURVE_X)
    y = np.clip(np.array(ys, float), 0, 1)
    d = np.diff(y) / np.diff(x)
    m = np.zeros_like(y)
    m[0], m[-1] = d[0], d[-1]
    for i in range(1, len(y) - 1):
        m[i] = 0 if d[i - 1] * d[i] <= 0 else (d[i - 1] + d[i]) / 2
    for i in range(len(d)):
        if d[i] == 0:
            m[i] = m[i + 1] = 0
        else:
            a, b = m[i] / d[i], m[i + 1] / d[i]
            s = a * a + b * b
            if s > 9:
                t = 3 / np.sqrt(s)
                m[i], m[i + 1] = t * a * d[i], t * b * d[i]
    t = np.linspace(0, 1, 256)
    idx = np.clip(np.searchsorted(x, t, side="right") - 1, 0, len(x) - 2)
    h = x[idx + 1] - x[idx]
    u = (t - x[idx]) / h
    h00, h10, h01, h11 = 2 * u**3 - 3 * u**2 + 1, u**3 - 2 * u**2 + u, -2 * u**3 + 3 * u**2, u**3 - u**2
    out = h00 * y[idx] + h10 * h * m[idx] + h01 * y[idx + 1] + h11 * h * m[idx + 1]
    return np.clip(out * 255 + 0.5, 0, 255).astype(np.uint8)


def box_blur(a, k, axis):
    """One box-blur pass of radius k along axis (edge-padded), float32 in/out."""
    pad = [(0, 0)] * a.ndim
    pad[axis] = (k + 1, k)
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float32)
    n = a.shape[axis]
    if axis == 0:
        return (c[2 * k + 1:2 * k + 1 + n] - c[:n]) * (1.0 / (2 * k + 1))
    return (c[:, 2 * k + 1:2 * k + 1 + n] - c[:, :n]) * (1.0 / (2 * k + 1))


def blurred(a, amount):
    """Gaussian-like blur whose radius is a share of the picture size (so the small preview and
    the full-size save look the same): shrink, 3 box passes each way, scale back up. Light on RAM."""
    h, w = a.shape[:2]
    r = amount * 0.025 * max(w, h)          # up to 2.5 % of the long side
    d = max(2.0, r / 3)                      # work on a copy where the radius is ~3 px
    sw, sh = max(1, int(w / d)), max(1, int(h / d))
    pb = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(np.ascontiguousarray(a).tobytes()),
                                         GdkPixbuf.Colorspace.RGB, False, 8, w, h, w * 3)
    small = to_array(pb.scale_simple(sw, sh, GdkPixbuf.InterpType.BILINEAR)).astype(np.float32)
    k = max(1, round(r / d / 1.7))           # 3 box passes of radius k ~ gaussian sigma r/d
    for axis in (0, 1):
        for _ in range(3):
            small = box_blur(small, k, axis)
    small = np.clip(small + 0.5, 0, 255).astype(np.uint8)
    spb = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(small.tobytes()), GdkPixbuf.Colorspace.RGB,
                                          False, 8, sw, sh, sw * 3)
    return to_array(spb.scale_simple(w, h, GdkPixbuf.InterpType.BILINEAR))


def radial(h, w, y0, y1):
    """Rows y0..y1 of the distance from the centre: 0 middle, 1 at the corners."""
    ys = (np.arange(y0, y1, dtype=np.float32) + 0.5) / h * 2 - 1
    xs = (np.arange(w, dtype=np.float32) + 0.5) / w * 2 - 1
    return np.sqrt(ys[:, None] ** 2 + xs[None, :] ** 2) / np.sqrt(2)


def smooth(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


_masks = {}


def mask(h, w, y0, y1, e0, e1):
    """smooth(e0, e1, radial) rows y0..y1, as HxWx1 float32; whole preview-size masks are cached
    (slider drags redraw the same size over and over)."""
    if h * w > 3_000_000:
        return smooth(e0, e1, radial(h, w, y0, y1))[..., None]
    key = (h, w, e0, e1)
    if key not in _masks:
        if len(_masks) > 6:
            _masks.clear()
        _masks[key] = smooth(e0, e1, radial(h, w, 0, h))[..., None]
    return _masks[key][y0:y1]


def blur_vignette(out, state):
    """Blur (whole picture, or EDGES: sharp centre fading into blur) then vignette, in place."""
    amount, edges, vig = state["blur"], state["blur_edges"], state["vig"]
    h, w = out.shape[:2]
    if amount > 0.01:
        b = blurred(out, amount)
        if not edges:
            out[:] = b
        else:
            for y in range(0, h, 512):
                m = mask(h, w, y, min(y + 512, h), 0.25, 0.75)
                o = out[y:y + 512].astype(np.float32)
                out[y:y + 512] = np.clip(o + (b[y:y + 512] - o) * m + 0.5, 0, 255).astype(np.uint8)
        del b
    if abs(vig) > 0.01:
        target = 0.0 if vig > 0 else 255.0   # right = dark corners, left = white corners
        for y in range(0, h, 512):
            m = mask(h, w, y, min(y + 512, h), 0.3, 1.05) * (abs(vig) * 0.9)
            o = out[y:y + 512].astype(np.float32)
            out[y:y + 512] = np.clip(o + (target - o) * m + 0.5, 0, 255).astype(np.uint8)
    return out


def render(a, state):
    """rotate -> crop -> curves -> saturation -> blur -> vignette. a: HxWx3 uint8.
    The crop box is in the ROTATED picture's coordinates (what the crop frame shows)."""
    rot = state["rot"] % 4
    if rot:
        a = np.rot90(a, k=-rot)            # rot = quarter turns clockwise
    x0, y0, x1, y1 = state["crop"]
    h, w = a.shape[:2]
    a = a[int(y0 * h):max(int(y1 * h), int(y0 * h) + 1), int(x0 * w):max(int(x1 * w), int(x0 * w) + 1)]
    master = monotone_lut(state["curves"]["rgb"])
    luts = [master[monotone_lut(state["curves"][c])] for c in "rgb"]
    out = np.empty_like(a)
    for c in range(3):
        out[..., c] = luts[c][a[..., c]]
    sat = state["sat"]
    if abs(sat - 1) > 1e-3:
        for y in range(0, out.shape[0], 512):          # strips: 12 MP floats are big
            f = out[y:y + 512].astype(np.float32)
            l = f @ np.array([0.299, 0.587, 0.114], np.float32)
            f = l[..., None] + (f - l[..., None]) * sat
            out[y:y + 512] = np.clip(f + 0.5, 0, 255).astype(np.uint8)
    return blur_vignette(out, state)


def default_state():
    return {"crop": (0.0, 0.0, 1.0, 1.0), "rot": 0, "sat": 1.0, "blur": 0.0, "blur_edges": False, "vig": 0.0,
            "curves": {c: list(CURVE_X) for c in ("rgb", "r", "g", "b")}, "marks": []}


# --- markup ---------------------------------------------------------------------------------------
# A mark: {"kind": pen|hl|arrow|box|text, "color", "size", "pts", "text"}. Points live in the rotated,
# UNCROPPED picture's 0..1 space (like the crop box); size is a share of its long side. So marks stay
# on the same spot when the crop changes, and the small preview and the full save draw them alike.

def draw_markup(snap, marks, crop, frame, layout_for):
    """Draw marks onto frame = (x, y, w, h), where the CROPPED picture sits."""
    if not marks:
        return
    cx0, cy0, cx1, cy1 = crop
    fx, fy, fw, fh = frame
    sx, sy = fw / max(cx1 - cx0, 1e-6), fh / max(cy1 - cy0, 1e-6)    # the whole picture's size
    ref = max(sx, sy)
    pt = lambda p: (fx + (p[0] - cx0) * sx, fy + (p[1] - cy0) * sy)
    snap.push_clip(rect(fx, fy, fw, fh))
    for m in marks:
        col = Gdk.RGBA()
        col.parse(m["color"])
        lw = max(1.0, m["size"] * ref)
        pts = [pt(p) for p in m["pts"]]
        if m["kind"] == "text":
            px = lw * 6
            layout = layout_for(m["text"], px)
            tw, th = layout.get_pixel_size()
            shadow = Gdk.RGBA()
            shadow.parse("rgba(255,255,255,0.6)" if m["color"] == MARK_COLORS[3] else "rgba(0,0,0,0.6)")
            for c, off in ((shadow, max(1.0, px * 0.05)), (col, 0.0)):
                snap.save()
                snap.translate(Graphene.Point().init(pts[0][0] - tw / 2 + off, pts[0][1] - th / 2 + off))
                snap.append_layout(layout, c)
                snap.restore()
            continue
        pb = Gsk.PathBuilder.new()
        (ax, ay), (bx, by) = pts[0], pts[-1]
        if m["kind"] in ("pen", "hl"):
            pb.move_to(ax, ay)
            for p in pts[1:]:
                pb.line_to(*p)
            if len(pts) == 1:
                pb.line_to(ax + 0.01, ay)            # a tap = a dot
        elif m["kind"] == "arrow":
            pb.move_to(ax, ay)
            pb.line_to(bx, by)
            ang, head = math.atan2(by - ay, bx - ax), lw * 4.5
            for s in (-1, 1):
                pb.move_to(bx, by)
                pb.line_to(bx - head * math.cos(ang + s * 0.45), by - head * math.sin(ang + s * 0.45))
        else:
            pb.add_rect(rect(min(ax, bx), min(ay, by), abs(bx - ax), abs(by - ay)))
        if m["kind"] == "hl":                         # highlighter: wide and see-through
            lw *= 4
            col.alpha = 0.4
        stroke = Gsk.Stroke.new(lw)
        stroke.set_line_cap(Gsk.LineCap.ROUND)
        stroke.set_line_join(Gsk.LineJoin.ROUND)
        snap.append_stroke(pb.to_path(), stroke, col)
    snap.pop()


def composite(out, layer):
    """Lay an HxWx4 RGBA (straight alpha) layer over out, in place."""
    for y in range(0, out.shape[0], 512):
        m = layer[y:y + 512]
        a = m[..., 3:4].astype(np.float32) * (1 / 255)
        o = out[y:y + 512].astype(np.float32)
        out[y:y + 512] = np.clip(o + (m[..., :3] - o) * a + 0.5, 0, 255).astype(np.uint8)
    return out


def rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


def rgba(name, a=1.0):
    return theme.color(name, a)


# --- the picture (with the crop frame) ------------------------------------------------------------

class Canvas(Gtk.Widget):
    __gtype_name__ = "NrEditCanvas"

    def __init__(self, editor):
        super().__init__(hexpand=True, vexpand=True)
        self.editor = editor
        self.texture = None
        self.full_tex = None          # the uncropped preview while cropping
        self.cropping = False
        self.marking = False
        self.ratio = None
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        self.add_controller(drag)
        self._grab = None

    def layout(self, text, px):
        lay = self.create_pango_layout(text)
        fd = Pango.FontDescription.from_string("Sans Bold")
        fd.set_absolute_size(px * Pango.SCALE)
        lay.set_font_description(fd)
        return lay

    def fit(self, tw, th):
        w, h = self.get_width(), self.get_height()
        s = min(w / tw, h / th) if tw and th else 1
        dw, dh = tw * s, th * s
        return (w - dw) / 2, (h - dh) / 2, dw, dh

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        tex = self.full_tex if self.cropping else self.texture
        if not tex:
            return
        x, y, dw, dh = self.fit(tex.get_width(), tex.get_height())
        snap.append_texture(tex, rect(x, y, dw, dh))
        draw_markup(snap, self.editor.state["marks"],
                    (0.0, 0.0, 1.0, 1.0) if self.cropping else self.editor.state["crop"], (x, y, dw, dh), self.layout)
        if not self.cropping:
            return
        cx0, cy0, cx1, cy1 = self.editor.state["crop"]
        L, T, R, B = x + cx0 * dw, y + cy0 * dh, x + cx1 * dw, y + cy1 * dh
        dim = Gdk.RGBA()
        dim.parse("rgba(0,0,0,0.55)")
        for r in ((x, y, dw, T - y), (x, B, dw, y + dh - B), (x, T, L - x, B - T), (R, T, x + dw - R, B - T)):
            if r[2] > 0 and r[3] > 0:
                snap.append_color(dim, rect(*r))
        line = rgba("primary")
        for r in ((L, T, R - L, 2), (L, B - 2, R - L, 2), (L, T, 2, B - T), (R - 2, T, 2, B - T)):
            snap.append_color(line, rect(*r))
        thirds = rgba("fg-bright", 0.35)
        for k in (1, 2):
            snap.append_color(thirds, rect(L + (R - L) * k / 3, T, 1, B - T))
            snap.append_color(thirds, rect(L, T + (B - T) * k / 3, R - L, 1))
        for hx, hy in ((L, T), (R, T), (L, B), (R, B)):
            snap.append_color(line, rect(hx - 9, hy - 9, 18, 18))

    def _frame(self):
        tex = self.full_tex
        return self.fit(tex.get_width(), tex.get_height()) if tex else (0, 0, 1, 1)

    def _to_pic(self, sx, sy):
        """Screen point -> the rotated, uncropped picture's 0..1 space (where marks live)."""
        x, y, dw, dh = self.fit(self.texture.get_width(), self.texture.get_height())
        cx0, cy0, cx1, cy1 = self.editor.state["crop"]
        return cx0 + (sx - x) / dw * (cx1 - cx0), cy0 + (sy - y) / dh * (cy1 - cy0)

    def _begin(self, g, sx, sy):
        if self.marking and self.texture:
            self._grab = None
            ed = self.editor
            m = {"kind": ed.mark_tool, "color": ed.mark_color, "size": ed.mark_size, "pts": [self._to_pic(sx, sy)]}
            if m["kind"] == "text":
                text = ed.mark_text.get_text().strip()
                if not text:
                    ed.toast("type the text first, then tap the photo")
                    ed.mark_text.grab_focus()
                    return
                m["text"] = text
            else:
                self._grab = "mark"
                self._start = (sx, sy, m)
            ed.state["marks"].append(m)
            self.queue_draw()
            return
        if not self.cropping:
            self._grab = None
            return
        x, y, dw, dh = self._frame()
        cx0, cy0, cx1, cy1 = self.editor.state["crop"]
        L, T, R, B = x + cx0 * dw, y + cy0 * dh, x + cx1 * dw, y + cy1 * dh
        near = lambda a, b: abs(a - b) < 36
        if near(sx, L) and near(sy, T):
            self._grab = "tl"
        elif near(sx, R) and near(sy, T):
            self._grab = "tr"
        elif near(sx, L) and near(sy, B):
            self._grab = "bl"
        elif near(sx, R) and near(sy, B):
            self._grab = "br"
        elif L < sx < R and T < sy < B:
            self._grab = "move"
        else:
            self._grab = None
        self._start = (sx, sy, (cx0, cy0, cx1, cy1))

    def _update(self, g, ox, oy):
        if not self._grab:
            return
        if self._grab == "mark":
            sx, sy, m = self._start
            p = self._to_pic(sx + ox, sy + oy)
            if m["kind"] in ("pen", "hl"):
                m["pts"].append(p)
            else:
                m["pts"][1:] = [p]
            self.queue_draw()
            return
        x, y, dw, dh = self._frame()
        sx, sy, (cx0, cy0, cx1, cy1) = self._start
        dx, dy = ox / dw, oy / dh
        MIN = 0.05
        if self._grab == "move":
            ww, hh = cx1 - cx0, cy1 - cy0
            nx0 = min(max(cx0 + dx, 0), 1 - ww)
            ny0 = min(max(cy0 + dy, 0), 1 - hh)
            crop = (nx0, ny0, nx0 + ww, ny0 + hh)
        else:
            x0, y0, x1, y1 = cx0, cy0, cx1, cy1
            if "l" in self._grab:
                x0 = min(max(cx0 + dx, 0), x1 - MIN)
            if "r" in self._grab:
                x1 = max(min(cx1 + dx, 1), x0 + MIN)
            if "t" in self._grab:
                y0 = min(max(cy0 + dy, 0), y1 - MIN)
            if "b" in self._grab:
                y1 = max(min(cy1 + dy, 1), y0 + MIN)
            if self.ratio:
                # keep the ratio in pixels: adjust the height from the width
                tw, th = self.full_tex.get_width(), self.full_tex.get_height()
                want_h = (x1 - x0) * tw / self.ratio / th
                if "t" in self._grab:
                    y0 = max(y1 - want_h, 0)
                else:
                    y1 = min(y0 + want_h, 1)
            crop = (x0, y0, x1, y1)
        self.editor.state["crop"] = crop
        self.queue_draw()

    def apply_ratio(self, ratio):
        self.ratio = ratio
        if not ratio or not self.full_tex:
            return
        tw, th = self.full_tex.get_width(), self.full_tex.get_height()
        # the biggest centred box with that ratio
        if tw / th > ratio:
            w = th * ratio / tw
            crop = ((1 - w) / 2, 0.0, (1 + w) / 2, 1.0)
        else:
            h = tw / ratio / th
            crop = (0.0, (1 - h) / 2, 1.0, (1 + h) / 2)
        self.editor.state["crop"] = crop
        self.queue_draw()


# --- the curve ------------------------------------------------------------------------------------

class CurveBox(Gtk.Widget):
    __gtype_name__ = "NrEditCurve"

    def __init__(self, editor):
        super().__init__(hexpand=True)
        self.editor = editor
        self.channel = "rgb"
        self.set_size_request(-1, 220)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", lambda *_: editor.refresh())
        self.add_controller(drag)
        self._idx = None

    def _area(self):
        w, h = self.get_width(), self.get_height()
        pad = 14
        return pad, pad, w - 2 * pad, h - 2 * pad

    def do_snapshot(self, snap):
        x, y, w, h = self._area()
        snap.append_color(rgba("fg", 0.05), rect(x, y, w, h))
        for k in range(1, 4):
            snap.append_color(rgba("fg", 0.12), rect(x + w * k / 4, y, 1, h))
            snap.append_color(rgba("fg", 0.12), rect(x, y + h * k / 4, w, 1))
        col = {"rgb": "fg-bright", "r": "hot-pink", "g": "mint", "b": "blue"}[self.channel]
        lut = monotone_lut(self.editor.state["curves"][self.channel])
        pb = Gsk.PathBuilder.new()
        for i in range(0, 256, 3):
            px, py = x + w * i / 255, y + h * (1 - lut[i] / 255)
            (pb.move_to if i == 0 else pb.line_to)(px, py)
        pb.line_to(x + w, y + h * (1 - lut[255] / 255))
        snap.append_stroke(pb.to_path(), Gsk.Stroke.new(2.5), rgba(col))
        for i, (cx, cy) in enumerate(zip(CURVE_X, self.editor.state["curves"][self.channel])):
            px, py = x + w * cx, y + h * (1 - cy)
            p = Gsk.PathBuilder.new()
            p.add_circle(Graphene.Point().init(px, py), 9 if i == self._idx else 7)
            snap.append_fill(p.to_path(), Gsk.FillRule.WINDING, rgba(col))

    def _begin(self, g, sx, sy):
        x, y, w, h = self._area()
        self._idx = min(range(len(CURVE_X)), key=lambda i: abs(x + w * CURVE_X[i] - sx))
        self._y0 = self.editor.state["curves"][self.channel][self._idx]

    def _update(self, g, ox, oy):
        if self._idx is None:
            return
        x, y, w, h = self._area()
        ys = self.editor.state["curves"][self.channel]
        ys[self._idx] = min(max(self._y0 - oy / h, 0.0), 1.0)
        self.queue_draw()
        self.editor.refresh(live=True)


# --- window ---------------------------------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app, path):
        super().__init__(application=app, title="Edit", default_width=540, default_height=1100)
        self.path = path
        self.state = default_state()
        self.full = None
        self.small = None
        self._pending = 0

        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        self.save_btn = Gtk.Button(label="SAVE", css_classes=["chip", "save"])
        self.save_btn.connect("clicked", lambda *_: self.save())
        hb.pack_start(self.save_btn)
        share = Gtk.Button(label="SHARE", css_classes=["chip"])
        share.connect("clicked", lambda *_: self.save(share=True))
        hb.pack_start(share)
        reset = Gtk.Button(icon_name="edit-undo-symbolic", css_classes=["flat"], tooltip_text="Reset all")
        reset.connect("clicked", lambda *_: self.reset())
        hb.pack_end(reset)
        hb.set_title_widget(Gtk.Label(label="NOTREAL.EDIT", css_classes=["app-title"]))
        tv.add_top_bar(hb)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.canvas = Canvas(self)
        body.append(self.canvas)

        self.tools = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vhomogeneous=False)
        # crop
        crop = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12, margin_end=12)
        rr = Gtk.Box(spacing=6)
        group = None
        for text, ratio in RATIOS:
            b = Gtk.ToggleButton(label=text, css_classes=["chip"], active=ratio is None)
            if group:
                b.set_group(group)
            group = group or b
            b.connect("toggled", lambda btn, r=ratio: btn.get_active() and self.canvas.apply_ratio(r))
            rr.append(b)
        crop.append(rr)
        rot = Gtk.Box(spacing=6)
        for text, step in (("⟲ 90°", -1), ("⟳ 90°", 1)):
            b = Gtk.Button(label=text, css_classes=["chip"])
            b.connect("clicked", lambda *_, s=step: self.rotate(s))
            rot.append(b)
        crop.append(rot)
        self.tools.add_named(crop, "crop")
        # curves
        cur = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=8, margin_end=8)
        ch = Gtk.Box(spacing=6, margin_start=4)
        group = None
        self.curve = CurveBox(self)
        for text, key in (("RGB", "rgb"), ("R", "r"), ("G", "g"), ("B", "b")):
            b = Gtk.ToggleButton(label=text, css_classes=["chip"], active=key == "rgb")
            if group:
                b.set_group(group)
            group = group or b
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self._channel(k))
            ch.append(b)
        cur.append(ch)
        cur.append(self.curve)
        self.tools.add_named(cur, "curves")
        # saturation
        sat = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=14, margin_end=14)
        self.sat_label = Gtk.Label(label="SATURATION 100%", css_classes=["tool-label"], xalign=0)
        sat.append(self.sat_label)
        self.sat = Gtk.Scale(adjustment=Gtk.Adjustment(lower=0, upper=2, value=1, step_increment=0.05),
                             draw_value=False, hexpand=True)
        self.sat.add_mark(1.0, Gtk.PositionType.BOTTOM, None)
        self.sat.connect("value-changed", self._sat)
        sat.append(self.sat)
        self.tools.add_named(sat, "sat")
        # blur: how much + where (left-handed: the mode chips on the left, above the slider)
        blur = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=14, margin_end=14)
        modes = Gtk.Box(spacing=6)
        group = None
        self.blur_modes = []
        for text, edges in (("ALL", False), ("EDGES", True)):
            b = Gtk.ToggleButton(label=text, css_classes=["chip"], active=not edges)
            if group:
                b.set_group(group)
            group = group or b
            b.connect("toggled", lambda btn, e=edges: btn.get_active() and self._blur_mode(e))
            modes.append(b)
            self.blur_modes.append(b)
        blur.append(modes)
        self.blur_label = Gtk.Label(label="BLUR 0%", css_classes=["tool-label"], xalign=0)
        blur.append(self.blur_label)
        self.blur = Gtk.Scale(adjustment=Gtk.Adjustment(lower=0, upper=1, value=0, step_increment=0.02),
                              draw_value=False, hexpand=True)
        self.blur.connect("value-changed", self._blur)
        blur.append(self.blur)
        self.tools.add_named(blur, "blur")
        # vignette: right darkens the corners, left whitens them
        vig = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=14, margin_end=14)
        self.vig_label = Gtk.Label(label="VIGNETTE 0", css_classes=["tool-label"], xalign=0)
        vig.append(self.vig_label)
        self.vig = Gtk.Scale(adjustment=Gtk.Adjustment(lower=-1, upper=1, value=0, step_increment=0.02),
                             draw_value=False, hexpand=True)
        self.vig.add_mark(0.0, Gtk.PositionType.BOTTOM, None)
        self.vig.connect("value-changed", self._vig)
        vig.append(self.vig)
        self.tools.add_named(vig, "vig")
        # markup: draw on the photo (left-handed: UNDO first in the colour row)
        self.mark_tool, self.mark_color, self.mark_size = "pen", MARK_COLORS[0], MARK_SIZES[1][1]
        mk = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12, margin_end=12)
        kinds = Gtk.Box(spacing=6)
        group = None
        for text, key in MARK_TOOLS:
            b = Gtk.ToggleButton(label=text, css_classes=["chip"], active=key == "pen")
            if group:
                b.set_group(group)
            group = group or b
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self._mark_tool(k))
            kinds.append(b)
        mk.append(kinds)
        row = Gtk.Box(spacing=6)
        undo = Gtk.Button(label="UNDO", css_classes=["chip"])
        undo.connect("clicked", lambda *_: self.undo_mark())
        row.append(undo)
        group = None
        for i, c in enumerate(MARK_COLORS):
            b = Gtk.ToggleButton(css_classes=["swatch", f"sw{i}"], active=i == 0, valign=Gtk.Align.CENTER)
            if group:
                b.set_group(group)
            group = group or b
            b.connect("toggled", lambda btn, c=c: btn.get_active() and setattr(self, "mark_color", c))
            row.append(b)
        group = None
        for text, size in MARK_SIZES:
            b = Gtk.ToggleButton(label=text, css_classes=["chip"], active=size == self.mark_size)
            if group:
                b.set_group(group)
            group = group or b
            b.connect("toggled", lambda btn, s=size: btn.get_active() and setattr(self, "mark_size", s))
            row.append(b)
        mk.append(row)
        self.mark_text = Gtk.Entry(placeholder_text="type, then tap the photo", visible=False)
        mk.append(self.mark_text)
        self.tools.add_named(mk, "mark")
        body.append(self.tools)

        self._tab_btns = []
        tabs = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=8, margin_bottom=14,
                       halign=Gtk.Align.START)
        group = None
        for text, key in (("CROP", "crop"), ("MARKUP", "mark"), ("CURVES", "curves"), ("COLOR", "sat"),
                          ("BLUR", "blur"), ("VIGNETTE", "vig")):
            b = Gtk.ToggleButton(label=text, css_classes=["tool-tab"], active=key == "curves")
            if group:
                b.set_group(group)
            group = group or b
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self.tool(k))
            b.key = key
            self._tab_btns.append(b)
            tabs.append(b)
        body.append(Gtk.ScrolledWindow(child=tabs, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                       hscrollbar_policy=Gtk.PolicyType.EXTERNAL, propagate_natural_height=True))
        self.toasts = Adw.ToastOverlay(child=body)
        tv.set_content(self.toasts)
        self.set_content(tv)
        self.tools.set_visible_child_name("curves")
        if os.environ.get("NREDIT_TAB"):        # headless screenshots: "blur:0.5" / "vig:0.6"
            tab, _, val = os.environ["NREDIT_TAB"].partition(":")
            next(b for b in self._tab_btns if b.key == tab).set_active(True)
            getattr(self, tab).set_value(float(val or 0))
        threading.Thread(target=self._load, daemon=True).start()

    # loading
    def _load(self):
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file(self.path)
            full = to_array(pb)
        except (GLib.Error, ValueError) as e:
            GLib.idle_add(self.toast, f"can't open: {e}")
            return
        h, w = full.shape[:2]
        s = min(1.0, PREVIEW_PX / max(w, h))
        spb = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(full.tobytes()), GdkPixbuf.Colorspace.RGB,
                                              False, 8, w, h, w * 3)
        spb = spb.scale_simple(max(1, int(w * s)), max(1, int(h * s)), GdkPixbuf.InterpType.BILINEAR)
        small = to_array(spb)

        def ready():
            self.full, self.small = full, small
            self.refresh()
            return False
        GLib.idle_add(ready)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))
        return False

    # tools
    def tool(self, key):
        self.tools.set_visible_child_name(key)
        self.canvas.marking = key == "mark"
        cropping = key == "crop"
        if cropping != self.canvas.cropping:
            self.canvas.cropping = cropping
            self.refresh()

    def _channel(self, key):
        self.curve.channel = key
        self.curve.queue_draw()

    def _sat(self, scale):
        self.state["sat"] = scale.get_value()
        self.sat_label.set_label(f"SATURATION {int(self.state['sat'] * 100)}%")
        self.refresh(live=True)

    def _blur(self, scale):
        self.state["blur"] = scale.get_value()
        self.blur_label.set_label(f"BLUR {int(self.state['blur'] * 100)}%")
        self.refresh(live=True)

    def _blur_mode(self, edges):
        self.state["blur_edges"] = edges
        self.refresh()

    def _vig(self, scale):
        v = scale.get_value()
        self.state["vig"] = v
        self.vig_label.set_label("VIGNETTE 0" if abs(v) < 0.01 else
                                 f"VIGNETTE {int(abs(v) * 100)}% {'DARK' if v > 0 else 'WHITE'}")
        self.refresh(live=True)

    def _mark_tool(self, key):
        self.mark_tool = key
        self.mark_text.set_visible(key == "text")
        if key == "text":
            self.mark_text.grab_focus()

    def undo_mark(self):
        if self.state["marks"]:
            self.state["marks"].pop()
            self.canvas.queue_draw()

    def rotate(self, step):
        self.state["rot"] = (self.state["rot"] + step) % 4
        # rotate the crop box (and the marks) with the picture so they keep covering the same part
        x0, y0, x1, y1 = self.state["crop"]
        self.state["crop"] = (1 - y1, x0, 1 - y0, x1) if step > 0 else (y0, 1 - x1, y1, 1 - x0)
        for m in self.state["marks"]:
            m["pts"] = [(1 - y, x) if step > 0 else (y, 1 - x) for x, y in m["pts"]]
        self.refresh()

    def reset(self):
        self.state = default_state()
        self.sat.set_value(1.0)
        self.blur.set_value(0.0)
        self.vig.set_value(0.0)
        self.blur_modes[0].set_active(True)
        self.mark_text.set_text("")
        self.canvas.ratio = None
        self.curve.queue_draw()
        self.refresh()

    def refresh(self, live=False):
        if self.small is None:
            return
        if live:                      # coalesce drags: at most one preview per frame
            if self._pending:
                return
            self._pending = GLib.idle_add(self._do_refresh)
        else:
            self._do_refresh()

    def _do_refresh(self):
        self._pending = 0
        st = dict(self.state)
        if self.canvas.cropping:
            # show the whole (rotated, toned) picture with the crop frame over it
            st["crop"] = (0.0, 0.0, 1.0, 1.0)
            base = render(self.small, st)
            self.canvas.full_tex = to_texture(base)
        self.canvas.texture = to_texture(render(self.small, self.state))
        self.canvas.queue_draw()
        return False

    # saving
    def markup_layer(self, state):
        """The marks at full size as an HxWx4 RGBA array (None without marks). GSK renders them on
        the GPU, so this runs here on the main thread, before the save thread starts."""
        if not state["marks"]:
            return None
        h, w = self.full.shape[:2]
        if state["rot"] % 2:
            w, h = h, w
        x0, y0, x1, y1 = state["crop"]
        cw = max(int(x1 * w), int(x0 * w) + 1) - int(x0 * w)       # the same crop maths as render()
        ch = max(int(y1 * h), int(y0 * h) + 1) - int(y0 * h)
        snap = Gtk.Snapshot()
        draw_markup(snap, state["marks"], state["crop"], (0, 0, cw, ch), self.canvas.layout)
        tex = self.get_renderer().render_texture(snap.to_node(), rect(0, 0, cw, ch))
        dl = Gdk.TextureDownloader.new(tex)
        dl.set_format(Gdk.MemoryFormat.R8G8B8A8)
        data, stride = dl.download_bytes()
        a = np.frombuffer(data.get_data(), np.uint8)
        return np.lib.stride_tricks.as_strided(a, (ch, cw, 4), (stride, 4, 1)).copy()

    def out_path(self):
        root, _ext = os.path.splitext(self.path)
        p, n = root + "_edit.jpg", 1
        while os.path.exists(p):
            p, n = f"{root}_edit{n}.jpg", n + 1
        return p

    def save(self, share=False):
        if self.full is None:
            return
        self.save_btn.set_sensitive(False)
        self.toast("saving…")
        state = {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.state.items()}
        state["curves"] = {k: list(v) for k, v in self.state["curves"].items()}
        layer = self.markup_layer(state)
        out = self.out_path()

        def work():
            a = render(self.full, state)
            if layer is not None:
                composite(a, layer)
            a = np.ascontiguousarray(a)
            h, w = a.shape[:2]
            pb = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(a.tobytes()), GdkPixbuf.Colorspace.RGB,
                                                 False, 8, w, h, w * 3)
            pb.savev(out + ".part", "jpeg", ["quality"], ["93"])
            os.replace(out + ".part", out)
            GLib.idle_add(done)

        def done():
            self.save_btn.set_sensitive(True)
            self.toast(f"saved {os.path.basename(out)}")
            if share:
                subprocess.Popen([os.path.expanduser("~/share/nrshare"), out], start_new_session=True)
            return False
        threading.Thread(target=work, daemon=True).start()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_OPEN | Gio.ApplicationFlags.NON_UNIQUE)

    def do_startup(self):
        Adw.Application.do_startup(self)
        theme.apply(os.path.join(HERE, "style.css"))

    def do_open(self, files, n, hint):
        for f in files:
            if f.get_path():
                Window(self, f.get_path()).present()

    def do_activate(self):
        dlg = Gtk.FileDialog(title="Edit a photo")
        filt = Gtk.FileFilter()
        filt.add_mime_type("image/*")
        store = Gio.ListStore(item_type=Gtk.FileFilter)
        store.append(filt)
        dlg.set_filters(store)
        dlg.set_initial_folder(Gio.File.new_for_path(os.path.expanduser("~/Pictures")))
        self.hold()

        def picked(d, res):
            self.release()
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            Window(self, f.get_path()).present()
        dlg.open(None, None, picked)


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
