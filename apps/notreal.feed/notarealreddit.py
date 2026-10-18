#!/usr/bin/env python3
"""notarealReddit: a Reddit reader for the phone, in the vapor/keyboard look.

Data comes from the PC: Redlib (docker, :8095) talks to Reddit, ~/redlib/api.py (:8096)
turns its pages into JSON with Pango-markup bodies, scales images down and remuxes
Reddit's HLS video into plain MP4 (GStreamer's HLS crashes in libsoup on the phone).

Home = your picked subreddits as one feed, chips to jump to one; tap a post for media
and threaded comments (tap a comment's header to fold it). Links to other subreddits
and posts open inside the app.  Left-handed: the frequent buttons sit on the left.
"""
import atexit
import collections
import concurrent.futures
import html
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gst, Gtk, Pango  # noqa: E402
import notareal_theme  # noqa: E402  ~/theme/theme.css

Gst.init(None)

def pc(key, default=""):
    """Address of our PC server (see docs/pc-link.md); set in ~/.config/notreal/pc.json."""
    try:
        with open(os.path.expanduser("~/.config/notreal/pc.json")) as f:
            return json.load(f).get(key) or default
    except (OSError, ValueError):
        return default


APP_ID = "dev.notareal.Reddit"
HERE = os.path.dirname(os.path.abspath(__file__))
CONF_DIR = os.path.join(GLib.get_user_config_dir(), "notarealreddit")
SUBS_FILE = os.path.join(CONF_DIR, "subs.json")
PREFS_FILE = os.path.join(CONF_DIR, "prefs.json")
SEEN_FILE = os.path.join(GLib.get_user_data_dir(), "notarealreddit", "seen.json")
SEEN_DAYS = 4              # a seen post stays hidden from the home feed this long
SEEN_MAX_EMPTY_PAGES = 6   # pages of nothing-new fetched before saying "all caught up"
API = os.environ.get("NOTAREAL_REDDIT_API", "http://%s:8096" % pc("host", "localhost"))
DEFAULT_SUBS = ["linux", "postmarketOS", "pics", "BeAmazed", "costarica"]
FEED_IMG_W = 720           # px asked from the PC for feed images (RAM: textures add up)
FULL_IMG_W = 1440
FEED_MAX_H = 520           # logical px; taller feed images are cropped
TEXTURE_CACHE = 36
SORTS = [("hot", "hot", ""), ("new", "new", ""), ("rising", "rising", ""),
         ("top · day", "top", "day"), ("top · week", "top", "week"),
         ("top · month", "top", "month"), ("top · all", "top", "all")]
SEARCH_SORTS = [("relevance", "relevance", ""), ("new", "new", ""), ("top · week", "top", "week"),
                ("top · month", "top", "month"), ("top · year", "top", "year"), ("top · all", "top", "all"),
                ("most comments", "comments", "")]
COMMENT_SORTS = ["top", "confidence", "new", "controversial", "old"]
DEPTH_COLORS = 6           # .d0 … .d5 in style.css


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


def api(endpoint, **q):
    url = API + endpoint + ("?" + urllib.parse.urlencode(q) if q else "")
    with urllib.request.urlopen(url, timeout=40) as r:
        data = json.loads(r.read().decode())
    if isinstance(data, dict) and data.get("error") and len(data) == 1:
        raise RuntimeError(data["error"])
    return data


def img_url(src, w):
    return API + "/img?" + urllib.parse.urlencode({"src": src, "w": w})


def video_url(src):
    if src.startswith("/hls/"):
        return API + "/video?" + urllib.parse.urlencode({"src": src})
    return API.rsplit(":", 1)[0] + ":8095" + src   # plain mp4 (gifs) straight from Redlib


def load_subs():
    try:
        with open(SUBS_FILE) as f:
            subs = [s for s in json.load(f) if isinstance(s, str)]
        return subs
    except (OSError, ValueError):
        return list(DEFAULT_SUBS)


def save_subs(subs):
    os.makedirs(CONF_DIR, exist_ok=True)
    tmp = SUBS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(subs, f, indent=1)
    os.replace(tmp, SUBS_FILE)


def title_label(text):
    lb = label(text, "sheet-title", xalign=0.5)
    lb.set_ellipsize(Pango.EllipsizeMode.NONE)
    return lb


def label(text="", css=None, xalign=0.0, wrap=False, lines=0, markup=False):
    lb = Gtk.Label(xalign=xalign)
    if markup:
        set_markup(lb, text)
    else:
        lb.set_text(text)
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


def set_markup(lb, m):
    try:
        Pango.parse_markup(m, -1, "\0")
        lb.set_markup(m)
    except GLib.Error:
        lb.set_text(re.sub(r"<[^>]+>", "", m))


def plain(m):
    try:
        return Pango.parse_markup(m, -1, "\0")[2]
    except GLib.Error:
        return re.sub(r"<[^>]+>", "", m)


def short_num(n):
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}m"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def open_external(url):
    try:
        Gio.AppInfo.launch_default_for_uri(url, None)
    except GLib.Error as e:
        print("open:", e.message, flush=True)


def tappable(widget, fn):
    """Tap on widget runs fn and is claimed, so the card/row under it doesn't open too."""
    g = Gtk.GestureClick()
    g.connect("pressed", lambda g, *a: g.set_state(Gtk.EventSequenceState.CLAIMED))
    g.connect("released", lambda g, n, x, y: fn())
    widget.add_controller(g)
    widget.add_css_class("tap")
    return widget


def copy_text(text):
    Gdk.Display.get_default().get_clipboard().set(text)


def copyable(widget, win, text, markup=None, links=()):
    """Long-press -> CopySheet: selectable text + copy buttons for the whole text and every link in it."""
    g = Gtk.GestureLongPress()
    g.connect("pressed", lambda g, x, y: (g.set_state(Gtk.EventSequenceState.CLAIMED),
                                          CopySheet(win, text, markup, links).present(win)))
    widget.add_controller(g)
    return widget


class CopySheet(Adw.Dialog):
    def __init__(self, win, text, markup=None, links=()):
        super().__init__(title="copy", content_height=520)
        self.win = win
        text = text or ""
        urls = list(links)
        for u in re.findall(r"""href=['"]([^'"]+)['"]""", markup or ""):
            u = html.unescape(u)
            if u not in urls:
                urls.append(u)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_title_widget(title_label("COPY"))
        allb = Gtk.Button(label="COPY ALL")
        allb.add_css_class("suggested-action")
        allb.connect("clicked", lambda b: self._copy(text))
        hb.pack_start(allb)  # left-handed: main action on the left
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("subs-sheet")
        view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.add_css_class("copy-text")
        view.get_buffer().set_text(text)
        box.append(view)
        if urls:
            box.append(label("LINKS", "section", xalign=0))
            for u in urls:
                row = Gtk.Box(spacing=8)
                cb = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy link")
                cb.connect("clicked", lambda b, u=u: self._copy(u))
                row.append(cb)
                lb = label(u, "copy-link", wrap=True)
                lb.set_selectable(True)
                lb.set_hexpand(True)
                row.append(lb)
                ob = Gtk.Button(icon_name="web-browser-symbolic", tooltip_text="Open")
                ob.connect("clicked", lambda b, u=u: (self.close(), open_external(u)))
                row.append(ob)
                box.append(row)
        hint = label("drag in the text to select · long-press the selection to copy", "meta", wrap=True)
        box.append(hint)
        tv.set_content(Gtk.ScrolledWindow(child=box, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.set_child(tv)

    def _copy(self, t):
        copy_text(t)
        self.win.toast("copied")
        self.close()


def uname(a):
    a = (a or "").strip()
    return a[2:] if a.startswith("u/") else a


def real_user(a):
    return bool(a) and a not in ("[deleted]", "[removed]", "u/[deleted]")


# --- images -----------------------------------------------------------------------

class Images:
    """Fetch + decode textures off the main loop, keep a small LRU of them."""

    def __init__(self):
        self.cache = collections.OrderedDict()
        self.waiting = {}
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)

    def get(self, url, cb):
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
            with urllib.request.urlopen(url, timeout=40) as r:
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(r.read()))
        except Exception as e:  # noqa: BLE001
            print("image:", url[-60:], repr(e), flush=True)
        GLib.idle_add(self._deliver, url, tex)

    def _deliver(self, url, tex):
        cbs = self.waiting.pop(url, [])
        if tex is not None:
            self.cache[url] = tex
            while len(self.cache) > TEXTURE_CACHE:
                self.cache.popitem(last=False)
        for cb in cbs:
            if tex is not None:
                cb(tex)
        return False


IMAGES = Images()


