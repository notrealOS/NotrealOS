#!/usr/bin/env python3
"""notreal.tube: one video hub for the phone (YouTube, TikTok, Instagram, Vimeo), no accounts.

All the work happens on the PC (~/videohub/hub.py, port 8099): it keeps the
subscription feed warm, searches, resolves streams ahead of time and pipes each video to us as
one plain fragmented MP4. So the phone only draws lists and plays a stream; a seek asks the PC for
a new stream from that second (t=). Offline copies live in ~/Videos/notreal.tube.

Left-handed: frequent buttons sit on the left (search, saved, play, subscribe, save).
"""
import collections
import concurrent.futures
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gst, Gtk, Pango  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.expanduser("~/theme"))
import notareal_theme  # noqa: E402

Gst.init(None)
# PulseAudio remembers mute/volume per application.name; every one of our Python apps was "python3", so
# the Reddit app's muted GIF videos left this app silent (10-22). Our own name gets its own memory.
GLib.set_prgname("nrtube")
GLib.set_application_name("notreal.tube")


def pc(key, default=""):
    """Address of our PC server; set in ~/.config/notreal/pc.json."""
    try:
        with open(os.path.expanduser("~/.config/notreal/pc.json")) as f:
            return json.load(f).get(key) or default
    except (OSError, ValueError):
        return default


APP_ID = "dev.notareal.Tube"
API = os.environ.get("NRTUBE_API", "http://%s:8099" % pc("host", "localhost"))
DATA_DIR = os.path.join(GLib.get_user_data_dir(), "nrtube")
POS_FILE = os.path.join(DATA_DIR, "positions.json")
PREFS_FILE = os.path.join(DATA_DIR, "prefs.json")
SAVED_DIR = os.path.join(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
                         or os.path.expanduser("~/Videos"), "notreal.tube")
SAVED_INDEX = os.path.join(SAVED_DIR, "index.json")
TEXTURE_CACHE = 80
SOURCES = [("all", "ALL"), ("youtube", "YOUTUBE"), ("tiktok", "TIKTOK"), ("instagram", "INSTAGRAM"),
           ("vimeo", "VIMEO")]
BADGE = {"youtube": "YT", "tiktok": "TT", "instagram": "IG", "vimeo": "VM"}
VERTICAL = ("tiktok", "instagram")      # these are mostly portrait: taller player
os.makedirs(DATA_DIR, exist_ok=True)


# --- plumbing ---------------------------------------------------------------------

def bg(fn, done=None, fail=None):
    """Run fn() in a thread, deliver the result (or exception) on the main loop."""
    def run():
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            print("bg error:", repr(e), flush=True)
            if fail:
                GLib.idle_add(lambda err=e: (fail(err), False)[1])
            return
        if done:
            GLib.idle_add(lambda: (done(res), False)[1])
    threading.Thread(target=run, daemon=True).start()


def api(endpoint, timeout=40, base=None, **q):
    url = (base or API) + endpoint + ("?" + urllib.parse.urlencode(q) if q else "")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error")
        except Exception:  # noqa: BLE001
            msg = None
        raise RuntimeError(msg or f"PC answered {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("can't reach the PC") from None


class Link:
    """Where streams come from. At home (Tailscale direct to the PC) the PC resolves + remuxes; on mobile
    data Tailscale relays through DERP (~0.2 MB/s, 10-22), so the phone runs the same hub code itself
    (~/tube/hub.py --local, 127.0.0.1:8098) and fetches straight from the site. Feed/search stay on the PC."""

    LOCAL = "http://127.0.0.1:8098"

    def __init__(self):
        self.mode = "pc"
        self.proc = None
        self.checking = False
        self.check()
        # after a network change Tailscale needs a moment to find (or lose) its direct path
        Gio.NetworkMonitor.get_default().connect("network-changed",
                                                 lambda *a: GLib.timeout_add(3000, lambda: self.check() and False))
        GLib.timeout_add_seconds(120, lambda: self.check() or True)

    def base(self):
        if self.mode == "phone":
            self.ensure_local()
            return self.LOCAL
        return API

    def check(self):
        if self.checking:
            return
        self.checking = True
        host = urllib.parse.urlparse(API).hostname

        def probe():
            import subprocess
            try:
                out = subprocess.run(["tailscale", "ping", "-c", "1", "--timeout=3s", host], capture_output=True,
                                     text=True, timeout=6).stdout
            except (OSError, subprocess.TimeoutExpired):
                out = ""
            return "phone" if ("DERP" in out or "pong" not in out) else "pc"

        def done(mode):
            self.checking = False
            if mode != self.mode:
                print("streams now via", mode, flush=True)
            self.mode = mode
            if mode == "phone":
                self.ensure_local()
        bg(probe, done, lambda e: setattr(self, "checking", False))

    def ensure_local(self):
        if self.proc and self.proc.poll() is None:
            return
        import subprocess
        log = open(os.path.join(GLib.get_user_runtime_dir(), "nrtube-hub.log"), "ab")
        self.proc = subprocess.Popen([sys.executable, os.path.join(HERE, "hub.py"), "--local"],
                                     stdout=log, stderr=log, stdin=subprocess.DEVNULL)
        import socket
        deadline = time.monotonic() + 4      # importing yt-dlp takes ~1.5 s; don't hand out a dead address
        while time.monotonic() < deadline and self.proc.poll() is None:
            try:
                socket.create_connection(("127.0.0.1", 8098), timeout=0.3).close()
                return
            except OSError:
                time.sleep(0.15)

    def prefetch(self, items, n=3):
        """On the phone each lookup costs ~1.7 s of CPU, so only the first few; the PC does its own."""
        if self.mode != "phone" or not items:
            return
        urls = "\n".join(i["url"] for i in items[:n])
        bg(lambda: api("/prefetch", base=self.LOCAL, u=urls))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def stream_uri(url, t=0, audio=False, h=720, base=None):
    return (base or LINK.base()) + "/play?" + urllib.parse.urlencode({"u": url, "t": f"{max(0, t):.1f}", "h": h,
                                                     "audio": "1" if audio else "0"})


def label(text="", css=None, xalign=0.0, wrap=False, lines=0):
    lb = Gtk.Label(label=text or "", xalign=xalign)
    if wrap:
        lb.set_wrap(True)
        lb.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        if lines:
            lb.set_lines(lines)
            lb.set_ellipsize(Pango.EllipsizeMode.END)
    else:
        lb.set_ellipsize(Pango.EllipsizeMode.END)
    for c in (css or "").split():
        lb.add_css_class(c)
    return lb


def short_num(n):
    if not n:
        return ""
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M".replace(".0M", "M")
    if n >= 1000:
        return f"{n / 1000:.0f}K"
    return str(n)


def fmt_time(s):
    s = int(max(0, s or 0))
    h, m, sec = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def ago(ts):
    if not ts:
        return ""
    d = time.time() - ts
    for span, unit in ((86400 * 365, "y"), (86400 * 30, "mo"), (86400 * 7, "w"), (86400, "d"), (3600, "h"),
                       (60, "min")):
        if d >= span:
            return f"{int(d // span)} {unit} ago"
    return "just now"


def meta_line(item):
    parts = [item.get("channel") or "", ago(item.get("published")),
             (short_num(item.get("views")) + " views") if item.get("views") else ""]
    return "  ·  ".join(p for p in parts if p)


def tappable(widget, fn):
    """Tap without claiming the touch on press: claiming it there stole every drag from the scrolled
    list, so lists only scrolled with a mouse wheel (10-22). A drag cancels the click by itself."""
    g = Gtk.GestureClick()
    g.connect("released", lambda g, n, x, y: (g.set_state(Gtk.EventSequenceState.CLAIMED), fn()))
    widget.add_controller(g)
    return widget


def flat_button(icon=None, text=None, css="flat", tip=None):
    b = Gtk.Button()
    if icon and text:
        b.set_child(Adw.ButtonContent(icon_name=icon, label=text))
    elif icon:
        b.set_icon_name(icon)
    else:
        b.set_label(text)
    for c in css.split():
        b.add_css_class(c)
    if tip:
        b.set_tooltip_text(tip)
    return b


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


class Positions:
    """Where you stopped in each video (resume + progress strip on the cards)."""

    def __init__(self):
        self.d = load_json(POS_FILE, {})
        self.dirty = False
        GLib.timeout_add_seconds(10, self._flush)

    def get(self, url):
        v = self.d.get(url)
        return (v[0], v[1]) if v else (0, 0)

    def set(self, url, pos, dur):
        if dur and pos > dur - 20:
            pos = dur        # finished
        self.d[url] = [round(pos, 1), round(dur or 0, 1), int(time.time())]
        self.dirty = True

    def _flush(self):
        if self.dirty:
            if len(self.d) > 3000:
                for k in sorted(self.d, key=lambda k: self.d[k][2])[:500]:
                    del self.d[k]
            save_json(POS_FILE, self.d)
            self.dirty = False
        return True


POS = Positions()


class Images:
    """Fetch + decode textures off the main loop, keep a small LRU of them."""

    def __init__(self):
        self.cache = collections.OrderedDict()
        self.waiting = {}
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)

    def get(self, url, cb):
        if not url:
            return
        tex = self.cache.get(url)
        if tex is not None:
            self.cache.move_to_end(url)
            cb(tex)
            return
        if url in self.waiting:
            self.waiting[url].append(cb)
            return
        self.waiting[url] = [cb]
        self.pool.submit(self._load, url)

    def _load(self, url):
        tex = None
        try:
            if url.startswith("/"):
                tex = Gdk.Texture.new_from_filename(url)
            else:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(r.read()))
        except Exception as e:  # noqa: BLE001
            print("image:", url[-60:], repr(e)[:80], flush=True)
        GLib.idle_add(self._deliver, url, tex)

    def _deliver(self, url, tex):
        cbs = self.waiting.pop(url, [])
        if tex is not None:
            self.cache[url] = tex
            while len(self.cache) > TEXTURE_CACHE:
                self.cache.popitem(last=False)
            for cb in cbs:
                cb(tex)
        return False


