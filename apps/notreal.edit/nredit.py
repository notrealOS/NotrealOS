#!/usr/bin/env python3
"""notarealEdit: a small photo editor - crop, curves, saturation, then save a copy or share.

Edits preview live on a downscaled copy; SAVE renders the full image in the background and
writes <name>_edit.jpg next to the original (the original is never overwritten).
Left-handed: tool tabs and SAVE / SHARE sit on the left.
"""
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
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Graphene, Gsk, Gtk  # noqa: E402

import numpy as np  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.expanduser("~/theme"))
import notareal_theme as theme  # noqa: E402

APP_ID = "dev.notareal.Edit"
PREVIEW_PX = 1400
CURVE_X = [0.0, 0.25, 0.5, 0.75, 1.0]
RATIOS = [("FREE", None), ("1:1", 1.0), ("4:3", 4 / 3), ("3:4", 3 / 4), ("16:9", 16 / 9)]


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


def render(a, state):
    """rotate -> crop -> curves -> saturation. a: HxWx3 uint8.
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
    return out


def default_state():
    return {"crop": (0.0, 0.0, 1.0, 1.0), "rot": 0, "sat": 1.0,
            "curves": {c: list(CURVE_X) for c in ("rgb", "r", "g", "b")}}


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
        self.ratio = None
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        self.add_controller(drag)
        self._grab = None

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

    def _begin(self, g, sx, sy):
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
        body.append(self.tools)

        tabs = Gtk.Box(spacing=8, margin_start=12, margin_top=8, margin_bottom=14, halign=Gtk.Align.START)
        group = None
        for text, key in (("CROP", "crop"), ("CURVES", "curves"), ("SATURATION", "sat")):
            b = Gtk.ToggleButton(label=text, css_classes=["tool-tab"], active=key == "curves")
            if group:
                b.set_group(group)
            group = group or b
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self.tool(k))
            tabs.append(b)
        body.append(tabs)
        self.toasts = Adw.ToastOverlay(child=body)
        tv.set_content(self.toasts)
        self.set_content(tv)
        self.tools.set_visible_child_name("curves")
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

    def rotate(self, step):
        self.state["rot"] = (self.state["rot"] + step) % 4
        # rotate the crop box with the picture so it keeps covering the same part
        x0, y0, x1, y1 = self.state["crop"]
        self.state["crop"] = (1 - y1, x0, 1 - y0, x1) if step > 0 else (y0, 1 - x1, y1, 1 - x0)
        self.refresh()

    def reset(self):
        self.state = default_state()
        self.sat.set_value(1.0)
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
        out = self.out_path()

        def work():
            a = np.ascontiguousarray(render(self.full, state))
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