class Pic(Gtk.Widget):
    """A texture that fills the width, height from its aspect ratio (capped -> cover crop)."""

    def __init__(self, ratio=None, max_h=None, css=None, size=None):
        super().__init__(hexpand=not size)
        self.ratio = ratio or 0.6          # height / width until the texture says otherwise
        self.max_h = max_h
        self.size = size                   # fixed square (thumbnails)
        self.tex = None
        self.want = None
        self.set_overflow(Gtk.Overflow.HIDDEN)
        for c in (css or "pic").split():
            self.add_css_class(c)

    def load(self, url):
        self.want = url
        if url:
            IMAGES.get(url, lambda tex, u=url: self._got(u, tex))

    def _got(self, url, tex):
        if url != self.want:
            return
        self.tex = tex
        r = tex.get_height() / max(1, tex.get_width())
        if abs(r - self.ratio) > 0.01:
            self.ratio = r
            self.queue_resize()
        self.queue_draw()

    def clear(self):
        self.want = None
        self.tex = None
        self.queue_draw()

    def do_get_request_mode(self):
        if self.size:
            return Gtk.SizeRequestMode.CONSTANT_SIZE
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation, for_size):
        if self.size:
            return self.size, self.size, -1, -1
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 0, 0, -1, -1
        w = for_size if for_size > 0 else 360
        h = int(w * self.ratio)
        if self.max_h:
            h = min(h, self.max_h)
        return h, h, -1, -1

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        if not self.tex or w <= 0 or h <= 0:
            return
        tw, th = self.tex.get_width(), self.tex.get_height()
        s = max(w / tw, h / th)
        dw, dh = tw * s, th * s
        snap.append_texture(self.tex, Graphene.Rect().init((w - dw) / 2, (h - dh) / 2, dw, dh))


class ZoomPic(Gtk.Widget):
    """Fullscreen image: fit, pinch to zoom, drag to pan, double-tap toggles 2.5x."""

    def __init__(self, on_zoom=None):
        super().__init__(hexpand=True, vexpand=True)
        self.tex = None
        self.scale = 1.0
        self.ox = self.oy = 0.0
        self.on_zoom = on_zoom
        self.set_overflow(Gtk.Overflow.HIDDEN)
        z = Gtk.GestureZoom()
        z.connect("begin", lambda g, s: setattr(self, "_z0", (self.scale, self.ox, self.oy)))
        z.connect("scale-changed", self._zoomed)
        self.add_controller(z)
        d = Gtk.GestureDrag()
        d.connect("drag-begin", lambda g, x, y: setattr(self, "_d0", (self.ox, self.oy)))
        d.connect("drag-update", self._dragged)
        self.add_controller(d)
        c = Gtk.GestureClick()
        c.connect("pressed", self._clicked)
        self.add_controller(c)

    def load(self, url):
        IMAGES.get(url, self._got)

    def _got(self, tex):
        self.tex = tex
        self.queue_draw()

    def _set(self, scale, ox, oy):
        self.scale = max(1.0, min(scale, 8.0))
        if self.scale == 1.0:
            ox = oy = 0.0
        lim_x = self.get_width() * (self.scale - 1) / 2
        lim_y = self.get_height() * (self.scale - 1) / 2
        self.ox = max(-lim_x, min(ox, lim_x))
        self.oy = max(-lim_y, min(oy, lim_y))
        if self.on_zoom:
            self.on_zoom(self.scale > 1.0)
        self.queue_draw()

    def _zoomed(self, g, s):
        s0, ox, oy = self._z0
        self._set(s0 * s, ox * s, oy * s)

    def _dragged(self, g, dx, dy):
        if self.scale > 1.0:
            ox, oy = self._d0
            self._set(self.scale, ox + dx, oy + dy)

    def _clicked(self, g, n, x, y):
        if n == 2:
            if self.scale > 1.0:
                self._set(1.0, 0, 0)
            else:
                cx, cy = x - self.get_width() / 2, y - self.get_height() / 2
                self._set(2.5, -cx * 1.5, -cy * 1.5)

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        if not self.tex or w <= 0 or h <= 0:
            return
        tw, th = self.tex.get_width(), self.tex.get_height()
        s = min(w / tw, h / th) * self.scale
        dw, dh = tw * s, th * s
        snap.append_texture(self.tex, Graphene.Rect().init((w - dw) / 2 + self.ox, (h - dh) / 2 + self.oy, dw, dh))


class Ratio(Gtk.Widget):
    """Holds one child (a Gtk.Video) at a fixed aspect ratio, height capped."""

    def __init__(self, child, ratio, max_h):
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


class Music:
    """Other audio on the phone: MPRIS players (is music playing?) and sound cards opened directly
    by another app (notarealPlayer HI-FI owns the DAC and suspends PulseAudio's sink for it)."""

    SPIN = "org.mpris.MediaPlayer2.notarealPlayer"

    @staticmethod
    def _bus():
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)

    @classmethod
    def players(cls):
        """{bus name: PlaybackStatus}"""
        out = {}
        try:
            bus = cls._bus()
            names = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                  "ListNames", None, None, 0, 500, None).unpack()[0]
            for n in names:
                if not n.startswith("org.mpris.MediaPlayer2."):
                    continue
                try:
                    v = bus.call_sync(n, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties", "Get",
                                      GLib.Variant("(ss)", ("org.mpris.MediaPlayer2.Player", "PlaybackStatus")),
                                      None, 0, 300, None).unpack()[0]
                    out[n] = v
                except GLib.Error:
                    pass
        except GLib.Error:
            pass
        return out

    @classmethod
    def playing(cls, players=None):
        return any(v == "Playing" for v in (players if players is not None else cls.players()).values())

    @staticmethod
    def card_held():
        """A playback PCM opened by something other than the sound server."""
        import glob
        for f in glob.glob("/proc/asound/card*/pcm*p/sub*/status"):
            try:
                m = re.search(r"owner_pid\s*:\s*(\d+)", open(f).read())
                if m and open(f"/proc/{m.group(1)}/comm").read().strip() not in ("pulseaudio", "pipewire", "wireplumber"):
                    return True
            except OSError:
                pass
        return False

    @classmethod
    def call(cls, name, method):
        try:
            cls._bus().call(name, "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", method,
                            None, None, 0, 1000, None, None, None)
        except GLib.Error:
            pass

    @classmethod
    def release(cls, players):
        """Make room for video sound: stop notarealPlayer (Stop hands the DAC back at once, Pause holds it
        20 s), pause anything else that plays."""
        for n, v in players.items():
            if n == cls.SPIN and v in ("Playing", "Paused"):
                cls.call(n, "Stop")
            elif v == "Playing":
                cls.call(n, "Pause")