IMAGES = Images()


class Ratio(Gtk.Widget):
    """Holds one child at a fixed aspect ratio (height = width * ratio), height capped."""

    def __init__(self, child, ratio, max_h=10000):
        super().__init__(hexpand=True)
        self.child, self.ratio, self.max_h = child, ratio, max_h
        child.set_parent(self)

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 0, 0, -1, -1
        w = for_size if for_size > 0 else 360
        h = min(int(w * self.ratio), self.max_h)
        return h, h, -1, -1

    def do_size_allocate(self, w, h, baseline):
        self.child.allocate(w, h, baseline, None)

    def do_dispose(self):
        if self.child:
            self.child.unparent()
            self.child = None
        Gtk.Widget.do_dispose(self)


# --- other audio on the phone + staying awake ---------------------------------------

class Music:
    """notarealPlayer (HI-FI) owns the DAC directly; a video started over it would wait forever for the
    sound card. So a video stops the music first (Stop releases the DAC at once, Pause holds it 20 s)."""

    SPIN = "org.mpris.MediaPlayer2.notarealPlayer"

    @staticmethod
    def release():
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            names = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                  "ListNames", None, None, 0, 500, None).unpack()[0]
        except GLib.Error:
            return
        for n in names:
            if not n.startswith("org.mpris.MediaPlayer2.") or n.endswith(".nrtube"):
                continue
            try:
                st = bus.call_sync(n, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties", "Get",
                                   GLib.Variant("(ss)", ("org.mpris.MediaPlayer2.Player", "PlaybackStatus")),
                                   None, 0, 300, None).unpack()[0]
            except GLib.Error:
                continue
            method = "Stop" if n == Music.SPIN and st in ("Playing", "Paused") else ("Pause" if st == "Playing" else None)
            if method:
                bus.call(n, "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", method,
                         None, None, 0, 1000, None, None, None)

    @staticmethod
    def card_held():
        import glob
        for f in glob.glob("/proc/asound/card*/pcm*p/sub*/status"):
            try:
                m = re.search(r"owner_pid\s*:\s*(\d+)", open(f).read())
                if not m:
                    continue
                # owner_pid is a thread id (PulseAudio's "alsa-sink-Multi"): judge by its process (10-22)
                pid = re.search(r"^Tgid:\s*(\d+)", open(f"/proc/{m.group(1)}/status").read(), re.M).group(1)
                if int(pid) == os.getpid():
                    continue
                if open(f"/proc/{pid}/comm").read().strip() not in ("pulseaudio", "pipewire", "wireplumber"):
                    return True
            except OSError:
                pass
        return False


