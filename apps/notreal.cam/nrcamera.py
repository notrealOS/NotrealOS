#!/usr/bin/env python3
"""notarealCamera: our own camera app (replaces Snapshot for photos).

Preview: PipeWire camera node -> (GL theme filter) -> gtk4paintablesink, which also turns the
picture upright from the stream's image-orientation tag. Lenses: 0.6 = s5kjn1 ultrawide,
1x = imx766 main, selfie = imx471.

Photos:
  12 MP (default): the preview pauses, libcamera's `cam` grabs ~10 frames at the sensor's full
    size (CPU soft ISP: the GPU path froze the phone at full size on 10-13), the last frame
    (exposure settled) is kept, the preview comes back. ~3 s.
  FAST: the current preview frame (2048x1536 / 1600x1200), instant.
Rotation: the sensor's mounting (cam Rotation property) + how the phone is held
(iio-sensor-proxy accelerometer). Saved to ~/Pictures/Camera (the Gallery's CAMERA tab).

Controls sit on the LEFT (left-handed user): shutter bottom-left, EV slider on the left edge.
Tap the preview = focus there (contrast sweep through afd's lens position files).
No FX: the theme looks were removed 10-17 (user); fx.py stays for the pass-through shader.
FILM (10-18, top-left chip): 35 mm stock looks from film.py, live in the shader (with the 3:2
frame dimmed in photo mode) and on saved photos (cropped to 3:2 + the notreal.camera stamp).

VIDEO mode (the PHOTO/VIDEO chip under the lenses): the shutter starts/stops recording. A branch
is hung off the tee after the FX shader while the preview runs: the GPU turns the frame upright
(lens mounting + how the phone was held at the start) and scales it to 1080x1440 (or 1440x1080),
x264 (software: the iris HW codec is a hard-freeze suspect, 10-12) + AAC from the mic, MP4 in
~/Videos/Camera (the Gallery's CAMERA tab). ~1.3 cores.
"""
import datetime
import glob
import json
import math
import os
import subprocess
import sys
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gst, Gtk  # noqa: E402

import numpy as np  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.expanduser("~/theme"))
import notareal_theme  # noqa: E402
import fx  # noqa: E402
import film  # noqa: E402
import rawdev  # noqa: E402
from camsvc import restart_camera_services  # noqa: E402

APP_ID = "dev.notareal.Camera"
HOME = os.path.expanduser("~")
OUT_DIR = os.environ.get("NRCAM_OUT") or os.path.join(HOME, "Pictures", "Camera")   # NRCAM_OUT: tests
CACHE = os.path.join(HOME, ".cache", "nrcamera")
CONF = os.path.join(HOME, ".config", "nrcamera", "config.json")
RUN = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
EV_FILE = os.path.join(RUN, "camera-ev")            # read by our libcamera AGC (r10+)
TORCH = "/sys/class/leds/white:flash/brightness"
AFD = "/usr/local/sbin/afd"

# preview size = what the PipeWire node offers in 4:3; still = the sensor's full mode via `cam`.
# rot = the sensor's mounting (libcamera Rotation): the frame is turned that many degrees
# counter-clockwise to stand upright (= GdkPixbuf.rotate_simple(rot)).
LENSES = [
    dict(key="uw", label="0.6", model="s5kjn1", path="camera@2d", preview=(2048, 1536),
         # NOT 4080x3072: libcamera then picks the 8160x6144 quad-Bayer mode (needs a bigger
         # input than the output) and the debayer turns it grey + checkerboard (10-17)
         still=(4072, 3064), rot=90, af=os.path.join(HOME, ".config", "af-position"),
         af_range=(250, 600), af_default=400),
    dict(key="main", label="1×", model="imx766", path="camera@10", preview=(1600, 1200),
         still=(4096, 3072), rot=90, af=os.path.join(HOME, ".config", "af-position-wide"),
         af_range=(50, 460), af_default=80),   # 80 = infinity (sweep 10-18)
    dict(key="front", label="◐", model="imx471", path="camera@1a", preview=(2048, 1536),
         still=None, rot=270, af=None, mirror=True),
]
STILL_FRAMES = 10          # frames `cam` captures; the last one has a settled exposure
VID_DIR = os.environ.get("NRCAM_OUT") or os.path.join(HOME, "Videos", "Camera")
VID_LONG, VID_SHORT = 1440, 1080
VID_KBPS = 12000
# counter-clockwise turn -> glvideoflip method
FLIP = {0: "none", 90: "counterclockwise", 180: "rotate-180", 270: "clockwise"}


def log(*a):
    print(time.strftime("%H:%M:%S"), "nrcamera:", *a, flush=True)


def load_conf():
    try:
        with open(CONF) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_conf(c):
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    with open(CONF + ".tmp", "w") as f:
        json.dump(c, f)
    os.replace(CONF + ".tmp", CONF)


def write_atomic(path, text):
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.replace(path + ".tmp", path)


def pw_nodes():
    """PipeWire camera nodes by sensor model: {"s5kjn1": node.name, ...}."""
    out = {}
    mon = Gst.DeviceMonitor()
    mon.add_filter("Video/Source", None)
    mon.start()
    for d in mon.get_devices():
        p = d.get_properties()
        if p and p.get_string("node.nick") and p.get_string("node.name"):
            out[p.get_string("node.nick")] = p.get_string("node.name")
    mon.stop()
    return out