class VideoPlayer(Gtk.Overlay):
    """playbin -> gtk4paintablesink. Gtk.MediaFile reads http:// through gvfs (slow, bad seeking:
    5 fps or a black frame), so stream with GStreamer itself. Controls left-first for the left thumb."""

    def __init__(self, uri, gif=False):
        super().__init__(hexpand=True, vexpand=True)
        self.add_css_class("video")
        self.gif = gif
        self.want_play = True
        self.buffering = False
        self.duration = 0
        self.seeking = False
        self.hide_id = 0
        self.tick_id = 0
        self.pb = Gst.ElementFactory.make("playbin")
        sink = Gst.ElementFactory.make("gtk4paintablesink")
        self.pb.set_property("video-sink", sink)
        self.pb.set_property("uri", uri)
        self.pb.set_property("mute", gif)
        # music playing -> the video plays silently (fakesink: also never waits for a DAC that HI-FI owns);
        # music paused/none -> sound, checked again every 1.5 s while the video lives
        self.silent = False
        self.pending_seek = None
        self.checks = 0
        self.released_at = 0
        if gif:
            self._set_sink(True)
        else:
            players = Music.players()
            if Music.playing(players):
                self._set_sink(True)
            elif Music.card_held():
                Music.release(players)    # paused HI-FI still holds the DAC: hand it back now
                self.released_at = time.monotonic()
                self._set_sink(True)
        pic = Gtk.Picture(paintable=sink.props.paintable, content_fit=Gtk.ContentFit.CONTAIN,
                          hexpand=True, vexpand=True)
        self.set_child(pic)
        self.spinner = Adw.Spinner(width_request=40, height_request=40, halign=Gtk.Align.CENTER,
                                   valign=Gtk.Align.CENTER, can_target=False)
        self.add_overlay(self.spinner)
        self.bar = Gtk.Box(spacing=4, valign=Gtk.Align.END)
        self.bar.add_css_class("video-bar")
        self.play_btn = Gtk.Button(icon_name="media-playback-pause-symbolic")
        self.play_btn.add_css_class("flat")
        self.play_btn.connect("clicked", lambda b: self.toggle())
        self.time = label("0:00", "video-time")
        self.time.set_ellipsize(Pango.EllipsizeMode.NONE)
        self.scale = Gtk.Scale(hexpand=True, draw_value=False)
        self.scale.set_range(0, 1)
        self.scale.connect("change-value", self._seek)
        self.mute_btn = Gtk.Button(icon_name="audio-volume-high-symbolic")
        self.mute_btn.add_css_class("flat")
        self.mute_btn.connect("clicked", lambda b: self._mute())
        for w in (self.play_btn, self.time, self.scale, self.mute_btn):
            self.bar.append(w)
        if not gif:
            self.add_overlay(self.bar)
        g = Gtk.GestureClick()
        g.connect("released", lambda *a: self.toggle() if gif else self._show_bar())
        pic.add_controller(g)
        bus = self.pb.get_bus()
        bus.add_signal_watch()
        self.bus_id = bus.connect("message", self._msg)
        self.connect("unrealize", lambda w: self.stop())
        self.pb.set_state(Gst.State.PLAYING)
        self.tick_id = GLib.timeout_add(250, self._tick)
        self._icons()
        self._show_bar()

    def _set_sink(self, silent):
        self.silent = silent
        if silent:
            sink = Gst.ElementFactory.make("fakesink")
            sink.set_property("sync", True)
        else:
            sink = None   # playbin default (autoaudiosink)
        self.pb.set_property("audio-sink", sink)

    def _swap_sink(self, silent):
        """Change the audio sink mid-video: playbin only takes a new sink from NULL, so restart and seek back."""
        ok, pos = self.pb.query_position(Gst.Format.TIME)
        if self.pending_seek is not None:   # the last swap never got going (sink stalled): keep its spot
            ok, pos = True, self.pending_seek
        self.pb.set_state(Gst.State.NULL)
        self._set_sink(silent)
        self.pending_seek = pos if ok and pos > 0 else None
        self.pb.set_state(Gst.State.PLAYING if self.want_play else Gst.State.PAUSED)
        self._icons()

    def _audio_check(self):
        if self.gif or not self.pb:
            return
        players = Music.players()
        if Music.playing(players):
            if not self.silent:
                self._swap_sink(True)
        elif self.silent:
            if not Music.card_held():
                self._swap_sink(False)
            elif time.monotonic() - self.released_at > 5:
                Music.release(players)
                self.released_at = time.monotonic()

    def _msg(self, bus, m):
        t = m.type
        if t == Gst.MessageType.BUFFERING:
            pct = m.parse_buffering()
            self.buffering = pct < 100
            self.spinner.set_visible(self.buffering)
            if self.want_play:
                self.pb.set_state(Gst.State.PAUSED if self.buffering else Gst.State.PLAYING)
        elif t == Gst.MessageType.EOS:
            if self.gif:
                self.pb.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
            else:
                self.want_play = False
                self.pb.set_state(Gst.State.PAUSED)
                self.pb.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
                self._icons()
                self._show_bar(stay=True)
        elif t == Gst.MessageType.ERROR:
            err, dbg = m.parse_error()
            print("video:", err.message, flush=True)
            self.spinner.set_visible(False)
            self.set_child(label("couldn't play this video", "empty", xalign=0.5))
        elif t == Gst.MessageType.ASYNC_DONE and self.pending_seek is not None:
            pos, self.pending_seek = self.pending_seek, None
            self.pb.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, pos)
        elif t == Gst.MessageType.STATE_CHANGED and m.src == self.pb:
            if m.parse_state_changed()[1] in (Gst.State.PAUSED, Gst.State.PLAYING) and not self.buffering:
                self.spinner.set_visible(False)

    def _tick(self):
        self.checks += 1
        if self.checks % 6 == 0:
            self._audio_check()
        if not self.duration:
            ok, d = self.pb.query_duration(Gst.Format.TIME)
            if ok and d > 0:
                self.duration = d
                self.scale.set_range(0, d / Gst.SECOND)
        ok, pos = self.pb.query_position(Gst.Format.TIME)
        if ok and not self.seeking:
            self.scale.set_value(pos / Gst.SECOND)
            self.time.set_text(fmt_time(pos / Gst.SECOND))
        return True

    def _seek(self, scale, scroll, value):
        self.pb.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                            int(max(0, value) * Gst.SECOND))
        self.time.set_text(fmt_time(value))
        self._show_bar()
        return False

    def _mute(self):
        if self.silent:   # silent because of music: tapping 🔊 means "I want this sound" -> stop the music
            Music.release(Music.players())
            self.released_at = time.monotonic()
            self.pb.set_property("mute", False)
            GLib.timeout_add(400, lambda: self._audio_check() or False)
            self._show_bar()
            return
        m = not self.pb.get_property("mute")
        self.pb.set_property("mute", m)
        self._icons()
        self._show_bar()

    def _icons(self):
        self.play_btn.set_icon_name("media-playback-pause-symbolic" if self.want_play
                                    else "media-playback-start-symbolic")
        self.mute_btn.set_icon_name("audio-volume-muted-symbolic" if self.silent or self.pb.get_property("mute")
                                    else "audio-volume-high-symbolic")

    def _show_bar(self, stay=False):
        self.bar.set_visible(True)
        if self.hide_id:
            GLib.source_remove(self.hide_id)
            self.hide_id = 0
        if not stay and self.want_play:
            def hide():
                self.hide_id = 0
                if self.want_play:
                    self.bar.set_visible(False)
                return False
            self.hide_id = GLib.timeout_add(3000, hide)

    def toggle(self):
        if self.want_play:
            self.pause()
        else:
            self.play()

    def play(self):
        self.want_play = True
        if not self.buffering:
            self.pb.set_state(Gst.State.PLAYING)
        self._icons()
        self._show_bar()

    def pause(self):
        self.want_play = False
        self.pb.set_state(Gst.State.PAUSED)
        self._icons()
        self._show_bar(stay=True)

    def stop(self):
        for attr in ("tick_id", "hide_id"):
            if getattr(self, attr):
                GLib.source_remove(getattr(self, attr))
                setattr(self, attr, 0)
        if self.pb:
            self.pb.set_state(Gst.State.NULL)
            bus = self.pb.get_bus()
            bus.disconnect(self.bus_id)
            bus.remove_signal_watch()
            self.pb = None


def fmt_time(s):
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


def overlay_badge(widget, text):
    ov = Gtk.Overlay(child=widget)
    b = label(text, "badge")
    b.set_halign(Gtk.Align.START)   # left: where the left thumb rests
    b.set_valign(Gtk.Align.END)
    b.set_margin_start(10)
    b.set_margin_bottom(10)
    ov.add_overlay(b)
    return ov, b


# --- feed ---------------------------------------------------------------------------

class PostItem(GObject.Object):
    def __init__(self, data):
        super().__init__()
        self.data = data


class Galleries:
    """Feed listings only carry a gallery's 140px thumbnail; fetch the post once for its images."""

    def __init__(self):
        self.cache = {}
        self.waiting = {}
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    def get(self, permalink, cb):
        if permalink in self.cache:
            cb(self.cache[permalink])
            return
        if permalink in self.waiting:
            self.waiting[permalink].append(cb)
            return
        self.waiting[permalink] = [cb]
        self.pool.submit(self._load, permalink)

    def _load(self, permalink):
        imgs = []
        try:
            m = (api("/post", path=permalink).get("post") or {}).get("media") or {}
            imgs = m.get("images") or []
        except Exception as e:  # noqa: BLE001
            print("gallery:", permalink[-40:], repr(e), flush=True)
        GLib.idle_add(self._deliver, permalink, imgs)

    def _deliver(self, permalink, imgs):
        if imgs:
            self.cache[permalink] = imgs
        for cb in self.waiting.pop(permalink, []):
            cb(imgs)
        return False


GALLERIES = Galleries()