class Awake:
    """logind sleep lock while something plays, so sleepd doesn't suspend mid-video (audio-only with the
    screen off). Gtk.Application.inhibit registers nothing under Phosh."""

    def __init__(self):
        self.fd = None

    def hold(self, on):
        if on and self.fd is None:
            try:
                sysbus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
                r, fds = sysbus.call_with_unix_fd_list_sync(
                    "org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                    "Inhibit", GLib.Variant("(ssss)", ("sleep", "notreal.tube", "Playing video", "block")),
                    GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, 2000, None, None)
                self.fd = fds.get(r.unpack()[0])
            except GLib.Error as e:
                print("inhibit failed:", e.message, flush=True)
        elif not on and self.fd is not None:
            os.close(self.fd)
            self.fd = None


AWAKE = Awake()
LINK = None      # Link(), made at startup


# --- player -------------------------------------------------------------------------

class Player(Gtk.Overlay):
    """playbin -> gtk4paintablesink over a poster picture. A PC stream can't seek by itself (it's a live
    remux), so seeking reloads the stream from that second and position = base + pipeline position.
    Local files seek normally. Controls left-first for the left thumb."""

    def __init__(self, page):
        super().__init__(hexpand=True, vexpand=True)
        self.add_css_class("player")
        self.page = page
        self.uri_for = None          # t -> uri (stream) or None for a local file
        self.local = None
        self.base = 0.0
        self.duration = 0.0
        self.want_play = True
        self.buffering = False
        self.started = False
        self.seek_id = self.hide_id = 0
        self.pending_seek = None
        self.dragging = False
        self.pb = Gst.ElementFactory.make("playbin")
        sink = Gst.ElementFactory.make("gtk4paintablesink")
        self.pb.set_property("video-sink", sink)
        self.pb.set_property("mute", False)
        self.pb.set_property("buffer-size", 4 * 1024 * 1024)     # ~20 s of 720p, filled in ~1-2 s
        self.pb.set_property("buffer-duration", 8 * Gst.SECOND)
        self.poster = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, hexpand=True, vexpand=True, can_shrink=True)
        self.set_child(self.poster)
        self.pic = Gtk.Picture(paintable=sink.props.paintable, content_fit=Gtk.ContentFit.CONTAIN,
                               hexpand=True, vexpand=True, can_shrink=True)
        self.pic.set_visible(False)
        self.add_overlay(self.pic)
        self.spinner = Adw.Spinner(width_request=44, height_request=44, halign=Gtk.Align.CENTER,
                                   valign=Gtk.Align.CENTER, can_target=False)
        self.add_overlay(self.spinner)
        self.buf_lb = label("", "buf-pct", xalign=0.5)
        self.buf_lb.set_halign(Gtk.Align.CENTER)
        self.buf_lb.set_valign(Gtk.Align.CENTER)
        self.buf_lb.set_margin_top(80)
        self.buf_lb.set_visible(False)
        self.add_overlay(self.buf_lb)
        self.msg = label("", "player-msg", xalign=0.5, wrap=True)
        self.msg.set_valign(Gtk.Align.CENTER)
        self.msg.set_visible(False)
        self.add_overlay(self.msg)
        self.skip_note = label("", "skip-note")
        self.skip_note.set_halign(Gtk.Align.START)
        self.skip_note.set_valign(Gtk.Align.START)
        self.skip_note.set_visible(False)
        self.add_overlay(self.skip_note)

        self.bar = Gtk.Box(spacing=2, valign=Gtk.Align.END)
        self.bar.add_css_class("player-bar")
        self.play_btn = flat_button("media-playback-pause-symbolic")
        self.play_btn.connect("clicked", lambda b: self.toggle())
        self.time = label("0:00", "player-time")
        self.time.set_ellipsize(Pango.EllipsizeMode.NONE)
        self.scale = Gtk.Scale(hexpand=True, draw_value=False)
        self.scale.set_range(0, 1)
        self.scale.connect("change-value", self._on_scale)
        self.dur_lb = label("", "player-time")
        self.dur_lb.set_ellipsize(Pango.EllipsizeMode.NONE)
        self.full_btn = flat_button("view-fullscreen-symbolic")
        self.full_btn.connect("clicked", lambda b: self.page.toggle_full())
        for w in (self.play_btn, self.time, self.scale, self.dur_lb, self.full_btn):
            self.bar.append(w)
        self.add_overlay(self.bar)

        tap = Gtk.GestureClick()
        tap.connect("released", self._tap)
        self.add_controller(tap)
        bus = self.pb.get_bus()
        bus.add_signal_watch()
        self.bus_id = bus.connect("message", self._msg)
        self.tick_id = GLib.timeout_add(250, self._tick)

    # -- loading
    def set_poster(self, tex):
        if not self.started:
            self.poster.set_paintable(tex)

    def open_stream(self, uri_for, t=0.0):
        self.uri_for, self.local = uri_for, None
        self._load(t)

    def open_file(self, path, t=0.0):
        self.uri_for, self.local = None, path
        self.pb.set_state(Gst.State.NULL)
        self.pb.set_property("uri", Gio.File.new_for_path(path).get_uri())
        self.base = 0.0
        self.pending_seek = t if t > 1 else None
        self._start()

    def _load(self, t):
        self.pb.set_state(Gst.State.NULL)
        self.base = max(0.0, t)
        self.pb.set_property("uri", self.uri_for(self.base))
        self._start()

    def _start(self):
        self.msg.set_visible(False)
        self.spinner.set_visible(True)
        if Music.card_held():          # HI-FI owns the DAC: ask it to let go, start once it has
            Music.release()
            deadline = time.monotonic() + 2.5

            def wait():
                if Music.card_held() and time.monotonic() < deadline:
                    return True
                self._go()
                return False
            GLib.timeout_add(150, wait)
        else:
            Music.release()
            self._go()
        self._show_bar()

    def _go(self):
        if self.pb:
            self.pb.set_state(Gst.State.PLAYING if self.want_play else Gst.State.PAUSED)
            self._icons()

    # -- state
    def position(self):
        ok, pos = self.pb.query_position(Gst.Format.TIME) if self.pb else (False, 0)
        return self.base + (pos / Gst.SECOND if ok and pos > 0 else 0)

    def seek(self, t):
        t = max(0.0, min(t, self.duration - 1 if self.duration > 2 else t))
        if self.local:
            self.pb.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, int(t * Gst.SECOND))
        else:
            self.started = False
            self._load(t)
        self.time.set_text(fmt_time(t))
        self.scale.set_value(t)

    def toggle(self):
        self.play() if not self.want_play else self.pause()

    def play(self):
        self.want_play = True
        if self.ended():
            self.seek(0)
        elif not self.buffering:
            self.pb.set_state(Gst.State.PLAYING)
        self._icons()
        self._show_bar()

    def pause(self):
        self.want_play = False
        self.pb.set_state(Gst.State.PAUSED)
        self._icons()
        self._show_bar(stay=True)

    def ended(self):
        return getattr(self, "_eos", False)

    def stop(self):
        for attr in ("tick_id", "hide_id", "seek_id"):
            if getattr(self, attr):
                GLib.source_remove(getattr(self, attr))
                setattr(self, attr, 0)
        if self.pb:
            self.pb.set_state(Gst.State.NULL)
            bus = self.pb.get_bus()
            bus.disconnect(self.bus_id)
            bus.remove_signal_watch()
            self.pb = None
        AWAKE.hold(False)

    def show_video(self, on):
        self.pic.set_visible(on and self.started)

    def _msg(self, bus, m):
        t = m.type
        if t == Gst.MessageType.BUFFERING:
            # The user would rather wait a moment than have it stall (10-22): hold until the buffer is full,
            # at the start and after any stall, with the percentage over the spinner.
            pct = m.parse_buffering()
            self.buffering = pct < 100
            self.spinner.set_visible(self.buffering)
            self.buf_lb.set_text(f"{pct}%" if self.buffering else "")
            self.buf_lb.set_visible(self.buffering)
            if self.want_play:
                self.pb.set_state(Gst.State.PAUSED if self.buffering else Gst.State.PLAYING)
        elif t == Gst.MessageType.EOS:
            self._eos = True
            self.want_play = False
            self.pb.set_state(Gst.State.PAUSED)
            self._icons()
            self._show_bar(stay=True)
            self.page.on_ended()
        elif t == Gst.MessageType.ERROR:
            err, dbg = m.parse_error()
            print("video:", err.message, dbg and dbg[-200:], flush=True)
            self.spinner.set_visible(False)
            self.page.on_error(err.message)
        elif t == Gst.MessageType.ASYNC_DONE:
            if self.pending_seek is not None:
                pos, self.pending_seek = self.pending_seek, None
                self.pb.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                                    int(pos * Gst.SECOND))
        elif t == Gst.MessageType.STATE_CHANGED and m.src == self.pb:
            new = m.parse_state_changed()[1]
            if new == Gst.State.PLAYING:
                self._eos = False
                if not self.started:
                    self.started = True
                    self.page.on_started()
                self.pic.set_visible(not self.page.audio_only)
            if new in (Gst.State.PAUSED, Gst.State.PLAYING) and not self.buffering:
                self.spinner.set_visible(False)
            AWAKE.hold(new == Gst.State.PLAYING)

    def _tick(self):
        if not self.pb:
            return False
        if self.local and not self.duration:
            ok, d = self.pb.query_duration(Gst.Format.TIME)
            if ok and d > 0:
                self.set_duration(d / Gst.SECOND)
        pos = self.position()
        if not self.dragging and self.started:
            self.scale.set_value(pos)
            self.time.set_text(fmt_time(pos))
        if self.started and self.want_play:
            self.page.on_tick(pos)
        return True

    def set_duration(self, d):
        self.duration = float(d or 0)
        if self.duration > 0:
            self.scale.set_range(0, self.duration)
            self.dur_lb.set_text(fmt_time(self.duration))

    def _on_scale(self, scale, scroll, value):
        # dragging streams one value per pixel: only reload the stream once the finger settles
        self.dragging = True
        self.time.set_text(fmt_time(value))
        if self.seek_id:
            GLib.source_remove(self.seek_id)

        def go():
            self.seek_id = 0
            self.dragging = False
            self.seek(value)
            return False
        self.seek_id = GLib.timeout_add(350, go)
        self._show_bar()
        return False

    def _tap(self, g, n, x, y):
        w = self.get_width()
        if n == 2 and self.duration:      # double tap: left third back 10 s, right third ahead 10 s
            if x < w / 3:
                self.seek(self.position() - 10)
                return
            if x > 2 * w / 3:
                self.seek(self.position() + 10)
                return
        if n == 1:
            if self.bar.get_visible() and self.want_play:
                self.bar.set_visible(False)
            else:
                self._show_bar()

    def _icons(self):
        self.play_btn.set_icon_name("media-playback-pause-symbolic" if self.want_play
                                    else "media-playback-start-symbolic")

    def _show_bar(self, stay=False):
        self.bar.set_visible(True)
        if self.hide_id:
            GLib.source_remove(self.hide_id)
            self.hide_id = 0
        if not stay and self.want_play and not self.page.audio_only:
            def hide():
                self.hide_id = 0
                if self.want_play and not self.page.audio_only:
                    self.bar.set_visible(False)
                return False
            self.hide_id = GLib.timeout_add(3500, hide)

    def note(self, text):
        self.skip_note.set_text(text)
        self.skip_note.set_visible(True)
        GLib.timeout_add(2200, lambda: self.skip_note.set_visible(False) or False)