def cam_indexes():
    """`cam -l` numbers by DT path ("camera@2d" -> 1); they can change between boots."""
    out = {}
    try:
        txt = subprocess.run(["cam", "-l"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return out
    for line in txt.splitlines():
        line = line.strip()
        if line[:1].isdigit() and ":" in line:
            idx = line.split(":")[0]
            for lens in LENSES:
                if lens["path"] + ")" in line:
                    out[lens["path"]] = idx
    return out


def sharpness(rgba, box):
    """Gradient energy of the green channel inside box (x0, y0, x1, y1), 0..1 of the frame."""
    h, w = rgba.shape[:2]
    x0, y0, x1, y1 = (int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h))
    g = rgba[y0:y1:2, x0:x1:2, 1].astype(np.float32)
    if g.shape[0] < 4 or g.shape[1] < 4:
        return 0.0
    gx = np.diff(g, axis=1)
    gy = np.diff(g, axis=0)
    return float((gx * gx).mean() + (gy * gy).mean())


class Accel:
    """Device orientation from iio-sensor-proxy (claimed while the app runs)."""

    def __init__(self):
        self.proxy = None
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None, "net.hadess.SensorProxy",
                "/net/hadess/SensorProxy", "net.hadess.SensorProxy", None)
            self.proxy.call_sync("ClaimAccelerometer", None, Gio.DBusCallFlags.NONE, 2000, None)
        except GLib.Error as e:
            log("accelerometer unavailable:", e.message)
            self.proxy = None

    def orientation(self):
        if not self.proxy:
            return "normal"
        try:   # ask now: the cached property stays "undefined" for a while after claiming
            v = self.proxy.call_sync("org.freedesktop.DBus.Properties.Get",
                                     GLib.Variant("(ss)", ("net.hadess.SensorProxy", "AccelerometerOrientation")),
                                     Gio.DBusCallFlags.NONE, 500, None)
            return v.unpack()[0]
        except GLib.Error:
            v = self.proxy.get_cached_property("AccelerometerOrientation")
            return v.unpack() if v else "normal"

    def release(self):
        if self.proxy:
            try:
                self.proxy.call_sync("ReleaseAccelerometer", None, Gio.DBusCallFlags.NONE, 1000, None)
            except GLib.Error:
                pass