class PostCard(Gtk.Box):
    """One post in a feed; rebound as the ListView recycles rows."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.win = win
        self.add_css_class("post-card")
        self.p = None
        self.metabox = Gtk.Box(spacing=0)
        self.tags = label(css="meta")
        self.sub = tappable(label(css="meta sub-link"), lambda: self.win.open_sub(self.p["sub"][2:]))
        self.sub.set_ellipsize(Pango.EllipsizeMode.NONE)
        self.age = label(css="meta")
        self.age.set_ellipsize(Pango.EllipsizeMode.NONE)
        self.author = tappable(label(css="meta user-link"), lambda: self.win.open_user(uname(self.p["author"])))
        for w in (self.tags, self.sub, self.age, self.author):
            self.metabox.append(w)
        self.meta = self.metabox
        self.flair = label(css="flair")
        self.flair.set_halign(Gtk.Align.START)
        self.title = label(css="title", wrap=True)
        self.pic = Pic(max_h=FEED_MAX_H)
        self.media, self.badge = overlay_badge(self.pic, "")
        self.link = Gtk.Box(spacing=10)
        self.link.add_css_class("link-row")
        self.thumb = Pic(ratio=1.0, css="thumb", size=64)
        self.domain = label(css="domain")
        self.domain.set_hexpand(True)
        self.link.append(self.thumb)
        self.link.append(self.domain)
        self.preview = label(css="preview md", wrap=True, lines=4)
        self.foot = label(css="foot")
        for w in (self.meta, self.flair, self.title, self.media, self.link, self.preview, self.foot):
            self.append(w)

    def bind(self, p):
        self.p = p
        tags = []
        if p.get("nsfw"):
            tags.append("<span foreground='#f7a1e8'>NSFW</span>")
        if p.get("stickied"):
            tags.append("<span foreground='#7ef0d2'>📌</span>")
        self.tags.set_markup("  ·  ".join(tags) + "  ·  " if tags else "")
        self.tags.set_visible(bool(tags))
        self.sub.set_text(p.get("sub") or "")
        self.sub.set_visible((p.get("sub") or "").startswith("r/"))
        self.age.set_text("  ·  " + (p.get("age") or "") + "  ·  ")
        self.author.set_text(p.get("author") or "")
        self.author.set_sensitive(real_user(p.get("author")))
        fl = p.get("flair")
        self.flair.set_visible(bool(fl and fl.get("text")))
        if fl:
            self.flair.set_text(fl.get("text") or "")
        self.title.set_text(p.get("title") or "")
        m = p.get("media") or {}
        t = m.get("type")
        src = None
        badge = ""
        if t == "image":
            src = m.get("src")
        elif t == "gallery" and m.get("images"):
            src = m["images"][0]["src"]
            badge = f"▦ {len(m['images'])}"
        elif t == "gallery" and p.get("permalink") and not p.get("nsfw"):
            # listing gave only a tiny thumbnail: show an empty frame now, the real image when fetched
            self.pic.ratio = 0.75
            self.pic.clear()
            self.pic.queue_resize()
            self.media.set_visible(True)
            self.badge.set_text("▦")
            self.badge.set_visible(True)
            self.link.set_visible(False)
            pid = p["id"]
            GALLERIES.get(p["permalink"], lambda imgs: self._gallery(pid, imgs))
            self._rest(p)
            return
        elif t in ("video", "gif"):
            src = m.get("poster")
            badge = "▶ video" if t == "video" else "▶ gif"
        if p.get("nsfw"):
            src, badge = None, ""
        if src:
            if m.get("w") and m.get("h"):
                self.pic.ratio = m["h"] / m["w"]
            else:
                self.pic.ratio = 0.6
            self.pic.clear()
            self.pic.queue_resize()
            self.pic.load(img_url(src, FEED_IMG_W))
        else:
            self.pic.clear()
        self.media.set_visible(bool(src))
        self.badge.set_text(badge)
        self.badge.set_visible(bool(badge))
        is_link = not src and bool(p.get("url") or (p.get("thumb") and not p.get("nsfw")))
        self.link.set_visible(is_link)
        if is_link:
            if p.get("url"):
                self.domain.set_text("↗ " + (p.get("domain") or urllib.parse.urlparse(p["url"]).netloc))
            else:
                self.domain.set_text(("▦ " if t == "gallery" else "") + (p.get("domain") or ""))
            if p.get("thumb"):
                self.thumb.set_visible(True)
                self.thumb.load(img_url(p["thumb"], 200))
            else:
                self.thumb.clear()
                self.thumb.set_visible(False)
        self._rest(p)

    def _rest(self, p):
        text = plain(p.get("body") or "").strip()
        self.preview.set_visible(bool(text))
        self.preview.set_text(text[:400])
        self.foot.set_text(f"▲ {p.get('score_text') or short_num(p.get('score'))}    "
                           f"◆ {short_num(p.get('comments'))} comments")

    def _gallery(self, pid, imgs):
        if not self.p or self.p["id"] != pid:
            return   # row was recycled for another post meanwhile
        if not imgs:   # fetch failed: fall back to the thumbnail row
            self.media.set_visible(False)
            if self.p.get("thumb"):
                self.link.set_visible(True)
                self.domain.set_text("▦ gallery")
                self.thumb.set_visible(True)
                self.thumb.load(img_url(self.p["thumb"], 200))
            return
        self.badge.set_text(f"▦ {len(imgs)}")
        self.pic.load(img_url(imgs[0]["src"], FEED_IMG_W))

    def unbind(self):
        self.p = None
        self.pic.clear()
        self.thumb.clear()


class CommentCard(Gtk.Box):
    """A comment on someone's profile: which post, where, and the comment itself."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.win = win
        self.c = None
        self.add_css_class("post-card")
        self.add_css_class("user-cmt")
        top = Gtk.Box()
        self.sub = tappable(label(css="meta sub-link"), lambda: self.win.open_sub(self.c["sub"][2:]))
        self.sub.set_ellipsize(Pango.EllipsizeMode.NONE)
        self.meta = label(css="meta")
        top.append(self.sub)
        top.append(self.meta)
        self.title = label(css="cmt-on", wrap=True, lines=2)
        self.body = label(css="cmt-body md", wrap=True, lines=8)
        self.foot = label(css="foot")
        for w in (top, self.title, self.body, self.foot):
            self.append(w)

    def bind(self, c):
        self.c = c
        self.sub.set_text(c.get("sub") or "")
        self.sub.set_visible((c.get("sub") or "").startswith("r/"))
        self.meta.set_text("  ·  " + (c.get("age") or ""))
        self.title.set_text("↳ " + (c.get("title") or ""))
        self.body.set_text(plain(c.get("body") or "").strip())
        sc = c.get("score_text") or ""
        self.foot.set_text(f"▲ {sc}" if sc and sc != "•" else "")
        self.foot.set_visible(bool(sc and sc != "•"))

    def unbind(self):
        self.c = None


class Seen:
    """Posts that were actually on screen in the home feed (id -> time), kept SEEN_DAYS.
    `before` is the snapshot hidden by the current listing, so what you see while scrolling stays."""

    def __init__(self):
        try:
            with open(SEEN_FILE) as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        cut = time.time() - SEEN_DAYS * 86400
        self.d = {k: v for k, v in d.items() if v > cut}
        self.before = set(self.d)
        self.save_id = 0

    def snapshot(self):
        self.before = set(self.d)

    def mark(self, pid):
        if pid and pid not in self.d:
            self.d[pid] = time.time()
            if not self.save_id:
                self.save_id = GLib.timeout_add_seconds(5, self.save)

    def save(self):
        self.save_id = 0
        os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
        tmp = SEEN_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.d, f)
        os.replace(tmp, SEEN_FILE)
        return False


SEEN = Seen()
atexit.register(lambda: SEEN.save() if SEEN.save_id else None)