# --- cards + lists ------------------------------------------------------------------

class Thumb(Gtk.Overlay):
    def __init__(self, item):
        super().__init__()
        self.pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.pic.add_css_class("thumb")
        self.set_child(Ratio(self.pic, 9 / 16))
        self.dur = label("", "badge")
        self.dur.set_ellipsize(Pango.EllipsizeMode.NONE)
        self.dur.set_halign(Gtk.Align.START)          # left: where the left thumb rests
        self.dur.set_valign(Gtk.Align.END)
        self.add_overlay(self.dur)
        src = item.get("src")
        if src and src != "youtube":
            b = label(BADGE.get(src, "WEB"), "src-badge " + src)
            b.set_halign(Gtk.Align.START)
            b.set_valign(Gtk.Align.START)
            self.add_overlay(b)
        self.progress = Gtk.ProgressBar(valign=Gtk.Align.END, hexpand=True)
        self.progress.add_css_class("watched")
        self.add_overlay(self.progress)
        self.update(item)

    def update(self, item):
        if item.get("live"):
            self.dur.set_text("LIVE")
        elif item.get("short"):
            self.dur.set_text("SHORT")
        else:
            self.dur.set_text(fmt_time(item["duration"]) if item.get("duration") else "")
        self.dur.set_visible(bool(self.dur.get_text()))
        pos, dur = POS.get(item["url"])
        dur = dur or item.get("duration") or 0
        self.progress.set_visible(pos > 5 and dur > 0)
        if dur:
            self.progress.set_fraction(min(1, pos / dur))
        IMAGES.get(item.get("thumb"), self.pic.set_paintable)