# extra counter-clockwise turn for how the phone is held (the scene's "up" is that phone edge)
HELD_CCW = {"normal": 0, "left-up": 270, "right-up": 90, "bottom-up": 180, "undefined": 0}


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Camera")
        self.set_default_size(360, 800)
        self.conf = {"lens": "main", "hq": True, "fx": False, "flash": "off", "timer": 0, "mode": "photo",
                     "film": None, "hdr": False}
        self.conf.update(load_conf())
        self.conf["fx"] = False      # FX removed (user 10-17)
        self.rec = None              # the running recording: dict(bin, pad, path, t0, ...)
        self.vtee = None
        self.accel = Accel()
        self.pipeline = None
        self.sink = None
        self.grab = None
        self.busy = False            # a capture or focus sweep is running
        self.nodes = {}
        self.cams = {}
        self.tag_rot = None
        self.countdown = 0
        os.makedirs(OUT_DIR, exist_ok=True)
        os.makedirs(CACHE, exist_ok=True)
        self.afd_fast = os.path.exists(AFD) and "FAST_POLL" in open(AFD, errors="ignore").read()
        self._build()
        notareal_theme.on_change(self._theme_changed)
        GLib.idle_add(self._first_start)
        if os.environ.get("NRCAM_SELFTEST") == "video":   # tests: a 5 s recording, then quit
            self.conf["mode"] = "video"
            GLib.timeout_add(4000, lambda: (self.shoot(), False)[1])
            GLib.timeout_add(9000, lambda: (self.shoot(), False)[1])
            GLib.timeout_add(12000, lambda: (self.close(), False)[1])
        elif os.environ.get("NRCAM_SELFTEST"):    # tests: one 12 MP + one FAST shot, then quit
            def selftest(step=[0]):
                if self.busy:
                    return True
                step[0] += 1
                if step[0] == 1:
                    self.conf["hq"] = True
                    self.shoot()
                elif step[0] == 2:
                    self.conf["hq"] = False
                    self.shoot()
                else:
                    GLib.timeout_add(2500, lambda: (self.close(), False)[1])
                    return False
                return True
            GLib.timeout_add(4000, selftest)
        self.connect("close-request", self._on_close)
        self.connect("notify::is-active", self._on_active)

    # ---------- UI ----------
    def _build(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.add_css_class("cam-root")
        self.set_content(root)

        # top bar: frequent toggles on the left
        top = Gtk.Box(spacing=8, margin_top=8, margin_bottom=6, margin_start=10, margin_end=10)
        self.flash_btn = Gtk.Button(css_classes=["cam-chip"])
        self.flash_btn.connect("clicked", lambda *_: self._cycle_flash())
        self.timer_btn = Gtk.Button(css_classes=["cam-chip"])
        self.timer_btn.connect("clicked", lambda *_: self._cycle_timer())
        # FILM: 35 mm stock look (live + saved), 3:2 frame and the notreal.camera stamp
        self.film_btn = Gtk.Button(css_classes=["cam-chip"])
        self.film_btn.connect("clicked", lambda *_: self._cycle_film())
        top.append(self.film_btn)
        # HDR (main lens, 12 MP): normal + -3 EV raw frames merged by rawdev
        self.hdr_btn = Gtk.ToggleButton(label="HDR", css_classes=["cam-chip"], active=self.conf["hdr"])
        self.hdr_btn.connect("toggled", self._hdr_toggled)
        top.append(self.hdr_btn)
        top.append(self.flash_btn)
        top.append(self.timer_btn)
        spacer = Gtk.Box(hexpand=True)
        top.append(spacer)
        self.hq_btn = Gtk.ToggleButton(css_classes=["cam-chip"], active=self.conf["hq"])
        self.hq_btn.connect("toggled", self._hq_toggled)
        top.append(self.hq_btn)
        root.append(top)

        # preview with overlays (focus box, EV slider on the left, countdown, status)
        ov = Gtk.Overlay(vexpand=True)
        ov.add_css_class("cam-view")
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True, vexpand=True)
        self.picture.add_css_class("cam-preview")
        ov.set_child(self.picture)
        self.freeze = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True, visible=False)
        self.freeze.add_css_class("cam-preview")
        ov.add_overlay(self.freeze)
        tap = Gtk.GestureClick()
        tap.connect("released", self._preview_tap)
        self.picture.add_controller(tap)

        self.focus_box = Gtk.Box(css_classes=["cam-focus"], visible=False, can_target=False,
                                 halign=Gtk.Align.START, valign=Gtk.Align.START)
        self.focus_box.set_size_request(72, 72)
        ov.add_overlay(self.focus_box)

        evcol = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, halign=Gtk.Align.START,
                        valign=Gtk.Align.CENTER, margin_start=6, css_classes=["cam-ev"])
        self.ev_label = Gtk.Button(label="EV 0", css_classes=["cam-chip", "small"])
        self.ev_label.connect("clicked", lambda *_: self.ev.set_value(0))
        self.ev = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL, inverted=True, draw_value=False,
                            adjustment=Gtk.Adjustment(lower=-3, upper=2, step_increment=0.5,
                                                      page_increment=0.5, value=0))
        self.ev.set_size_request(-1, 220)
        self.ev.connect("value-changed", self._ev_changed)
        evcol.append(self.ev_label)
        evcol.append(self.ev)
        ov.add_overlay(evcol)

        self.big = Gtk.Label(css_classes=["cam-countdown"], visible=False, can_target=False)
        ov.add_overlay(self.big)
        self.status = Gtk.Label(css_classes=["cam-status"], visible=False, can_target=False,
                                valign=Gtk.Align.END, margin_bottom=12)
        ov.add_overlay(self.status)
        root.append(ov)

        # lens row
        lens_row = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, margin_top=10)
        self.lens_btns = {}
        for lens in LENSES:
            b = Gtk.ToggleButton(label=lens["label"], css_classes=["cam-lens"])
            b.connect("clicked", lambda _b, k=lens["key"]: self.switch_lens(k))
            lens_row.append(b)
            self.lens_btns[lens["key"]] = b
        root.append(lens_row)

        # bottom: shutter on the LEFT, last photo on the right
        bottom = Gtk.CenterBox(margin_top=12, margin_bottom=22, margin_start=26, margin_end=26)
        self.shutter = Gtk.Button(css_classes=["cam-shutter"], valign=Gtk.Align.CENTER)
        self.shutter.set_size_request(88, 88)
        self.shutter.connect("clicked", lambda *_: self.shoot())
        bottom.set_start_widget(self.shutter)
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER)
        self.mode_btn = Gtk.Button(css_classes=["cam-chip", "mode"], halign=Gtk.Align.CENTER)
        self.mode_btn.connect("clicked", lambda *_: self._toggle_mode())
        self.mode_label = Gtk.Label(css_classes=["cam-mode"])
        center.append(self.mode_btn)
        center.append(self.mode_label)
        bottom.set_center_widget(center)
        self.thumb = Gtk.Button(css_classes=["cam-thumb"], valign=Gtk.Align.CENTER)
        self.thumb.set_size_request(60, 60)
        self.thumb_pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.thumb.set_child(self.thumb_pic)
        self.thumb.connect("clicked", lambda *_: self.open_gallery())
        bottom.set_end_widget(self.thumb)
        root.append(bottom)

        # volume keys = shutter
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self._refresh_chips()
        self._load_thumb()

    def _refresh_chips(self):
        self.flash_btn.set_label({"off": "⚡ OFF", "on": "⚡ ON", "torch": "⚡ TORCH"}[self.conf["flash"]])
        self.timer_btn.set_label("⏱ OFF" if not self.conf["timer"] else f"⏱ {self.conf['timer']}s")
        self.film_btn.set_label("🎞 " + film.label(self.conf.get("film")))
        (self.film_btn.add_css_class if self.conf.get("film") else self.film_btn.remove_css_class)("on")
        lens = self.lens()
        video = self.conf["mode"] == "video"
        hq_ok = lens["still"] is not None and not video
        self.hq_btn.set_sensitive(hq_ok)
        self.hq_btn.set_label("1440p" if video else "12 MP" if (self.conf["hq"] and hq_ok) else "FAST")
        self.hdr_btn.set_sensitive(hq_ok and self.conf["hq"] and lens["key"] == "main")
        (self.hdr_btn.add_css_class if self.conf["hdr"] else self.hdr_btn.remove_css_class)("on")
        self.mode_btn.set_label("● VIDEO" if video else "◉ PHOTO")
        self.mode_btn.set_sensitive(not self.rec)
        (self.shutter.add_css_class if video else self.shutter.remove_css_class)("video")
        (self.shutter.add_css_class if self.rec else self.shutter.remove_css_class)("rec")
        for k, b in self.lens_btns.items():
            b.set_active(k == lens["key"])
        self.mode_label.set_label(film.label(self.conf["film"]) + " 35MM" if self.conf.get("film")
                                  else notareal_theme.active().upper())
        (self.picture.add_css_class if lens.get("mirror") else self.picture.remove_css_class)("mirror")

    # ---------- pipeline ----------
    def lens(self):
        return next((x for x in LENSES if x["key"] == self.conf["lens"]), LENSES[1])

    def _first_start(self):
        self.nodes = pw_nodes()
        # NOT `cam -l` here: it probes the camera graph while PipeWire starts the stream and camss
        # then refused it ("Failed to start media pipeline: -32", black preview, 10-16).
        # The numbers are looked up when a 12 MP shot has already stopped the preview.
        self._reset_focus()
        self.start_preview()
        self._apply_flash()
        write_atomic(EV_FILE, "0")
        return False

    def _reset_focus(self):
        # a tap-to-focus position would otherwise stick across launches; start at infinity
        for lens in LENSES:
            if lens.get("af"):
                write_atomic(lens["af"], str(lens["af_default"]))

    def _shader(self):
        return (film.glsl(self.conf.get("film"), frame=self.conf["mode"] != "video")
                or fx.glsl(notareal_theme.active() if self.conf["fx"] else None))

    def start_preview(self, retry=True):
        self.stop_preview()
        lens = self.lens()
        node = self.nodes.get(lens["model"])
        if not node:
            self.nodes = pw_nodes()
            node = self.nodes.get(lens["model"])
        if not node:
            self.say(f"{lens['model']} not found")
            return
        w, h = lens["preview"]
        p = Gst.Pipeline.new("cam")
        if os.environ.get("NRCAM_TEST"):          # UI tests: a test pattern instead of the sensor
            src = Gst.ElementFactory.make("videotestsrc")
            src.set_property("is-live", True)
            src.set_property("pattern", "smpte")
        else:
            src = Gst.ElementFactory.make("pipewiresrc")
            src.set_property("target-object", node)
            src.set_property("do-timestamp", True)
        caps = Gst.ElementFactory.make("capsfilter")
        fmt = ",format=RGBA,framerate=30/1" if os.environ.get("NRCAM_TEST") else ""
        caps.set_property("caps", Gst.Caps.from_string(f"video/x-raw,width={w},height={h}{fmt}"))
        tee = Gst.ElementFactory.make("tee")
        q1 = Gst.ElementFactory.make("queue")
        q1.set_property("leaky", 2)
        q1.set_property("max-size-buffers", 2)
        up = Gst.ElementFactory.make("glupload")
        conv = Gst.ElementFactory.make("glcolorconvert")
        self.shader = Gst.ElementFactory.make("glshader")
        self.shader.set_property("fragment", self._shader())
        sink = Gst.ElementFactory.make("gtk4paintablesink")
        # pin the shader's output to the preview size: otherwise linking a recording branch
        # renegotiated it to the video size (squished preview, softer video, 10-17)
        fixsize = Gst.ElementFactory.make("capsfilter")
        fixsize.set_property("caps", Gst.Caps.from_string(
            f"video/x-raw(memory:GLMemory),width={w},height={h}"))
        vtee = Gst.ElementFactory.make("tee")       # recordings hang off here (after the FX)
        vtee.set_property("allow-not-linked", True)
        q3 = Gst.ElementFactory.make("queue")
        q3.set_property("leaky", 2)
        q3.set_property("max-size-buffers", 2)
        q2 = Gst.ElementFactory.make("queue")
        q2.set_property("leaky", 2)
        q2.set_property("max-size-buffers", 1)
        grab = Gst.ElementFactory.make("appsink")
        grab.set_property("max-buffers", 1)
        grab.set_property("drop", True)
        grab.set_property("sync", False)
        grab.set_property("caps", Gst.Caps.from_string("video/x-raw,format=RGBA"))
        for e in (src, caps, tee, q1, up, conv, self.shader, fixsize, vtee, q3, sink, q2, grab):
            p.add(e)
        src.link(caps)
        caps.link(tee)
        # the caps are fixed: never let a reconfigure (a recording branch being linked) reach
        # pipewiresrc, which then restarted the camera stream (10-17: squished 1x preview,
        # 6 s freeze on the 0.6 recording)
        caps.get_static_pad("src").add_probe(Gst.PadProbeType.EVENT_UPSTREAM, self._drop_reconfigure)
        tee.link(q1)
        q1.link(up)
        up.link(conv)
        conv.link(self.shader)
        self.shader.link(fixsize)
        fixsize.link(vtee)
        vtee.link(q3)
        q3.link(sink)
        self.vtee = vtee
        tee.link(q2)
        q2.link(grab)
        self.picture.set_paintable(sink.get_property("paintable"))
        bus = p.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error, retry)
        bus.connect("message::tag", self._on_tag)
        self.pipeline, self.sink, self.grab = p, sink, grab
        self.frames = 0
        grab.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._count_frame)
        p.set_state(Gst.State.PLAYING)
        gen = self.pipeline
        GLib.timeout_add(3000, self._watchdog, gen, retry)
        log("preview", lens["model"], w, h)

    def _count_frame(self, pad, info):
        self.frames += 1
        return Gst.PadProbeReturn.OK

    def _watchdog(self, gen, retry):
        """No frame 3 s after starting (camss refused the stream without a GStreamer error):
        start again once, then restart the camera services."""
        if self.pipeline is not gen or self.frames:
            return False
        log("no frames after 3 s", "-> retry" if retry else "-> restarting camera services")
        if retry:
            self.start_preview(retry=False)
        elif getattr(self, "_services_restarted", False):
            self.stop_preview()
            self.say("camera won't start · close and reopen")
        else:
            self._services_restarted = True
            self.stop_preview()
            self.say("restarting camera service…")
            threading.Thread(target=self._restart_pw, daemon=True).start()
        return False

    def stop_preview(self):
        if self.rec:              # the preview is going away under a recording: keep what we have
            self._rec_abandon()
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline.get_bus().remove_signal_watch()
            self.pipeline = self.sink = self.grab = None

    def _on_tag(self, bus, msg):
        ok, val = msg.parse_tag().get_string("image-orientation")
        if ok and val.startswith("rotate-"):
            self.tag_rot = int(val.split("-")[1])

    def _on_error(self, bus, msg, retry):
        err, dbg = msg.parse_error()
        log("pipeline error:", err.message)
        self.stop_preview()
        if retry:
            # 10-16: after a `cam` run in GPU mode WirePlumber's cameras failed to negotiate
            # until pipewire + wireplumber were restarted
            self.say("restarting camera service…")
            threading.Thread(target=self._restart_pw, daemon=True).start()
        else:
            self.say("camera error: " + err.message)

    def _restart_pw(self):
        restart_camera_services()

        def again():
            self.nodes = pw_nodes()
            self.start_preview(retry=False)
            self.say(None)
            return False
        GLib.idle_add(again)

    def switch_lens(self, key):
        if self.busy or self.rec:
            self._refresh_chips()
            return
        if key != self.conf["lens"]:
            self.conf["lens"] = key
            save_conf(self.conf)
            self._reset_focus()
            self.start_preview()
        self._refresh_chips()

    def _theme_changed(self):
        self._update_shader()
        self._refresh_chips()

    def _update_shader(self):
        if self.pipeline and self.shader:
            self.shader.set_property("fragment", self._shader())
            self.shader.set_property("update-shader", True)

    # ---------- controls ----------
    def _hq_toggled(self, btn):
        self.conf["hq"] = btn.get_active()
        save_conf(self.conf)
        self._refresh_chips()

    def _hdr_toggled(self, btn):
        self.conf["hdr"] = btn.get_active()
        save_conf(self.conf)
        self._refresh_chips()

    def _cycle_flash(self):
        order = ["off", "on", "torch"]
        self.conf["flash"] = order[(order.index(self.conf["flash"]) + 1) % 3]
        save_conf(self.conf)
        self._apply_flash()
        self._refresh_chips()

    def _apply_flash(self):
        self.led(self.conf["flash"] == "torch" and self.lens()["key"] != "front")

    def led(self, on):
        try:
            write_atomic_led(on)
        except OSError as e:
            log("torch:", e)

    def _toggle_mode(self):
        if self.rec or self.busy:
            return
        self.conf["mode"] = "photo" if self.conf["mode"] == "video" else "video"
        save_conf(self.conf)
        self._update_shader()          # no 3:2 frame dimming in video
        self._refresh_chips()

    def _cycle_film(self):
        if self.rec or self.busy:
            return
        order = film.ORDER
        cur = self.conf.get("film") if self.conf.get("film") in order else None
        self.conf["film"] = order[(order.index(cur) + 1) % len(order)]
        save_conf(self.conf)
        self._update_shader()
        self._refresh_chips()

    def _cycle_timer(self):
        order = [0, 3, 10]
        self.conf["timer"] = order[(order.index(self.conf["timer"]) + 1) % 3]
        save_conf(self.conf)
        self._refresh_chips()

    def _ev_changed(self, scale):
        v = round(scale.get_value() * 2) / 2
        if abs(v - scale.get_value()) > 1e-3:
            scale.set_value(v)
            return
        self.ev_label.set_label(f"EV {v:+g}" if v else "EV 0")
        write_atomic(EV_FILE, f"{v:g}")

    def _key(self, ctrl, keyval, code, state):
        if keyval in (Gdk.KEY_AudioRaiseVolume, Gdk.KEY_AudioLowerVolume, Gdk.KEY_space, Gdk.KEY_Return):
            self.shoot()
            return True
        return False

    def say(self, text):
        self.status.set_visible(bool(text))
        if text:
            self.status.set_label(text)

    # ---------- tap to focus ----------
    def _preview_tap(self, gesture, n, x, y):
        lens = self.lens()
        if self.busy or not lens["af"] or not self.grab:
            return
        pw, ph = self.picture.get_width(), self.picture.get_height()
        # the picture keeps the 3:4 frame centred (CONTAIN); map the tap into it
        fw, fh = (pw, pw * 4 / 3) if pw * 4 / 3 <= ph else (ph * 3 / 4, ph)
        ox, oy = (pw - fw) / 2, (ph - fh) / 2
        u, v = (x - ox) / fw, (y - oy) / fh
        if not (0 <= u <= 1 and 0 <= v <= 1):
            return
        self.focus_box.set_margin_start(int(x - 36))
        self.focus_box.set_margin_top(int(y - 36))
        self.focus_box.remove_css_class("locked")
        self.focus_box.set_visible(True)
        # portrait (u, v) -> sensor frame box: the sensor is landscape, turned 90° ccw to show
        if lens["rot"] == 90:
            su, sv = 1 - v, u
        else:
            su, sv = v, 1 - u
        r = 0.09
        box = (max(su - r, 0), max(sv - r * 4 / 3, 0), min(su + r, 1), min(sv + r * 4 / 3, 1))
        self.busy = True
        threading.Thread(target=self._focus_sweep, args=(lens, box), daemon=True).start()

    def _frame(self):
        grab = self.grab
        if not grab:
            return None
        sample = grab.emit("try-pull-sample", Gst.SECOND // 2)
        if not sample:
            return None
        buf, caps = sample.get_buffer(), sample.get_caps().get_structure(0)
        w, h = caps.get_value("width"), caps.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            stride = len(info.data) // h
            a = np.frombuffer(info.data, np.uint8).reshape(h, stride)[:, :w * 4].reshape(h, w, 4).copy()
        finally:
            buf.unmap(info)
        return a

    def _focus_sweep(self, lens, box):
        settle = 0.12 if self.afd_fast else 0.55     # afd re-reads the file every 0.4 s (FAST: 0.03)
        lo, hi = lens["af_range"]

        def measure(pos):
            write_atomic(lens["af"], str(int(pos)))
            time.sleep(settle)
            self._frame()                                # drop a frame taken mid-move
            f = self._frame()
            return sharpness(f, box) if f is not None else 0.0
        try:
            coarse = list(range(lo, hi + 1, 50))
            scores = [(measure(p), p) for p in coarse]
            best = max(scores)[1]
            fine = [p for p in range(best - 25, best + 26, 12) if lo <= p <= hi and p != best]
            scores = [(measure(p), p) for p in fine] + [max(scores)]
            best = max(scores)[1]
            write_atomic(lens["af"], str(int(best)))
            log("focus", lens["key"], "->", best)
        except Exception as e:
            log("focus failed:", e)
        GLib.idle_add(self._focus_done)

    def _focus_done(self):
        self.busy = False
        self.focus_box.add_css_class("locked")
        GLib.timeout_add(1200, lambda: (self.focus_box.set_visible(False), False)[1])
        return False

    # ---------- capture ----------
    def shoot(self):
        if self.rec:
            self.stop_record()
            return
        if self.busy or not self.pipeline:
            return
        if self.conf["timer"]:
            self.busy = True
            self.countdown = self.conf["timer"]
            self.big.set_label(str(self.countdown))
            self.big.set_visible(True)
            GLib.timeout_add(1000, self._tick)
            return
        self._capture()

    def _tick(self):
        self.countdown -= 1
        if self.countdown > 0:
            self.big.set_label(str(self.countdown))
            return True
        self.big.set_visible(False)
        self.busy = False
        self._capture()
        return False

    def _capture(self):
        if self.conf["mode"] == "video":
            self.start_record()
            return
        self.busy = True
        lens = self.lens()
        held = self.accel.orientation()
        theme = self.conf.get("film")      # the film stock (FX looks were removed 10-17)
        flash = self.conf["flash"] in ("on", "torch") and lens["key"] != "front"
        self.add_css_class("snap")
        GLib.timeout_add(140, lambda: (self.remove_css_class("snap"), False)[1])
        hq = self.conf["hq"] and lens["still"] is not None
        if not hq:
            if flash and self.conf["flash"] == "on":
                self.led(True)
                GLib.timeout_add(450, lambda: (self._grab_fast(lens, held, theme, True), False)[1])
            else:
                self._grab_fast(lens, held, theme, False)
            return
        # 12 MP: freeze the last preview frame on screen while `cam` owns the sensor
        tex = self.picture.get_paintable()
        if tex:
            self.freeze.set_paintable(tex.get_current_image())
            self.freeze.set_visible(True)
        self.say("HOLD STILL · HDR" if self.conf["hdr"] and lens["key"] == "main" else "HOLD STILL · 12 MP")
        self.stop_preview()
        if flash:
            self.led(True)
        threading.Thread(target=self._grab_full, args=(lens, held, theme, flash), daemon=True).start()

    def _grab_fast(self, lens, held, theme, led_was_on):
        f = self._frame()
        if led_was_on:
            self._apply_flash()
        if f is None:
            self.busy = False
            self.say("no frame")
            GLib.timeout_add(1500, lambda: (self.say(None), False)[1])
            return
        rgb = f[:, :, :3]
        threading.Thread(target=self._save, args=(rgb, lens, held, theme), daemon=True).start()
        self.busy = False

    def _grab_full(self, lens, held, theme, flash):
        for old in glob.glob(os.path.join(CACHE, "f-*.ppm")):
            os.remove(old)
        w, h = lens["still"]
        time.sleep(0.25)                          # let PipeWire release the sensor
        if lens["path"] not in self.cams:         # safe now: the preview is stopped
            self.cams.update(cam_indexes())
        if lens["path"] not in self.cams:
            log("cam index not found for", lens["path"])
            GLib.idle_add(self._after_full)
            GLib.idle_add(self.say, "12 MP: camera not found")
            return
        if lens["key"] == "main":
            return self._grab_raw(lens, held, theme, flash)
        env = dict(os.environ, LIBCAMERA_SOFTISP_MODE="cpu")
        cmd = ["cam", "-c", self.cams[lens["path"]], "-s",
               f"role=still,width={w},height={h},pixelformat=BGR888",
               f"-C{STILL_FRAMES}", "-F" + os.path.join(CACHE, "f-#.ppm")]
        t0 = time.time()
        try:
            subprocess.run(cmd, env=env, capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            log("cam timed out")
        frames = sorted(glob.glob(os.path.join(CACHE, "f-*.ppm")))
        log(f"cam: {len(frames)} frames in {time.time() - t0:.1f}s")
        GLib.idle_add(self._after_full)
        if flash and self.conf["flash"] != "torch":
            self.led(False)
        if not frames:
            GLib.idle_add(self.say, "12 MP capture failed")
            return
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file(frames[-1])
            rgb = pixbuf_to_rgb(pb)
        finally:
            for fr in frames:
                os.remove(fr)
        self._save(rgb, lens, held, theme)

    def _grab_raw(self, lens, held, theme, flash):
        """Main lens: raw frames developed by rawdev (the soft ISP's white balance is off, 10-22)."""
        hdr = bool(self.conf.get("hdr"))
        try:
            frames = rawdev.capture(self.cams[lens["path"]], EV_FILE, self.ev.get_value(), hdr=hdr)
        except Exception as e:
            log("raw capture failed:", e)
            frames = []
        GLib.idle_add(self._after_full)
        if flash and self.conf["flash"] != "torch":
            self.led(False)
        if not frames:
            GLib.idle_add(self.say, "12 MP capture failed")
            return
        GLib.timeout_add(800, lambda: (self.say("DEVELOPING…"), False)[1])
        try:
            rgb = rawdev.develop(frames)
        except Exception as e:
            log("develop failed:", e)
            GLib.idle_add(self.say, "develop failed")
            return
        finally:
            del frames
        self._save(rgb, lens, held, theme)
        GLib.idle_add(self.say, None)

    def _after_full(self):
        self.start_preview()
        GLib.timeout_add(700, self._unfreeze)
        self.busy = False
        return False

    def _unfreeze(self):
        self.freeze.set_visible(False)
        self.freeze.set_paintable(None)
        self.say(None)
        self._apply_flash()
        return False

    # ---------- video ----------
    def start_record(self):
        if self.rec or not self.pipeline or not self.vtee:
            return
        lens = self.lens()
        held = self.accel.orientation()
        ccw = (lens["rot"] + HELD_CCW.get(held, 0)) % 360
        w, h = (VID_SHORT, VID_LONG) if ccw in (90, 270) else (VID_LONG, VID_SHORT)
        os.makedirs(VID_DIR, exist_ok=True)
        name = datetime.datetime.now().strftime("VID_%Y%m%d_%H%M%S")
        path = os.path.join(VID_DIR, name + ".mp4")
        n = 1
        while os.path.exists(path):
            path = os.path.join(VID_DIR, f"{name}_{n}.mp4")
            n += 1
        part = path + ".part"             # the Gallery ignores it until it is finished
        desc = (
            "queue leaky=downstream max-size-buffers=4 ! "
            f"glvideoflip method={FLIP[ccw]} ! glcolorscale ! "
            f"video/x-raw(memory:GLMemory),width={w},height={h} ! "
            "glcolorconvert ! video/x-raw(memory:GLMemory),format=NV12 ! gldownload ! "
            # the camera caps carry no framerate (x264/mp4mux need one): constant 30 fps
            "videorate ! video/x-raw,framerate=30/1 ! "
            "queue leaky=downstream max-size-buffers=30 max-size-time=0 max-size-bytes=0 ! "
            f"x264enc speed-preset=superfast threads=6 rc-lookahead=10 bitrate={VID_KBPS} key-int-max=60 ! "
            "video/x-h264,profile=high ! h264parse ! "
            # 1 s fragments (a crash still leaves a playable file), rewritten into a plain MP4 at
            # the end (reserved-moov wasted ~30 MB per file)
            "mp4mux name=mux fragment-duration=1000 fragment-mode=first-moov-then-finalise ! "
            f'filesink name=fsink location="{part}" '
            "pulsesrc name=asrc ! audioconvert ! audioresample ! "
            "audio/x-raw,rate=48000,channels=2 ! fdkaacenc bitrate=128000 ! aacparse ! "
            "queue max-size-time=3000000000 ! mux.")
        try:
            rbin = Gst.parse_bin_from_description(desc, True)
        except GLib.Error as e:
            log("record: bad pipeline:", e.message)
            self.say("video failed")
            return
        p = self.pipeline
        # both branches start at running time 0 in the file
        start = p.get_clock().get_time() - p.get_base_time() if p.get_clock() else 0
        p.add(rbin)
        rbin.get_by_name("asrc").get_static_pad("src").set_offset(-start)
        pad = self.vtee.request_pad_simple("src_%u")
        pad.set_offset(-start)
        rbin.get_by_name("fsink").get_static_pad("sink").add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM, self._rec_eos_probe)
        # the camera's image-orientation tag would make mp4mux write a 90° display matrix on top
        # of the frames we already turned upright
        rbin.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._drop_tags)
        rbin.sync_state_with_parent()
        pad.link(rbin.get_static_pad("sink"))
        self.rec = dict(bin=rbin, pad=pad, path=path, part=part, t0=time.time(), done=False)
        self.rec["cookie"] = self.get_application().inhibit(
            self, Gtk.ApplicationInhibitFlags.IDLE | Gtk.ApplicationInhibitFlags.SUSPEND, "Recording video")
        if self.conf["flash"] != "off" and lens["key"] != "front":
            self.led(True)
        self.status.add_css_class("rec")
        self._rec_tick()
        GLib.timeout_add(500, self._rec_tick)
        self._refresh_chips()
        log("recording", path, f"{w}x{h}", "held", held)

    @staticmethod
    def _drop_reconfigure(pad, info):
        if info.get_event().type == Gst.EventType.RECONFIGURE:
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK

    @staticmethod
    def _drop_tags(pad, info):
        if info.get_event().type == Gst.EventType.TAG:
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK

    def _rec_tick(self):
        if not self.rec or self.rec.get("stopping"):
            return False
        s = int(time.time() - self.rec["t0"])
        self.say(f"● REC {s // 60}:{s % 60:02d}")
        return True

    def stop_record(self):
        rec = self.rec
        if not rec or rec.get("stopping"):
            return
        rec["stopping"] = True
        self.say("saving video…")
        rbin, pad = rec["bin"], rec["pad"]

        def unlink(tpad, info):
            if rec.get("unlinked"):
                return Gst.PadProbeReturn.REMOVE
            rec["unlinked"] = True
            sinkpad = rbin.get_static_pad("sink")
            tpad.unlink(sinkpad)
            self.vtee.release_request_pad(tpad)
            sinkpad.send_event(Gst.Event.new_eos())
            rbin.get_by_name("asrc").send_event(Gst.Event.new_eos())
            return Gst.PadProbeReturn.REMOVE
        pad.add_probe(Gst.PadProbeType.IDLE, unlink)
        GLib.timeout_add(6000, self._rec_finish, rec)   # in case EOS never reaches the file

    def _rec_eos_probe(self, pad, info):
        if info.get_event().type == Gst.EventType.EOS:
            rec = self.rec
            GLib.idle_add(self._rec_finish, rec)
        return Gst.PadProbeReturn.OK

    def _rec_finish(self, rec):
        if not rec or rec["done"]:
            return False
        rec["done"] = True
        rec["bin"].set_state(Gst.State.NULL)
        if self.pipeline and rec["bin"].get_parent() is self.pipeline:
            self.pipeline.remove(rec["bin"])
        try:
            os.replace(rec["part"], rec["path"])
            log("saved", rec["path"], f"{time.time() - rec['t0']:.1f}s")
        except OSError as e:
            log("video save failed:", e)
        self.get_application().uninhibit(rec["cookie"])
        if self.rec is rec:
            self.rec = None
        self.status.remove_css_class("rec")
        self.say(None)
        self._apply_flash()
        self._refresh_chips()
        tex = self.picture.get_paintable()
        img = tex.get_current_image() if tex else None
        if img:                                    # thumbnail = the last preview frame
            self.thumb_pic.set_paintable(img)
        if rec.get("then"):
            rec["then"]()
        return False

    def _rec_abandon(self):
        """The preview pipeline is being torn down mid-recording (error, background): the file
        is fragmented every second, so just close it and keep it."""
        rec, self.rec = self.rec, None
        try:
            rec["bin"].set_state(Gst.State.NULL)
            os.replace(rec["part"], rec["path"])
        except OSError:
            pass
        rec["done"] = True
        self.get_application().uninhibit(rec["cookie"])
        self.status.remove_css_class("rec")
        self._refresh_chips()
        log("recording cut short", rec["path"])

    def _save(self, rgb, lens, held, theme):
        """rgb: HxWx3 uint8 in sensor orientation -> upright, film look (theme = stock), JPEG."""
        try:
            if theme:
                rgb = film.develop(rgb, theme)
            ccw = (lens["rot"] + HELD_CCW.get(held, 0)) % 360
            rgb = np.rot90(rgb, k=ccw // 90)
            rgb = np.ascontiguousarray(rgb)
            if theme:
                rgb = film.watermark(rgb)
            h, w = rgb.shape[:2]
            pb = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(rgb.tobytes()), GdkPixbuf.Colorspace.RGB,
                                                 False, 8, w, h, w * 3)
            name = datetime.datetime.now().strftime("IMG_%Y%m%d_%H%M%S")
            path = os.path.join(OUT_DIR, name + ".jpg")
            n = 1
            while os.path.exists(path):
                path = os.path.join(OUT_DIR, f"{name}_{n}.jpg")
                n += 1
            pb.savev(path + ".part", "jpeg", ["quality"], ["92"])
            os.replace(path + ".part", path)
            log("saved", path, f"{w}x{h}")
            GLib.idle_add(self._load_thumb, path)
        except Exception as e:
            log("save failed:", e)
            GLib.idle_add(self.say, "save failed")

    def _load_thumb(self, path=None):
        if path is None:
            files = sorted(glob.glob(os.path.join(OUT_DIR, "*.jp*g")), key=os.path.getmtime)
            path = files[-1] if files else None
        if path:
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 160, 160, True)
                self.thumb_pic.set_pixbuf(pb) if hasattr(self.thumb_pic, "set_pixbuf") else None
            except GLib.Error:
                pass
        self.last_photo = path
        return False

    def open_gallery(self):
        subprocess.Popen(["gtk-launch", "dev.notareal.Gallery"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)

    # ---------- lifecycle ----------
    def _on_active(self, *_):
        # release the sensor (and the torch) when the app goes to the background
        if self.is_active():
            if not self.pipeline and not self.busy:
                self.start_preview()
                self._apply_flash()
        elif not self.busy and not self.rec:
            GLib.timeout_add(1500, self._pause_if_inactive)

    def _pause_if_inactive(self):
        if not self.is_active() and not self.busy and not self.rec:
            self.stop_preview()
            self.led(False)
        return False

    def _on_close(self, *_):
        if self.rec:                  # finish the file first, then close
            self.rec["then"] = self.close
            self.stop_record()
            return True
        self.stop_preview()
        self.led(False)
        write_atomic(EV_FILE, "0")
        self.accel.release()
        return False


def write_atomic_led(on):
    with open(TORCH, "w") as f:
        f.write("255" if on else "0")


def pixbuf_to_rgb(pb):
    w, h, rs, nc = pb.get_width(), pb.get_height(), pb.get_rowstride(), pb.get_n_channels()
    a = np.frombuffer(pb.get_pixels(), np.uint8)
    return np.lib.stride_tricks.as_strided(a, (h, w, 3), (rs, nc, 1)).copy()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.win = None

    def do_activate(self):
        if self.win is None:
            Gst.init(None)
            notareal_theme.apply(os.path.join(HERE, "style.css"))
            self.win = Window(self)
        self.win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