def load_prefs():
    try:
        with open(PREFS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_prefs(p):
    os.makedirs(CONF_DIR, exist_ok=True)
    with open(PREFS_FILE, "w") as f:
        json.dump(p, f)


class Feed(Gtk.Box):
    """Infinite list of posts (or a profile's posts + comments) with sort and an optional header card.
    source(sort, t, after) -> (endpoint, params)."""

    def __init__(self, win, source, header=None, on_about=None, sort="hot", on_page=None, hide_seen=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.win = win
        self.hide_seen = hide_seen   # callable -> bool: the home feed tracks + hides seen posts
        self.empty_pages = 0
        self.hidden = 0
        self.after_toast = False
        self.mark_id = 0
        self.on_page = on_page
        self.source = source
        self.header = header
        self.on_about = on_about
        self.sort, self.t = sort, ""
        self.after = None
        self.loading = False
        self.done = False
        self.gen = 0
        self.seen = set()
        self.store = Gio.ListStore(item_type=PostItem)
        fac = Gtk.SignalListItemFactory()
        fac.connect("bind", self._bind)
        fac.connect("unbind", self._unbind)
        self.view = Gtk.ListView(model=Gtk.NoSelection(model=self.store), factory=fac,
                                 single_click_activate=True)
        self.view.add_css_class("feed")
        self.view.connect("activate", self._activate)
        self.scroller = Gtk.ScrolledWindow(child=self.view, vexpand=True,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroller.get_vadjustment().connect("value-changed", self._scrolled)
        self.status = label(css="empty", xalign=0.5, wrap=True)
        self.status.set_margin_top(40)
        self.spinner = Adw.Spinner(height_request=36)
        self.spinner.set_margin_top(10)
        self.spinner.set_margin_bottom(10)
        self.append(self.scroller)
        self.append(self.spinner)
        self.overlay_status()

    def _bind(self, f, li):
        d = li.get_item().data
        if "_header" in d:
            li.set_activatable(False)
            li.set_child(d["_header"])
            return
        li.set_activatable(True)
        want = CommentCard if d.get("kind") == "comment" else PostCard
        child = li.get_child()
        if type(child) is not want:
            child = want(self.win)
            li.set_child(child)
        child.bind(d)

    def _unbind(self, f, li):
        child = li.get_child()
        if isinstance(child, (PostCard, CommentCard)):
            child.unbind()
        else:
            li.set_child(None)   # the header card: let go so the next bind can take it

    def n_posts(self):
        return self.store.get_n_items() - (1 if self.header else 0)

    def overlay_status(self):
        self.remove(self.scroller)
        ov = Gtk.Overlay(child=self.scroller, vexpand=True)
        self.status.set_valign(Gtk.Align.END if self.header else Gtk.Align.START)
        self.status.set_margin_bottom(60)
        ov.add_overlay(self.status)
        self.status.set_can_target(False)
        self.prepend(ov)

    def reload(self, sort=None, t=None):
        if sort is not None:
            self.sort, self.t = sort, t
        self.gen += 1
        if self.header and self.store.get_n_items():
            # keep the header row: re-adding it rebinds the widget while the old row still owns it
            self.store.splice(1, self.store.get_n_items() - 1, [])
        else:
            self.store.remove_all()
            if self.header:
                self.store.append(PostItem({"_header": self.header}))
        self.seen.clear()
        if self.hide_seen:
            SEEN.snapshot()          # everything seen until now stays out of this listing
        self.empty_pages = 0
        self.hidden = 0
        self.after_toast = False
        self.after = None
        self.done = False
        self.loading = False
        self.scroller.get_vadjustment().set_value(0)
        self.load_more()

    def load_more(self):
        if self.loading or self.done:
            return
        src = self.source(self.sort, self.t, self.after)
        if not src:
            self.status.set_text("no subreddits yet\ntap ✚ to add some")
            self.status.set_visible(True)
            self.spinner.set_visible(False)
            return
        endpoint, q = src
        self.loading = True
        self.spinner.set_visible(True)
        self.status.set_visible(False)
        gen = self.gen
        bg(lambda: api(endpoint, **q), lambda d: self._got(gen, d), lambda e: self._fail(gen, e))

    def _got(self, gen, d):
        if gen != self.gen:
            return
        self.loading = False
        self.spinner.set_visible(False)
        if d.get("about") and self.on_about:
            self.on_about(d["about"])
        if self.on_page:
            self.on_page(d)
        fresh = [p for p in d.get("posts", []) if p["id"] not in self.seen]
        for p in fresh:
            self.seen.add(p["id"])
        hiding = bool(self.hide_seen and self.hide_seen())
        if hiding:
            kept = [p for p in fresh if p["id"] not in SEEN.before]
            self.hidden += len(fresh) - len(kept)
            fresh = kept
        self.store.splice(self.store.get_n_items(), 0, [PostItem(p) for p in fresh])
        self.after = d.get("after")
        self.done = not self.after
        if hiding and not fresh and not self.done and self.empty_pages < SEEN_MAX_EMPTY_PAGES:
            self.empty_pages += 1           # a page of only seen posts: look further
            return self.load_more()
        self.empty_pages = 0
        if not self.n_posts():
            if hiding and self.hidden:
                self.status.set_text("You're all caught up ✓\n%d posts you've already seen are hidden" % self.hidden)
            else:
                self.status.set_text(d.get("error") or "nothing here")
            self.status.set_visible(True)
        else:
            GLib.idle_add(lambda: (self._scrolled(self.scroller.get_vadjustment()), False)[1])
            if hiding and self.hidden and not self.after_toast:
                self.after_toast = True
                self.win.toast("hid %d posts you've already seen" % self.hidden)

    def _fail(self, gen, e):
        if gen != self.gen:
            return
        self.loading = False
        self.spinner.set_visible(False)
        if not self.n_posts():
            self.status.set_text(f"couldn't load\n{e}")
            self.status.set_visible(True)
        else:
            self.win.toast("couldn't load more — scroll to retry")

    def _scrolled(self, adj):
        if adj.get_value() + adj.get_page_size() * 3 >= adj.get_upper():
            self.load_more()
        if self.hide_seen and not self.mark_id:
            self.mark_id = GLib.timeout_add(100, self._mark_visible)   # often: flicks fly past fast

    def mark_all_seen(self):
        """Everything loaded in this listing counts as seen (clears an old backlog in one tap)."""
        n = 0
        for i in range(self.store.get_n_items()):
            d = self.store.get_item(i).data
            if "_header" not in d and d.get("id") and d["id"] not in SEEN.d:
                SEEN.mark(d["id"])
                n += 1
        SEEN.save()
        return n

    def _mark_visible(self):
        """Mark the posts actually on screen as seen (sample points down the middle of the list)."""
        self.mark_id = 0
        w, h = self.scroller.get_width(), self.scroller.get_height()
        if w <= 0 or h <= 0 or not self.get_mapped():
            return False
        for frac in (0.04, 0.16, 0.28, 0.40, 0.52, 0.64, 0.76, 0.88, 0.97):
            node = self.scroller.pick(w / 2, h * frac, Gtk.PickFlags.DEFAULT)
            while node is not None and not isinstance(node, PostCard):
                node = node.get_parent()
            if node is not None and getattr(node, "p", None):
                SEEN.mark(node.p.get("id"))
        return False

    def _activate(self, view, pos):
        item = self.store.get_item(pos)
        if not item or "_header" in item.data:
            return
        d = item.data
        if d.get("kind") == "comment":
            self.win.open_post(d.get("permalink"))
        else:
            self.win.open_post(d["permalink"], d)


def sort_button(on_pick, options):
    """MenuButton with a popover of flat choices; on_pick(index)."""
    mb = Gtk.MenuButton(icon_name="view-sort-descending-symbolic", tooltip_text="Sort")
    pop = Gtk.Popover()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.add_css_class("sort-pop")
    for i, name in enumerate(options):
        b = Gtk.Button(label=name)
        b.add_css_class("flat")
        b.get_child().set_xalign(0)

        def picked(_b, i=i):
            pop.popdown()
            on_pick(i)
        b.connect("clicked", picked)
        box.append(b)
    pop.set_child(box)
    mb.set_popover(pop)
    return mb


class HomePage(Adw.NavigationPage):
    def __init__(self, win):
        super().__init__(title="notreal.feed", tag="home")
        self.win = win
        self.current = None      # None = all picked subs
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.add_css_class("vapor-bar")
        t = label("NOTREAL.FEED", "app-title", xalign=0.5)
        t.set_ellipsize(Pango.EllipsizeMode.NONE)
        hb.set_title_widget(t)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh")
        refresh.connect("clicked", lambda b: self.feed.reload())
        hb.pack_start(refresh)
        find = Gtk.Button(icon_name="system-search-symbolic", tooltip_text="Search & browse subreddits")
        find.connect("clicked", lambda b: self.win.open_search())
        hb.pack_start(find)
        self.sort_btn = sort_button(self._sorted, [s[0] for s in SORTS])
        hb.pack_start(self.sort_btn)
        edit = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Subreddits")
        edit.connect("clicked", lambda b: self.win.edit_subs())
        hb.pack_end(edit)
        tv.add_top_bar(hb)
        self.chips = Gtk.Box(spacing=4)
        self.chips.add_css_class("chips")
        cs = Gtk.ScrolledWindow(child=self.chips, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                hscrollbar_policy=Gtk.PolicyType.EXTERNAL)
        tv.add_top_bar(cs)
        self.prefs = load_prefs()
        on = self.prefs.get("hide_seen", True)
        eye = Gtk.ToggleButton(icon_name="view-conceal-symbolic" if on else "view-reveal-symbolic",
                               tooltip_text="Hide posts you've seen", active=on)
        eye.connect("toggled", self._toggle_seen)
        hb.pack_end(eye)
        done = Gtk.Button(icon_name="object-select-symbolic", tooltip_text="Mark all as seen")
        done.connect("clicked", self._mark_all)
        hb.pack_end(done)
        self.feed = Feed(win, self._source, hide_seen=lambda: self.prefs.get("hide_seen", True))
        tv.set_content(self.feed)
        self.set_child(tv)
        self.build_chips()
        self.feed.reload()

    def _source(self, sort, t, after):
        subs = self.current or "+".join(self.win.subs)
        if not subs:
            return None
        q = dict(subs=subs, sort=sort, t=t)
        if after:
            q["after"] = after
        return "/feed", q

    def _mark_all(self, _btn):
        n = self.feed.mark_all_seen()
        self.win.toast("marked %d posts as seen" % n)
        self.feed.reload()

    def _toggle_seen(self, btn):
        on = btn.get_active()
        btn.set_icon_name("view-conceal-symbolic" if on else "view-reveal-symbolic")
        self.prefs["hide_seen"] = on
        save_prefs(self.prefs)
        self.win.toast("hiding posts you've seen" if on else "showing everything")
        self.feed.reload()

    def _sorted(self, i):
        name, sort, t = SORTS[i]
        self.feed.reload(sort, t)
        self.win.toast(f"sorted by {name}")

    def build_chips(self):
        while (c := self.chips.get_first_child()):
            self.chips.remove(c)
        group = None
        for name in [None] + list(self.win.subs):
            b = Gtk.ToggleButton(label=("all" if name is None else name))
            b.add_css_class("chip")
            if group:
                b.set_group(group)
            else:
                group = b
            b.set_active(name == self.current)
            b.connect("toggled", self._chip, name)
            self.chips.append(b)

    def _chip(self, b, name):
        if b.get_active() and name != self.current:
            self.current = name
            self.feed.reload()

    def subs_changed(self):
        if self.current and self.current not in self.win.subs:
            self.current = None
        self.build_chips()
        self.feed.reload()


class AboutCard(Gtk.Box):
    """Header card at the top of a subreddit or profile feed: icon, names, numbers, description."""

    def __init__(self, title):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add_css_class("about-card")
        top = Gtk.Box(spacing=12)
        self.icon = Pic(ratio=1.0, css="about-icon", size=60)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self.title = label(title, "about-title", wrap=True, lines=2)
        self.name = label("", "about-name")
        self.stats = label("", "about-stats")
        for w in (self.title, self.name, self.stats):
            names.append(w)
        top.append(self.icon)
        top.append(names)
        self.append(top)
        self.actions = Gtk.Box(spacing=8)   # left side: the left thumb
        self.append(self.actions)
        self.desc = label("", "about-desc md", wrap=True, lines=3)
        self.desc.set_visible(False)
        tappable(self.desc, self._expand)
        self.append(self.desc)

    def fill(self, title, name, stats, desc, icon):
        if title:
            self.title.set_text(title)
        self.name.set_text(name or "")
        self.stats.set_text(stats or "")
        self.desc.set_text((desc or "").strip())
        self.desc.set_visible(bool((desc or "").strip()))
        if icon:
            self.icon.load(img_url(icon, 128))

    def _expand(self):
        self.desc.set_lines(0 if self.desc.get_lines() else 3)
        self.desc.set_ellipsize(Pango.EllipsizeMode.NONE if self.desc.get_lines() == 0 else Pango.EllipsizeMode.END)


class SubPage(Adw.NavigationPage):
    """One subreddit: about card with JOIN / JOINED, then its posts."""

    def __init__(self, win, name):
        super().__init__(title="r/" + name)
        self.win, self.name = win, name
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.add_css_class("vapor-bar")
        hb.set_title_widget(title_label("r/" + name))
        self.card = AboutCard("r/" + name)
        self.join = Gtk.Button()
        self.join.connect("clicked", self._toggle)
        self.card.actions.append(self.join)
        self.feed = Feed(win, lambda sort, t, after: ("/feed", dict(subs=self.name, sort=sort, t=t, **({"after": after} if after else {}))),
                         header=self.card, on_about=self._about)
        hb.pack_start(sort_button(lambda i: self.feed.reload(SORTS[i][1], SORTS[i][2]), [s[0] for s in SORTS]))
        find = Gtk.Button(icon_name="system-search-symbolic", tooltip_text="Search this subreddit")
        find.connect("clicked", lambda b: self.win.open_search(self.name))
        hb.pack_start(find)
        tv.add_top_bar(hb)
        tv.set_content(self.feed)
        self.set_child(tv)
        self._label_join()
        self.connect("showing", lambda p: self._label_join())
        self.feed.reload()

    def _about(self, a):
        if (a.get("name") or "").startswith("r/"):
            self.name = a["name"][2:]   # Reddit's own casing for the chip
        stats = "  ·  ".join(x for x in (f"{a['members_text']} members" if a.get("members_text") else "",) if x)
        self.card.fill(a.get("title"), a.get("name"), stats, a.get("desc"), a.get("icon"))

    def _have(self):
        return any(s.lower() == self.name.lower() for s in self.win.subs)

    def _label_join(self):
        have = self._have()
        self.join.set_label("JOINED ✓" if have else "JOIN")
        for c, on in (("joined", have), ("join", not have)):
            (self.join.add_css_class if on else self.join.remove_css_class)(c)

    def _toggle(self, b):
        if self._have():
            self.win.set_subs([s for s in self.win.subs if s.lower() != self.name.lower()])
            self.win.toast(f"left r/{self.name}")
        else:
            self.win.set_subs(self.win.subs + [self.name])
            self.win.toast(f"joined r/{self.name} — it's in your feed")
        self._label_join()


USER_TABS = [("overview", "overview"), ("comments", "comments"), ("posts", "submitted")]
USER_SORTS = [("new", "new", ""), ("hot", "hot", ""), ("top · all", "top", "all"),
              ("top · year", "top", "year"), ("top · month", "top", "month")]


class UserPage(Adw.NavigationPage):
    """Someone's profile: about card, overview / comments / posts tabs."""

    def __init__(self, win, name):
        super().__init__(title="u/" + name)
        self.win, self.name = win, name
        self.tab = "overview"
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.add_css_class("vapor-bar")
        hb.set_title_widget(title_label("u/" + name))
        self.card = AboutCard("u/" + name)
        group = None
        for text, tab in USER_TABS:
            b = Gtk.ToggleButton(label=text)
            b.add_css_class("chip")
            if group:
                b.set_group(group)
            else:
                group = b
            b.set_active(tab == self.tab)
            b.connect("toggled", self._tabbed, tab)
            self.card.actions.append(b)
        self.feed = Feed(win, self._source, header=self.card, on_about=self._about, sort="new")
        hb.pack_start(sort_button(lambda i: self.feed.reload(USER_SORTS[i][1], USER_SORTS[i][2]),
                                  [s[0] for s in USER_SORTS]))
        web = Gtk.Button(icon_name="web-browser-symbolic", tooltip_text="Open on reddit.com")
        web.connect("clicked", lambda b: open_external(f"https://www.reddit.com/user/{self.name}"))
        hb.pack_end(web)
        tv.add_top_bar(hb)
        tv.set_content(self.feed)
        self.set_child(tv)
        self.feed.reload()

    def _source(self, sort, t, after):
        q = dict(name=self.name, tab=self.tab, sort=sort, t=t)
        if after:
            q["after"] = after
        return "/user", q

    def _tabbed(self, b, tab):
        if b.get_active() and tab != self.tab:
            self.tab = tab
            self.feed.reload()

    def _about(self, a):
        bits = []
        if a.get("karma") is not None:
            bits.append(f"{short_num(a['karma'])} karma")
        if a.get("created"):
            bits.append("since " + a["created"])
        self.card.fill(None, a.get("name"), "  ·  ".join(bits), a.get("desc"), a.get("icon"))


# --- post + comments -------------------------------------------------------------------

class PostPage(Adw.NavigationPage):
    def __init__(self, win, permalink, preview=None):
        sub = (preview or {}).get("sub") or (re.match(r"/(r/\w+)", permalink) or [None, "post"])[1]
        super().__init__(title=sub)
        self.win, self.permalink = win, permalink
        self.sort = "top"
        self.shown = set()
        self.media_streams = []
        self.gen = 0
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.add_css_class("vapor-bar")
        tl = title_label(sub)
        if sub.startswith("r/"):
            tappable(tl, lambda: self.win.open_sub(sub[2:]))
        hb.set_title_widget(tl)
        hb.pack_start(sort_button(self._sorted, COMMENT_SORTS))
        web = Gtk.Button(icon_name="web-browser-symbolic", tooltip_text="Open on reddit.com")
        web.connect("clicked", lambda b: open_external("https://www.reddit.com" + self.permalink))
        hb.pack_end(web)
        tv.add_top_bar(hb)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.box.add_css_class("post-page")
        self.head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.comments = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.comments.add_css_class("comments")
        self.spinner = Adw.Spinner(height_request=40)
        self.box.append(self.head)
        self.box.append(self.spinner)
        self.box.append(self.comments)
        sc = Gtk.ScrolledWindow(child=self.box, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        tv.set_content(sc)
        self.set_child(tv)
        self.connect("hidden", lambda p: [m.pause() for m in self.media_streams])
        if preview:
            self._render_head(preview, quick=True)
        self.load()

    def load(self):
        self.gen += 1
        gen = self.gen
        self.spinner.set_visible(True)
        bg(lambda: api("/post", path=self.permalink, sort=self.sort),
           lambda d: self._got(gen, d), lambda e: self._fail(gen, e))

    def _sorted(self, i):
        self.sort = COMMENT_SORTS[i]
        while (c := self.comments.get_first_child()):
            self.comments.remove(c)
        self.shown.clear()
        self.load()

    def _fail(self, gen, e):
        if gen == self.gen:
            self.spinner.set_visible(False)
            self.comments.append(label(f"couldn't load\n{e}", "empty", xalign=0.5, wrap=True))

    def _got(self, gen, d):
        if gen != self.gen:
            return
        self.spinner.set_visible(False)
        if d.get("error"):
            return self._fail(gen, d["error"])
        if not getattr(self, "_full_head", False):
            self._render_head(d["post"], quick=False)
        cs = d.get("comments", [])
        if not cs:
            self.comments.append(label("no comments yet", "empty", xalign=0.5))
            return
        # build in small batches so a 1k-comment thread doesn't freeze the page
        queue = list(cs)

        def step():
            if gen != self.gen:
                return False
            for c in queue[:8]:
                self.comments.append(self.comment_widget(c, 0))
            del queue[:8]
            return bool(queue)
        GLib.idle_add(step)

    def _render_head(self, p, quick):
        while (c := self.head.get_first_child()):
            self.head.remove(c)
        for m in self.media_streams:
            m.stop()
        self.media_streams.clear()
        self._full_head = not quick
        mb = Gtk.Box()
        author = p.get("author") or ""
        if author:
            a = label(author, "meta user-link")
            a.set_ellipsize(Pango.EllipsizeMode.NONE)
            if real_user(author):
                tappable(a, lambda: self.win.open_user(uname(author)))
            mb.append(a)
        if p.get("age"):
            mb.append(label(("  ·  " if author else "") + p["age"], "meta"))
        self.head.append(mb)
        fl = p.get("flair")
        if fl and fl.get("text"):
            f = label(fl["text"], "flair")
            f.set_halign(Gtk.Align.START)
            self.head.append(f)
        self.head.append(copyable(label(p.get("title") or "", "post-title", wrap=True), self.win,
                                  p.get("title") or "", links=[x for x in (p.get("url"),) if x]))
        media = self.media_widget(p.get("media") or {}, quick)
        if media:
            self.head.append(media)
        if p.get("url") and not (p.get("media") or {}).get("type"):
            b = Gtk.Button()
            b.add_css_class("link-btn")
            b.set_child(label("↗ " + (p.get("domain") or urllib.parse.urlparse(p["url"]).netloc or p["url"]), wrap=True))
            b.connect("clicked", lambda _b: self.win.handle_link(p["url"]))
            copyable(b, self.win, p["url"], links=[p["url"]])
            self.head.append(b)
        if p.get("body") and not quick:
            body = label(p["body"], "body md", wrap=True, markup=True)
            body.connect("activate-link", lambda lb, uri: self.win.handle_link(uri))
            copyable(body, self.win, plain(p["body"]), p["body"])
            self.head.append(body)
        self.head.append(label(f"▲ {p.get('score_text') or short_num(p.get('score'))}    "
                               f"◆ {short_num(p.get('comments'))} comments", "foot"))

    def media_widget(self, m, quick):
        t = m.get("type")
        if t == "image":
            pic = Pic(ratio=(m["h"] / m["w"]) if m.get("w") and m.get("h") else None, max_h=900)
            pic.load(img_url(m["src"], FULL_IMG_W))
            self._tap(pic, lambda: self.win.view_images([m["src"]], 0))
            return pic
        if t == "gallery":
            imgs = m.get("images") or []
            car = Adw.Carousel(allow_scroll_wheel=False, spacing=8)
            for i, im in enumerate(imgs):
                pic = Pic(max_h=900)
                pic.load(img_url(im["src"], FULL_IMG_W))
                self._tap(pic, lambda i=i: self.win.view_images([x["src"] for x in imgs], i))
                car.append(pic)
            dots = Adw.CarouselIndicatorDots(carousel=car)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            box.append(car)
            box.append(dots)
            return box
        if t in ("video", "gif"):
            ratio = (m["h"] / m["w"]) if m.get("w") and m.get("h") else 0.5625
            if quick:  # wait for the full post before starting the stream
                pic = Pic(ratio=ratio, max_h=700)
                if m.get("poster"):
                    pic.load(img_url(m["poster"], FULL_IMG_W))
                return pic
            video = VideoPlayer(video_url(m["src"]), gif=(t == "gif"))
            self.media_streams.append(video)
            return Ratio(video, ratio, 700)
        return None

    def _tap(self, widget, fn):
        g = Gtk.GestureClick()
        g.connect("released", lambda *a: fn())
        widget.add_controller(g)

    def comment_widget(self, c, depth):
        self.shown.add(c["id"])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("cmt")
        box.add_css_class(f"d{depth % DEPTH_COLORS}")
        if depth == 0:
            box.add_css_class("top")
        who = c.get("author") or "[deleted]"
        author = label(GLib.markup_escape_text(who) + (" <b>OP</b>" if c.get("op") else ""),
                       "cmt-head user-link" + (" op" if c.get("op") else ""), markup=True)
        author.set_ellipsize(Pango.EllipsizeMode.NONE)
        if real_user(who):
            tappable(author, lambda: self.win.open_user(uname(who)))
        bits = ["", GLib.markup_escape_text(c.get("age") or "")]
        sc = c.get("score_text") or ""
        if sc and sc != "•":
            bits.append("▲ " + GLib.markup_escape_text(sc))
        if c.get("edited"):
            bits.append("<i>edited</i>")
        head = label("  ·  " + "  ·  ".join(b for b in bits if b), "cmt-head", markup=True)
        head.set_hexpand(True)
        headrow = Gtk.Box()
        headrow.append(author)
        headrow.append(head)
        body = label(c.get("body") or "", "cmt-body md", wrap=True, markup=True)
        body.connect("activate-link", lambda lb, uri: self.win.handle_link(uri))
        copyable(body, self.win, plain(c.get("body") or ""), c.get("body") or "")
        kids = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        kids.add_css_class("replies")
        for r in c.get("replies") or []:
            kids.append(self.comment_widget(r, depth + 1))
        if c.get("more"):
            kids.append(self.more_button(c, depth + 1))
        box.append(headrow)
        box.append(body)
        box.append(kids)
        kids.set_visible(kids.get_first_child() is not None)

        def fold(*_a):
            folded = body.get_visible()
            body.set_visible(not folded)
            kids.set_visible(not folded and kids.get_first_child() is not None)
            n = count(c)
            head.set_markup("  ·  " + "  ·  ".join(b for b in bits if b) + (f"   <span foreground='#c5a0ff'>[+{n}]</span>" if folded else ""))
            (box.add_css_class if folded else box.remove_css_class)("folded")
        g = Gtk.GestureClick()
        g.connect("released", fold)
        head.add_controller(g)
        return box

    def more_button(self, c, depth):
        more = c["more"]
        b = Gtk.Button(label=f"↳ more replies ({more.get('count') or '…'})")
        b.add_css_class("more-btn")
        b.set_halign(Gtk.Align.START)

        def clicked(_b):
            b.set_sensitive(False)
            b.set_child(Adw.Spinner())
            bg(lambda: api("/post", path=more["path"], sort=self.sort), got, failed)

        def got(d):
            parent = b.get_parent()
            if parent is None:
                return
            root = find_comment(d.get("comments", []), more["path"].rstrip("/").rsplit("/", 1)[-1])
            replies = (root or {}).get("replies") if root else d.get("comments", [])
            for r in replies or []:
                if r["id"] not in self.shown:
                    parent.insert_child_after(self.comment_widget(r, depth), b)
            parent.remove(b)

        def failed(e):
            b.set_sensitive(True)
            b.set_label("↳ couldn't load — tap to retry")
        b.connect("clicked", clicked)
        return b


def count(c):
    return 1 + sum(count(r) for r in c.get("replies") or [])


def find_comment(cs, cid):
    for c in cs:
        if c["id"] == cid:
            return c
        hit = find_comment(c.get("replies") or [], cid)
        if hit:
            return hit
    return None


# --- image viewer ---------------------------------------------------------------------

class ViewerPage(Adw.NavigationPage):
    def __init__(self, srcs, index):
        super().__init__(title="image")
        self.add_css_class("viewer")
        tv = Adw.ToolbarView(extend_content_to_top_edge=True)
        hb = Adw.HeaderBar(show_title=False)
        hb.add_css_class("viewer-bar")
        tv.add_top_bar(hb)
        self.car = Adw.Carousel(allow_scroll_wheel=False, vexpand=True)
        for s in srcs:
            z = ZoomPic(on_zoom=lambda zoomed: self.car.set_interactive(not zoomed))
            z.load(img_url(s, FULL_IMG_W))
            self.car.append(z)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.car)
        if len(srcs) > 1:
            box.append(Adw.CarouselIndicatorDots(carousel=self.car))
        tv.set_content(box)
        self.set_child(tv)
        if index:
            GLib.idle_add(lambda: (self.car.scroll_to(self.car.get_nth_page(index), False), False)[1])


# --- subreddit picker ---------------------------------------------------------------------

class SearchPage(Adw.NavigationPage):
    """Search posts (all of reddit, or one sub) + a list of subreddits to jump straight into."""

    def __init__(self, win, sub=None):
        super().__init__(title="search r/" + sub if sub else "search", tag=None)
        self.win, self.sub, self.query = win, sub, ""
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.add_css_class("vapor-bar")
        hb.set_title_widget(title_label(("SEARCH R/" + sub.upper()) if sub else "SEARCH"))
        self.feed = Feed(win, self._source, header=Gtk.Box(orientation=Gtk.Orientation.VERTICAL),
                         sort="relevance", on_page=self._page)
        self.subs_box = self.feed.header
        self.feed.status.set_valign(Gtk.Align.START)
        self.subs_box.add_css_class("subs-sheet")
        hb.pack_start(sort_button(self._sorted, [x[0] for x in SEARCH_SORTS]))
        tv.add_top_bar(hb)
        self.entry = Gtk.SearchEntry(placeholder_text=f"search r/{sub}" if sub else "search reddit or r/name",
                                     hexpand=True)
        self.entry.add_css_class("setup-entry")
        self.entry.connect("activate", self._go)
        self.entry.connect("search-changed", self._changed)
        eb = Gtk.Box()
        eb.add_css_class("search-bar")
        eb.append(self.entry)
        tv.add_top_bar(eb)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._browse(), "browse")
        self.stack.add_named(self.feed, "results")
        tv.set_content(self.stack)
        self.set_child(tv)
        if sub:   # searching one sub: nothing to browse, start with the keyboard up
            self.stack.set_visible_child_name("results")
            self.feed.status.set_text(f"type and press Enter to search r/{sub}")
            self.feed.status.set_visible(True)
            self.feed.spinner.set_visible(False)
            GLib.idle_add(lambda: (self.entry.grab_focus(), False)[1])
        self.connect("showing", lambda p: self.fill_browse())

    # --- browse list: your subs + busy today; tap = visit, left button = join/leave ---
    def _browse(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("subs-sheet")
        box.append(label("YOUR SUBREDDITS", "section", xalign=0))
        self.mine = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.mine)
        box.append(label("POPULAR  ·  busy on reddit today", "section", xalign=0))
        self.popular = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.popular)
        if self.win.suggested is None:
            self.popular.append(Adw.Spinner(height_request=28))
            bg(lambda: api("/suggest").get("subs", []), self._suggested, lambda e: self._suggested([]))
        return Gtk.ScrolledWindow(child=box, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _suggested(self, subs):
        if subs:
            self.win.suggested = subs
        self.fill_browse()

    def _joined(self, name):
        return any(x.lower() == name.lower() for x in self.win.subs)

    def _join_toggle(self, name):
        if self._joined(name):
            self.win.set_subs([x for x in self.win.subs if x.lower() != name.lower()])
            self.win.toast(f"left r/{name}")
        else:
            self.win.set_subs(self.win.subs + [name])
            self.win.toast(f"joined r/{name}")
        self.fill_browse()
        self._fill_subs(getattr(self, "found", []))

    def _row(self, name, desc, icon=None):
        return sub_row(name, desc, "object-select-symbolic" if self._joined(name) else "list-add-symbolic",
                       lambda: self._join_toggle(name), lambda: self.win.open_sub(name), icon=icon)

    def fill_browse(self):
        if self.sub:
            return
        for b in (self.mine, self.popular):
            while (c := b.get_first_child()) and not (b is self.popular and self.win.suggested is None):
                b.remove(c)
        if not self.win.subs:
            self.mine.append(label("none yet — tap ✚ on any below", "empty"))
        for name in self.win.subs:
            self.mine.append(self._row(name, None))
        if self.win.suggested is None:
            return
        mine = {x.lower() for x in self.win.subs}
        rows = [x for x in self.win.suggested if x["name"].lower() not in mine]
        if not rows:
            self.popular.append(label("couldn't load the list", "empty"))
        for x in rows:
            desc = "  ·  ".join(v for v in ((x.get("members_text") or "") and x["members_text"] + " members",
                                            x.get("title") or "") if v)
            self.popular.append(self._row(x["name"], desc, x.get("icon")))

    # --- search ---
    def _source(self, sort, t, after):
        if not self.query:
            return None
        q = dict(q=self.query, sort=sort, t=t)
        if self.sub:
            q["sub"] = self.sub
        if after:
            q["after"] = after
        return "/search", q

    def _changed(self, e):
        if not e.get_text().strip() and not self.sub:
            self.query = ""
            self.stack.set_visible_child_name("browse")

    def _go(self, e):
        q = e.get_text().strip()
        m = re.fullmatch(r"/?r/(\w{2,21})", q)
        if m and not self.sub:      # typed a sub name: go straight there
            self.win.open_sub(m.group(1))
            return
        if not q:
            return
        self.query = q
        self.found = []
        self._fill_subs([])
        self.stack.set_visible_child_name("results")
        self.feed.view.grab_focus()   # drops the keyboard so the results are visible
        self.feed.reload()

    def _sorted(self, i):
        name, sort, t = SEARCH_SORTS[i]
        if self.query:
            self.feed.reload(sort, t)
            self.win.toast(f"sorted by {name}")
        else:
            self.feed.sort, self.feed.t = sort, t

    def _page(self, d):
        if "subs" in d:
            self.found = d["subs"]
            self._fill_subs(self.found)

    def _fill_subs(self, subs):
        while (c := self.subs_box.get_first_child()):
            self.subs_box.remove(c)
        if subs:
            self.subs_box.append(label("SUBREDDITS", "section", xalign=0))
            for x in subs:
                desc = "  ·  ".join(v for v in ((x.get("members_text") or "") and x["members_text"] + " members",
                                                x.get("desc") or "") if v)
                self.subs_box.append(self._row(x["name"], desc))
            self.subs_box.append(label("POSTS", "section", xalign=0))
        self.subs_box.set_visible(bool(subs))


def sub_row(name, sub, btn_icon, on_btn, on_open=None, icon=None):
    r = Gtk.Box(spacing=8)
    r.add_css_class("sub-row")
    b = Gtk.Button(icon_name=btn_icon)   # action button first: left thumb
    b.add_css_class("flat")
    b.connect("clicked", lambda _b: on_btn())
    r.append(b)
    if icon:
        pic = Pic(ratio=1.0, css="sub-icon", size=36)
        pic.set_valign(Gtk.Align.CENTER)
        pic.load(img_url(icon, 96))
        r.append(pic)
    txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
    txt.append(label("r/" + name, "sub-name"))
    if sub:
        txt.append(label(sub, "sub-desc", wrap=True, lines=2))
    r.append(txt)
    if on_open:
        g = Gtk.GestureClick()
        g.connect("released", lambda *a: on_open())
        txt.add_controller(g)
    return r


class SubsDialog(Adw.Dialog):
    def __init__(self, win):
        super().__init__(title="subreddits", content_height=640)
        self.win = win
        self.timer = 0
        self.qgen = 0
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_title_widget(title_label("SUBREDDITS"))
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("subs-sheet")
        self.entry = Gtk.SearchEntry(placeholder_text="find or type a subreddit")
        self.entry.add_css_class("setup-entry")
        self.entry.connect("search-changed", self._changed)
        self.entry.connect("activate", self._enter)
        box.append(self.entry)
        self.results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.results)
        box.append(label("IN YOUR FEED", "section", xalign=0))
        self.mine = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.mine)
        box.append(label("SUGGESTED  ·  busy on reddit today", "section", xalign=0))
        self.suggested = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.suggested)
        sc = Gtk.ScrolledWindow(child=box, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        tv.set_content(sc)
        self.set_child(tv)
        self.fill_mine()
        if win.suggested is None:
            self.suggested.append(Adw.Spinner(height_request=28))
            bg(lambda: api("/suggest").get("subs", []), self._suggested, lambda e: self._suggested([]))
        else:
            self.fill_suggested()

    def _suggested(self, subs):
        if subs:
            self.win.suggested = subs
        self.fill_suggested()

    def fill_suggested(self):
        while (c := self.suggested.get_first_child()):
            self.suggested.remove(c)
        mine = {s.lower() for s in self.win.subs}
        rows = [s for s in (self.win.suggested or []) if s["name"].lower() not in mine]
        if not rows:
            self.suggested.append(label("couldn't load suggestions" if self.win.suggested is None
                                        else "you've joined them all", "empty"))
        for s in rows:
            desc = "  ·  ".join(x for x in ((s.get("members_text") or "") and s["members_text"] + " members",
                                            s.get("title") or "") if x)
            self.suggested.append(self.row(s["name"], desc, "list-add-symbolic",
                                           lambda n=s["name"]: self.add(n), lambda n=s["name"]: self.open(n),
                                           icon=s.get("icon")))

    def row(self, *a, **kw):
        return sub_row(*a, **kw)

    def fill_mine(self):
        while (c := self.mine.get_first_child()):
            self.mine.remove(c)
        if not self.win.subs:
            self.mine.append(label("nothing yet — search above", "empty"))
        for s in self.win.subs:
            self.mine.append(self.row(s, None, "list-remove-symbolic", lambda s=s: self.remove(s),
                                      lambda s=s: self.open(s)))

    def open(self, name):
        self.close()
        self.win.open_sub(name)

    def add(self, name):
        if not any(s.lower() == name.lower() for s in self.win.subs):
            self.win.set_subs(self.win.subs + [name])
            self.win.toast(f"joined r/{name}")
        self.fill_mine()
        self.fill_suggested()

    def remove(self, name):
        self.win.set_subs([s for s in self.win.subs if s != name])
        self.win.toast(f"left r/{name}")
        self.fill_mine()
        self.fill_suggested()

    def _enter(self, e):
        name = re.sub(r"^/?r/", "", e.get_text().strip())
        if re.fullmatch(r"\w{2,21}", name):
            self.add(name)
            e.set_text("")

    def _changed(self, e):
        if self.timer:
            GLib.source_remove(self.timer)
        self.timer = GLib.timeout_add(450, self._search)

    def _search(self):
        self.timer = 0
        q = self.entry.get_text().strip()
        self.qgen += 1
        gen = self.qgen
        while (c := self.results.get_first_child()):
            self.results.remove(c)
        if len(q) < 2:
            return False
        self.results.append(Adw.Spinner(height_request=28))
        bg(lambda: api("/subs", q=q), lambda d: self._found(gen, d), lambda e: self._found(gen, {"subs": []}))
        return False

    def _found(self, gen, d):
        if gen != self.qgen:
            return
        while (c := self.results.get_first_child()):
            self.results.remove(c)
        for s in d.get("subs", [])[:12]:
            desc = (s.get("members_text") or "") + ("  ·  " + s["desc"] if s.get("desc") else "")
            self.results.append(self.row(s["name"], desc, "list-add-symbolic",
                                         lambda n=s["name"]: self.add(n), lambda n=s["name"]: self.open(n)))
        if not d.get("subs"):
            self.results.append(label("no matches — Enter adds it as typed", "empty"))


# --- window -------------------------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="notreal.feed", default_width=400, default_height=800)
        self.subs = load_subs()
        self.suggested = None    # /suggest result, fetched once per run
        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)
        self.home = HomePage(self)
        self.nav.add(self.home)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=2))

    def set_subs(self, subs):
        self.subs = subs
        save_subs(subs)
        self.home.subs_changed()

    def edit_subs(self):
        SubsDialog(self).present(self)

    def open_post(self, permalink, preview=None):
        if permalink:
            self.nav.push(PostPage(self, permalink, preview))

    def open_sub(self, name):
        if name:
            self.nav.push(SubPage(self, name))

    def open_search(self, sub=None):
        self.nav.push(SearchPage(self, sub))

    def open_user(self, name):
        if name:
            self.nav.push(UserPage(self, name))

    def view_images(self, srcs, index):
        self.nav.push(ViewerPage(srcs, index))

    def handle_link(self, uri):
        """Label link / post link -> in-app page when we can, browser otherwise. True = handled."""
        u = urllib.parse.urlparse(uri)
        host = u.netloc.lower()
        path = u.path
        if host in ("i.redd.it",):
            self.view_images(["/img" + path], 0)
            return True
        if host == "preview.redd.it":
            self.view_images(["/preview/pre" + path + ("?" + u.query if u.query else "")], 0)
            return True
        internal = not host or host.endswith("reddit.com") or host == "redd.it"
        if internal:
            if path.startswith(("/img/", "/preview/")):
                self.view_images([path + ("?" + u.query if u.query else "")], 0)
                return True
            m = re.match(r"^/r/(\w+)/comments/(\w+)", path)
            if m:
                self.open_post(path)
                return True
            m = re.match(r"^/r/(\w+)/?$", path)
            if m:
                self.open_sub(m.group(1))
                return True
            m = re.match(r"^/(?:u|user)/([\w-]+)/?$", path)
            if m:
                self.open_user(m.group(1))
                return True
            if host == "redd.it" and re.fullmatch(r"/\w+", path):
                open_external("https://redd.it" + path)
                return True
            open_external("https://www.reddit.com" + path + ("?" + u.query if u.query else ""))
            return True
        open_external(uri)
        return True


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        # shared look from ~/theme/theme.css, then this app's style.css (both live-reload)
        notareal_theme.apply(os.path.join(HERE, "style.css"))

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        if not self.win:
            self.win = Window(self)
        self.win.present()
        for a in args:
            if "reddit.com" in a or a.startswith(("/r/", "/u/", "/user/")):
                GLib.timeout_add(300, lambda a=a: (self.win.handle_link(a), False)[1])
        return 0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    sys.exit(App().run(sys.argv))