class VideoCard(Gtk.Box):
    def __init__(self, item, on_open, on_channel=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class("card")
        self.item = item
        self.thumb = Thumb(item)
        self.append(self.thumb)
        self.title = label(item.get("title") or "", "card-title", wrap=True, lines=2)
        self.append(self.title)
        self.meta = label(meta_line(item), "card-meta")
        self.append(self.meta)
        pos, dur = POS.get(item["url"])
        if dur and pos >= dur:
            self.add_css_class("seen")
        tappable(self, lambda: on_open(self.item))

    def update(self, extra):
        self.item.update({k: v for k, v in extra.items() if v})
        self.title.set_text(self.item.get("title") or "")
        self.meta.set_text(meta_line(self.item))
        self.thumb.update(self.item)


class VideoList(Gtk.ScrolledWindow):
    """Cards in a column; asks for the next page near the bottom."""

    def __init__(self, win, more=None, header=None):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.win, self.more = win, more
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.box.add_css_class("list")
        if header:
            self.box.append(header)
        self.set_child(self.box)
        self.cards = {}
        self.loading = False
        self.status = label("", "status", xalign=0.5, wrap=True)
        self.box.append(self.status)
        self.get_vadjustment().connect("value-changed", self._scrolled)

    def clear(self):
        for c in list(self.cards.values()):
            self.box.remove(c)
        self.cards.clear()
        self.get_vadjustment().set_value(0)

    def add(self, items):
        for it in items:
            if it["url"] in self.cards:
                continue
            c = VideoCard(it, self.win.open_video, self.win.open_channel)
            self.cards[it["url"]] = c
            self.box.insert_child_after(c, self._last())
        self.set_status("" if self.cards else "nothing here")

    def _last(self):
        last = None
        child = self.box.get_first_child()
        while child is not None and child is not self.status:
            last = child
            child = child.get_next_sibling()
        return last

    def remove_url(self, url):
        c = self.cards.pop(url, None)
        if c:
            self.box.remove(c)

    def set_status(self, text, spin=False):
        self.status.set_text(text)
        self.status.set_visible(bool(text))

    def _scrolled(self, adj):
        if self.more and not self.loading and adj.get_value() + adj.get_page_size() > adj.get_upper() - 2500:
            self.more()


# --- pages --------------------------------------------------------------------------

def header(title_widget=None, start=(), end=()):
    hb = Adw.HeaderBar(show_title=True)
    hb.add_css_class("tube-bar")
    if title_widget is not None:
        hb.set_title_widget(title_widget)
    for w in start:
        hb.pack_start(w)
    for w in end:
        hb.pack_end(w)
    return hb


def app_title(text, long=False):
    lb = label(text, "tube-title" + (" long" if long else ""))
    lb.set_ellipsize(Pango.EllipsizeMode.END if long else Pango.EllipsizeMode.NONE)
    return lb


class HomePage(Adw.NavigationPage):
    def __init__(self, win):
        super().__init__(title="notreal.tube", tag="home")
        self.win = win
        self.page = 0
        self.has_more = True
        search = flat_button("system-search-symbolic", tip="Search")
        search.connect("clicked", lambda b: win.open_search())
        saved = flat_button("folder-download-symbolic", tip="Saved offline")
        saved.connect("clicked", lambda b: win.open_saved())
        refresh = flat_button("view-refresh-symbolic", tip="Refresh")
        refresh.connect("clicked", lambda b: self.load(refresh=True))
        self.prefs = load_json(PREFS_FILE, {})
        self.shorts = Gtk.ToggleButton(label="SHORTS", active=self.prefs.get("shorts", False))
        self.shorts.add_css_class("chip")
        self.shorts.add_css_class("bar-chip")
        self.shorts.set_tooltip_text("Show YouTube Shorts in the feed")
        self.shorts.connect("toggled", self._shorts)
        tv = Adw.ToolbarView()
        tv.add_top_bar(header(app_title("NOTREAL.TUBE"), start=(search, saved), end=(refresh, self.shorts)))
        self.list = VideoList(win, more=self.next_page)
        tv.set_content(self.list)
        self.set_child(tv)
        self.load()

    def _shorts(self, b):
        self.prefs["shorts"] = b.get_active()
        save_json(PREFS_FILE, self.prefs)
        self.load()

    def load(self, refresh=False):
        self.page = 0
        self.has_more = True
        self.list.clear()
        self.list.set_status("refreshing your channels…" if refresh else "loading…")
        self.list.loading = True
        shorts = "1" if self.shorts.get_active() else "0"
        bg(lambda: api("/feed", timeout=90, page=0, shorts=shorts, refresh="1" if refresh else "0"), self._got,
           self._fail)

    def next_page(self):
        if not self.has_more:
            return
        self.list.loading = True
        shorts = "1" if self.shorts.get_active() else "0"
        bg(lambda: api("/feed", page=self.page + 1, shorts=shorts), self._got_more, self._fail)

    def _got(self, d):
        self.list.loading = False
        self.has_more = d.get("more")
        self.list.add(d["items"])
        if self.page == 0:
            LINK.prefetch(d["items"])
        if not d["items"]:
            self.list.set_status("no subscriptions yet: search for a channel and tap FOLLOW"
                                 if not d.get("subs") else "nothing new")

    def _got_more(self, d):
        self.page += 1
        self._got(d)

    def _fail(self, e):
        self.list.loading = False
        self.list.set_status(f"{e}\ntap ⟳ to try again")


class SearchPage(Adw.NavigationPage):
    def __init__(self, win):
        super().__init__(title="Search", tag="search")
        self.win = win
        self.src = "all"
        self.query = ""
        self.gen = 0
        self.entry = Gtk.SearchEntry(hexpand=True, placeholder_text="search videos")
        self.entry.connect("activate", lambda e: self.run())
        tv = Adw.ToolbarView()
        tv.add_top_bar(header(self.entry))
        chips = Gtk.Box(spacing=6)
        chips.add_css_class("chips")
        first = None
        for key, text in SOURCES:
            b = Gtk.ToggleButton(label=text)
            b.add_css_class("chip")
            if first:
                b.set_group(first)
            else:
                first = b
                b.set_active(True)
            b.connect("toggled", lambda b, k=key: b.get_active() and self.set_src(k))
            chips.append(b)
        sc = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, child=chips)
        tv.add_top_bar(sc)
        self.list = VideoList(win)
        self.list.set_status("YouTube, TikTok, Instagram and Vimeo in one search")
        tv.set_content(self.list)
        self.set_child(tv)
        self.connect("shown", lambda p: self.entry.grab_focus())

    def set_src(self, key):
        self.src = key
        if self.query:
            self.run()

    def run(self):
        q = self.entry.get_text().strip()
        if not q:
            return
        self.query = q
        self.gen += 1
        gen = self.gen
        self.list.clear()
        self.list.set_status("searching…")
        if re.match(r"https?://", q):          # a pasted link: just play it
            self.win.open_video({"url": q, "title": "", "src": ""})
            self.list.set_status("")
            return
        src = self.src
        bg(lambda: api("/search", q=q, src=src), lambda d: self._got(gen, d), self._fail)

    def _got(self, gen, d):
        if gen != self.gen:
            return
        self.list.add(d["items"])
        LINK.prefetch([i for i in d["items"] if not i.get("unverified")])
        if not d["items"]:
            self.list.set_status("nothing found")
        self.warmed = {}
        pending = [i["url"] for i in d["items"] if i.get("unverified")]
        if pending:
            self._check(gen, pending, time.monotonic())

    def _check(self, gen, pending, since):
        """Site results come from a web search: the PC tries each one; drop what won't play, fill in
        thumbnails and lengths for the rest."""
        def done(res):
            if gen != self.gen:
                return
            left = []
            warm = []
            for u in pending:
                r = res.get(u)
                if r is None:
                    left.append(u)
                elif not r.get("ok"):
                    self.list.remove_url(u)
                elif u in self.list.cards:
                    c = self.list.cards[u]
                    c.update({"thumb": r.get("thumb"), "duration": r.get("duration"),
                              "title": c.item.get("title") or r.get("title"),
                              "channel": c.item.get("channel") or r.get("channel")})
                    c.item.pop("unverified", None)
                    src = c.item.get("src")
                    self.warmed[src] = self.warmed.get(src, 0) + 1
                    if self.warmed[src] <= 2:      # the first couple per site: likely taps, look them up now
                        warm.append(c.item)
            LINK.prefetch(warm, n=len(warm))
            if left and time.monotonic() - since < 40:
                GLib.timeout_add(1500, lambda: self._check(gen, left, since) or False)
        bg(lambda: api("/check", u="\n".join(pending)), done)

    def _fail(self, e):
        self.list.set_status(str(e))


class ChannelPage(Adw.NavigationPage):
    def __init__(self, win, src, cid, name):
        super().__init__(title=name or cid)
        self.win, self.src, self.cid, self.name = win, src, cid, name
        self.follow = Gtk.Button(label="FOLLOW")
        self.follow.add_css_class("pill")
        self.follow.set_sensitive(False)
        self.follow.connect("clicked", lambda b: self.toggle())
        self.subscribed = False
        tv = Adw.ToolbarView()
        tv.add_top_bar(header(app_title(name or cid, long=True), start=(self.follow,)))
        self.list = VideoList(win)
        self.list.set_status("loading…")
        tv.set_content(self.list)
        self.set_child(tv)
        bg(lambda: api("/channel", src=src, id=cid), self._got, lambda e: self.list.set_status(str(e)))

    def _got(self, d):
        for it in d["items"]:
            it.setdefault("channel", self.name)
        self.list.add(d["items"])
        self.set_subscribed(d.get("subscribed"))
        self.follow.set_sensitive(True)

    def set_subscribed(self, on):
        self.subscribed = bool(on)
        self.follow.set_label("FOLLOWING" if on else "FOLLOW")
        (self.follow.add_css_class if on else self.follow.remove_css_class)("on")

    def toggle(self):
        on = not self.subscribed
        self.set_subscribed(on)
        bg(lambda: api("/sub", src=self.src, id=self.cid, name=self.name, on="1" if on else "0"),
           lambda d: self.win.subs_changed(), lambda e: (self.set_subscribed(not on), self.win.toast(str(e))))


class WatchPage(Adw.NavigationPage):
    def __init__(self, win, item, local=None):
        super().__init__(title=item.get("title") or "video")
        self.win, self.item, self.local = win, dict(item), local
        self.url = item["url"]
        self.base = LINK.base()         # /info from the same hub that streams, so they share one lookup
        self.info = None
        self.audio_only = bool(local and local.get("audio"))
        self.skipped = set()
        self.full = False
        src = item.get("src") or ""
        self.player = Player(self)
        self.player.set_duration(item.get("duration"))
        self.frame = Ratio(self.player, 16 / 9 if src not in VERTICAL else 4 / 3)

        self.tv = Adw.ToolbarView()
        self.tv.add_top_bar(header(app_title(BADGE.get(src, "") and f"{BADGE[src]}  ·  {item.get('channel') or ''}"
                                             or (item.get("channel") or ""), long=True)))
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.frame)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        info.add_css_class("watch-info")
        self.title = label(item.get("title") or "", "watch-title", wrap=True)
        info.append(self.title)
        self.meta = label(meta_line(item), "watch-meta", wrap=True)
        info.append(self.meta)

        acts = Gtk.Box(spacing=8)                       # left-first: the actions you use most
        self.follow = Gtk.Button(label="FOLLOW")
        self.follow.add_css_class("pill")
        self.follow.connect("clicked", lambda b: self.toggle_follow())
        self.follow.set_visible(False)
        self.audio_btn = Gtk.ToggleButton(label="AUDIO ONLY", active=self.audio_only)
        self.audio_btn.add_css_class("pill")
        self.audio_btn.connect("toggled", lambda b: self.set_audio(b.get_active()))
        self.save_btn = Gtk.Button(label="SAVE OFFLINE")
        self.save_btn.add_css_class("pill")
        self.save_btn.connect("clicked", lambda b: self.save())
        for w in (self.follow, self.audio_btn, self.save_btn):
            acts.append(w)
        acts_sc = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, child=acts)
        info.append(acts_sc)
        if local:
            self.save_btn.set_visible(False)
            self.audio_btn.set_visible(False)
        self.channel_row = label("", "channel-link")
        tappable(self.channel_row, self.open_channel)
        info.append(self.channel_row)
        self.desc = label("", "desc", wrap=True)
        self.desc.set_ellipsize(Pango.EllipsizeMode.NONE)
        self.desc_full = ""
        self.desc_open = False
        tappable(self.desc, self._toggle_desc)
        info.append(self.desc)
        self.info_sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=info)
        body.append(self.info_sc)
        self.tv.set_content(body)
        self.set_child(self.tv)

        IMAGES.get(item.get("thumb"), self.player.set_poster)
        resume, dur = POS.get(self.url)
        start = resume if 20 < resume < (dur or 1e9) - 20 else 0
        if local:
            self.player.open_file(local["file"], start)
            self._show_saved_saving()
        else:
            self.player.open_stream(self._uri, start)       # play at once; details arrive alongside
            bg(lambda: api("/info", base=self.base, u=self.url), self._got_info, lambda e: None)
        self.win.downloads.watch(self.url, self._show_saved_saving)
        self.connect("hidden", lambda p: self.close())

    def _uri(self, t):
        return stream_uri(self.url, t, self.audio_only, base=self.base)

    def _got_info(self, info):
        self.info = info
        self.item.update({k: v for k, v in info.items() if v and k in ("title", "channel", "channel_id",
                                                                         "duration", "views", "published")})
        self.title.set_text(self.item.get("title") or "")
        self.meta.set_text(meta_line(self.item))
        if info.get("duration") and not self.player.duration:
            self.player.set_duration(info["duration"])
        if info.get("width") and info.get("height") and not self.full:
            # portrait videos get a tall frame, capped so the title and buttons stay on screen
            self.frame.ratio = min(4 / 3, info["height"] / info["width"])
            self.frame.queue_resize()
        if not self.player.started and info.get("thumb"):
            IMAGES.get(info["thumb"], self.player.set_poster)
        self.desc_full = (info.get("description") or "").strip()
        self._toggle_desc(keep=True)
        if self.item.get("channel"):
            self.channel_row.set_text(self.item["channel"] + "  ›")
        if info.get("src") in ("youtube", "tiktok") and self.item.get("channel_id"):
            self.follow.set_visible(True)
            self.set_followed(self.win.is_followed(info["src"], self.item["channel_id"]))
        n = len(info.get("sponsor") or [])
        if n:
            self.player.note(f"will skip {n} sponsor part{'s' if n > 1 else ''}")

    def _toggle_desc(self, keep=False):
        """Pango limits lines per paragraph, so a multi-paragraph description can't be capped by
        set_lines: show the first few lines ourselves, all of it on tap."""
        if not keep:
            self.desc_open = not self.desc_open
        text = self.desc_full
        lines = text.split("\n")
        short = "\n".join(lines[:3])
        if len(short) > 240:
            short = short[:240].rsplit(" ", 1)[0]
        cut = short != text
        self.desc.set_text(text if self.desc_open or not cut else short + "  … more")

    # -- player callbacks
    def on_started(self):
        self.player.poster.set_visible(self.audio_only)

    def on_tick(self, pos):
        POS.set(self.url, pos, self.player.duration)
        for seg in (self.info or {}).get("sponsor") or []:
            s, e = seg[0], seg[1]
            key = (s, e)
            if s <= pos < e - 1 and key not in self.skipped:
                self.skipped.add(key)
                self.player.seek(e)
                self.player.note("skipped " + {"selfpromo": "self-promo", "interaction": "reminder"}.get(seg[2], seg[2]))
                return

    def on_ended(self):
        POS.set(self.url, self.player.duration or self.player.position(), self.player.duration)

    def on_error(self, message):
        def show(text):
            self.player.msg.set_text(text)
            self.player.msg.set_visible(True)
        if self.local:
            return show("can't play this file")
        show("loading failed… asking the PC why")
        bg(lambda: api("/info", base=self.base, u=self.url), lambda i: show("couldn't play this stream, tap ▶ to retry"),
           lambda e: show(f"can't play this video:\n{e}"))

    # -- actions
    def set_audio(self, on):
        if on == self.audio_only:
            return
        self.audio_only = on
        pos = self.player.position()
        self.player.poster.set_visible(True)
        self.player.show_video(not on)
        if not self.local:
            self.player.started = False
            self.player.open_stream(self._uri, pos)
        self.save_btn.set_label("SAVE AUDIO" if on else "SAVE OFFLINE")
        self._show_saved_saving()

    def save(self):
        self.win.downloads.start(self.item, self.audio_only)
        self._show_saved_saving()

    def _show_saved_saving(self, *_):
        if self.local:
            return
        st = self.win.downloads.state(self.url, self.audio_only)
        if st is None:
            self.save_btn.set_label("SAVE AUDIO" if self.audio_only else "SAVE OFFLINE")
            self.save_btn.set_sensitive(True)
        elif st == "done":
            self.save_btn.set_label("SAVED ✓")
            self.save_btn.set_sensitive(False)
        elif st == "error":
            self.save_btn.set_label("SAVE FAILED, RETRY")
            self.save_btn.set_sensitive(True)
        else:
            self.save_btn.set_label(f"SAVING {st}")
            self.save_btn.set_sensitive(False)

    def set_followed(self, on):
        self.followed = on
        self.follow.set_label("FOLLOWING" if on else "FOLLOW")
        (self.follow.add_css_class if on else self.follow.remove_css_class)("on")

    def toggle_follow(self):
        on = not self.followed
        self.set_followed(on)
        src, cid, name = self.info["src"], self.item["channel_id"], self.item.get("channel") or ""
        bg(lambda: api("/sub", src=src, id=cid, name=name, on="1" if on else "0"),
           lambda d: (self.win.subs_changed(), self.win.toast("following " + name if on else "unfollowed " + name)),
           lambda e: (self.set_followed(not on), self.win.toast(str(e))))

    def open_channel(self):
        if self.info and self.info.get("src") in ("youtube", "tiktok") and self.item.get("channel_id"):
            self.win.open_channel(self.info["src"], self.item["channel_id"], self.item.get("channel"))

    def toggle_full(self):
        self.full = not self.full
        self.tv.set_reveal_top_bars(not self.full)
        self.info_sc.set_visible(not self.full)
        self.frame.max_h = 10000
        if self.full:
            self.frame.set_vexpand(True)
            self.frame.ratio = 10       # fill the screen; the picture keeps its own shape inside
            self.win.fullscreen()
        else:
            self.frame.set_vexpand(False)
            self.frame.ratio = 16 / 9 if (self.item.get("src") not in VERTICAL) else 4 / 3
            self.win.unfullscreen()
        self.frame.queue_resize()
        self.player.full_btn.set_icon_name("view-restore-symbolic" if self.full else "view-fullscreen-symbolic")

    def close(self):
        if self.full:
            self.win.unfullscreen()
        POS.set(self.url, self.player.position(), self.player.duration)
        self.player.stop()
        self.win.downloads.unwatch(self.url, self._show_saved_saving)


class SavedPage(Adw.NavigationPage):
    def __init__(self, win):
        super().__init__(title="Saved", tag="saved")
        self.win = win
        tv = Adw.ToolbarView()
        tv.add_top_bar(header(app_title("SAVED")))
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.box.add_css_class("list")
        tv.set_content(Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=self.box))
        self.set_child(tv)
        self.connect("showing", lambda p: self.fill())

    def fill(self):
        while (c := self.box.get_first_child()) is not None:
            self.box.remove(c)
        entries = self.win.downloads.saved()
        if not entries:
            self.box.append(label("nothing saved yet: tap SAVE OFFLINE under a video", "status", xalign=0.5,
                                  wrap=True))
        for e in entries:
            item = {"url": e["url"], "title": e["title"], "channel": e.get("channel"), "src": e.get("src"),
                    "thumb": e.get("thumb_file") or e.get("thumb"), "duration": e.get("duration")}
            card = VideoCard(item, lambda it, e=e: self.win.open_video(it, local=e))
            if e.get("audio"):
                card.meta.set_text("♪ audio only  ·  " + (e.get("channel") or ""))
            delete = flat_button("user-trash-symbolic", tip="Delete")
            delete.set_halign(Gtk.Align.END)
            delete.connect("clicked", lambda b, e=e: (self.win.downloads.delete(e), self.fill()))
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            row.append(card)
            row.append(delete)
            self.box.append(row)


class Downloads:
    """Offline copies: the PC downloads (yt-dlp, faststart MP4 / M4A), then we fetch the file."""

    def __init__(self, win):
        self.win = win
        os.makedirs(SAVED_DIR, exist_ok=True)
        self.jobs = {}        # (url, audio) -> "12%" / "done" / "error"
        self.watchers = collections.defaultdict(list)

    def saved(self):
        return [e for e in load_json(SAVED_INDEX, []) if os.path.exists(e["file"])]

    def state(self, url, audio):
        if any(e["url"] == url and bool(e.get("audio")) == audio for e in self.saved()):
            return "done"
        return self.jobs.get((url, audio))

    def watch(self, url, cb):
        self.watchers[url].append(cb)

    def unwatch(self, url, cb):
        if cb in self.watchers.get(url, []):
            self.watchers[url].remove(cb)

    def _set(self, url, audio, st):
        self.jobs[(url, audio)] = st
        for cb in list(self.watchers.get(url, [])):
            cb()

    def start(self, item, audio):
        url = item["url"]
        if self.jobs.get((url, audio)) not in (None, "error", "done"):
            return
        self._set(url, audio, "0%")
        item = dict(item)

        def run():
            key = None
            while True:
                d = api("/download", u=url, audio="1" if audio else "0")
                key = d["key"]
                if d["state"] == "done":
                    break
                if d["state"] == "error":
                    raise RuntimeError(d.get("error") or "download failed")
                GLib.idle_add(lambda p=d.get("pct", 0): self._set(url, audio, f"{p / 2:.0f}%") or False)
                time.sleep(2)
            safe = re.sub(r"[^\w .,()-]+", "", item.get("title") or key)[:80].strip() or key
            path = os.path.join(SAVED_DIR, safe + (".m4a" if audio else ".mp4"))
            with urllib.request.urlopen(API + "/file?key=" + key, timeout=60) as r, open(path + ".part", "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                last = 0
                while chunk := r.read(262144):
                    f.write(chunk)
                    got += len(chunk)
                    if total and time.monotonic() - last > 1:
                        last = time.monotonic()
                        GLib.idle_add(lambda p=50 + 50 * got / total: self._set(url, audio, f"{p:.0f}%") or False)
            os.replace(path + ".part", path)
            thumb_file = None
            if item.get("thumb"):
                try:
                    req = urllib.request.Request(item["thumb"], headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=20) as r:
                        thumb_file = path.rsplit(".", 1)[0] + ".jpg"
                        with open(thumb_file, "wb") as f:
                            f.write(r.read())
                except Exception:  # noqa: BLE001
                    thumb_file = None
            return {"url": url, "title": item.get("title") or safe, "channel": item.get("channel"),
                    "src": item.get("src"), "duration": item.get("duration"), "thumb_file": thumb_file,
                    "file": path, "audio": audio, "saved": int(time.time())}

        def done(entry):
            idx = [e for e in load_json(SAVED_INDEX, []) if not (e["url"] == url and bool(e.get("audio")) == audio)]
            save_json(SAVED_INDEX, [entry] + idx)
            self._set(url, audio, "done")
            self.win.toast("saved for offline: " + (entry["title"] or "")[:40])

        def fail(e):
            self._set(url, audio, "error")
            self.win.toast(f"couldn't save: {e}")
        bg(run, done, fail)

    def delete(self, entry):
        for p in (entry.get("file"), entry.get("thumb_file")):
            if p and os.path.exists(p):
                os.remove(p)
        save_json(SAVED_INDEX, [e for e in load_json(SAVED_INDEX, []) if e.get("file") != entry.get("file")])


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="notreal.tube", default_width=540, default_height=1100)
        self.downloads = Downloads(self)
        self.subs = set()
        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)
        self.home = HomePage(self)
        self.nav.add(self.home)
        self.search = None
        self.saved_page = None
        self.subs_changed()
        notareal_theme.on_change(self.on_theme)
        self.on_theme()

    def on_theme(self, *_):
        (self.add_css_class if notareal_theme.value("corners", "1") == "0" else self.remove_css_class)("square")

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=2))

    def subs_changed(self):
        def got(d):
            self.subs = {(s["src"], s["id"]) for s in d["subs"]}
        bg(lambda: api("/subs"), got)

    def is_followed(self, src, cid):
        return (src, cid) in self.subs

    def open_video(self, item, local=None):
        self.nav.push(WatchPage(self, item, local))

    def open_channel(self, src, cid, name):
        self.nav.push(ChannelPage(self, src, cid, name))

    def open_search(self):
        if self.search is None:
            self.search = SearchPage(self)
        self.nav.push(self.search)

    def open_saved(self):
        if self.saved_page is None:
            self.saved_page = SavedPage(self)
        self.nav.push(self.saved_page)


class App(Adw.Application):
    def __init__(self):
        flags = Gio.ApplicationFlags.NON_UNIQUE if os.environ.get("NRTUBE_TEST") else Gio.ApplicationFlags.DEFAULT_FLAGS
        super().__init__(application_id=APP_ID, flags=flags)
        self.win = None

    def do_startup(self):
        global LINK
        Adw.Application.do_startup(self)
        notareal_theme.apply(os.path.join(HERE, "style.css"))
        LINK = Link()

    def do_shutdown(self):
        LINK.stop()
        POS._flush()
        Adw.Application.do_shutdown(self)

    def do_activate(self):
        if not self.win:
            self.win = Window(self)
        self.win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
