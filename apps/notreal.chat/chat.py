#!/usr/bin/env python3
"""notarealChat: WhatsApp / Messenger / Google Voice (Matrix bridges) and SMS in one
vapor-styled, left-handed chat app. Replaces Chatty.

Chat list: swipe left = archive (Undo), swipe right = read/unread, filter chips per network,
NEW button bottom-left. In a chat: swipe a bubble right = reply, long-press = react / copy /
delete, Send + GIF + photos on the composer.
`chat.py --service` keeps it running in the background for notifications (autostart).
"""
import datetime
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango  # noqa: E402
import notareal_theme  # noqa: E402  ~/theme/theme.css

try:
    gi.require_version("Lfb", "0.0")
    from gi.repository import Lfb  # feedbackd: the phone's message buzz/sound
except (ValueError, ImportError):
    Lfb = None

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import contacts  # noqa: E402
import gifs  # noqa: E402
import sms as smsmod  # noqa: E402
from matrix import Matrix  # noqa: E402
from store import Store  # noqa: E402

APP_ID = "dev.notareal.Chat"
LOCK = os.path.join(GLib.get_user_runtime_dir(), "notarealchat.lock")
CACHE = os.path.expanduser("~/.cache/notarealchat")
SWIPE_COMMIT = 0.33
SWIPE_START = 14
REPLY_PULL = 70            # px a bubble must slide right to reply
UNDO_S = 5
PAGE = 50                  # messages per load
FIRST_PAGE = 20            # opening a chat; older ones load when you scroll up
PAGE_CACHE = 4             # recently opened chats kept built
BUBBLE_MAX = 320           # px
NETS = {"wa": "WA", "fb": "FB", "ig": "IG", "gv": "GV", "sms": "SMS", "mx": "MX"}
NET_NAMES = {"wa": "WhatsApp", "fb": "Messenger", "ig": "Instagram", "gv": "Google Voice",
             "sms": "SMS", "mx": "Matrix"}
QUICK_REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏"]
PHOTO_DIRS = ["~/Pictures/Camera", "~/Pictures/Screenshots", "~/Pictures", "~/Videos/Camera", "~/Downloads"]
URL_RE = re.compile(r"(https?://[^\s<>\"]+|www\.[^\s<>\"]+)")


# --- helpers -----------------------------------------------------------------

# timing tests (10-17): $XDG_RUNTIME_DIR/nrchat-test exists -> log TIMING lines; if it holds a chat
# id, the window opens + closes that chat twice (the service wrapper drops env vars on restart)
TEST_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "nrchat-test")


def timing_on():
    return bool(os.environ.get("NRCHAT_TIMING")) or os.path.exists(TEST_FILE)


def frame_watch(widget, what, secs=0.7):
    """Log the frame timing of a transition: count + worst gap between frames."""
    if not timing_on():
        return
    times = []
    t_start = GLib.get_monotonic_time() / 1e3
    print("TIMING %s starts at %.0f" % (what, t_start % 100000), flush=True)

    def tick(w, clock):
        times.append(clock.get_frame_time() / 1e3)
        if times[-1] - times[0] < secs * 1000:
            return GLib.SOURCE_CONTINUE
        gaps = [b - a for a, b in zip(times, times[1:])]
        print("TIMING %s: %d frames, worst gap %.0f ms, >25 ms gaps: %s" % (
            what, len(times), max(gaps or [0]), ["%.0f@%.0f" % (g, times[i] - t_start) for i, g in
                                                  enumerate(gaps) if g > 25]), flush=True)
        return GLib.SOURCE_REMOVE
    widget.add_tick_callback(tick)


def timed(what, t0):
    if timing_on():
        print("TIMING %s took %.0f ms" % (what, (time.time() - t0) * 1000), flush=True)


def bg(fn, done=None, fail=None):
    """Run fn() in a thread, deliver the result (or exception) on the main loop."""
    def run():
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            print("bg error:", repr(e), flush=True)
            if fail:
                GLib.idle_add(lambda e=e: (fail(e), False)[1])
            return
        if done:
            GLib.idle_add(lambda: (done(res), False)[1])
    threading.Thread(target=run, daemon=True).start()


def label(text="", css=None, xalign=0.0, ellipsize=True, wrap=False):
    lb = Gtk.Label(label=text, xalign=xalign)
    if ellipsize:
        lb.set_ellipsize(Pango.EllipsizeMode.END)
    if wrap:
        lb.set_wrap(True)
        lb.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    for c in (css or "").split():
        lb.add_css_class(c)
    return lb


def icon_button(icon, tip, cb, css=None):
    b = Gtk.Button(icon_name=icon, tooltip_text=tip)
    b.add_css_class("flat")
    for c in (css or "").split():
        b.add_css_class(c)
    b.connect("clicked", lambda *_: cb())
    return b


def text_button(text, cb, css="flat"):
    b = Gtk.Button(label=text)
    for c in css.split():
        b.add_css_class(c)
    b.connect("clicked", lambda *_: cb())
    return b


def list_time(ms):
    d = datetime.datetime.fromtimestamp(ms / 1000)
    now = datetime.datetime.now()
    if d.date() == now.date():
        return d.strftime("%H:%M")
    if (now.date() - d.date()).days < 7:
        return d.strftime("%a")
    if d.year == now.year:
        return d.strftime("%-d %b")
    return d.strftime("%-d/%-m/%y")


def day_title(ms):
    d = datetime.datetime.fromtimestamp(ms / 1000).date()
    today = datetime.date.today()
    if d == today:
        return "TODAY"
    if d == today - datetime.timedelta(days=1):
        return "YESTERDAY"
    if (today - d).days < 7:
        return d.strftime("%A").upper()
    return d.strftime("%-d %B %Y").upper()


def buzz(event="button-pressed"):
    subprocess.Popen(["fbcli", "-E", event], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def linkify(text):
    out, last = [], 0
    for m in URL_RE.finditer(text):
        out.append(GLib.markup_escape_text(text[last:m.start()]))
        url = m.group(0).rstrip(".,!?)")
        href = url if url.startswith("http") else "https://" + url
        out.append('<a href="%s">%s</a>' % (GLib.markup_escape_text(href), GLib.markup_escape_text(url)))
        out.append(GLib.markup_escape_text(m.group(0)[len(url):]))
        last = m.end()
    out.append(GLib.markup_escape_text(text[last:]))
    return "".join(out)


def only_emoji(text):
    t = text.strip()
    return 0 < len(t) <= 8 and not any(ch.isalnum() or ch.isspace() for ch in t)


_textures = {}


def texture(path):
    """Cached Gdk.Texture for a local file (None if it can't be read)."""
    if not path:
        return None
    t = _textures.get(path)
    if t is None:
        try:
            t = Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            return None
        if len(_textures) > 300:
            _textures.clear()
        _textures[path] = t
    return t


KARERE = "dev.notareal.WaCall.desktop"   # WhatsApp Web in a lean Chromium (~/wacall); was Karere
NEWCHAT_CHOICES = os.path.expanduser("~/.local/share/newchat/choices.json")   # name -> number (New Chat app)
CALL_NOTICE = "Incoming call"   # the WhatsApp bridge's notice when someone calls


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def launch_desktop(desktop_id):
    info = Gio.DesktopAppInfo.new(desktop_id)
    if not info:
        return False
    info.launch([], None)
    return True


MAPS = os.path.expanduser("~/maps/notarealmaps")


NRLINK = os.path.expanduser("~/link/nrlink")


def open_uri(uri, parent=None):
    """Web links -> notarealLink (light WebKit viewer, opens fast); anything else -> the system."""
    if uri.lower().startswith(("http://", "https://")) and os.path.exists(NRLINK):
        subprocess.Popen([NRLINK, uri], start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    Gtk.UriLauncher.new(uri).launch(parent, None, None, None)


# --- sliding row (chat list) --------------------------------------------------------

class SwipeRow(Gtk.Box):
    """A row that slides sideways. Right = read/unread (cyan), left = archive (pink).
    10-17: built on a native Gtk.Overlay (was Python do_measure/do_size_allocate, called for all
    ~120 rows whenever the list reappeared: ~130 ms stall when leaving a chat). The Python
    position handler is attached only while the row is actually offset."""
    __gsignals__ = {"swiped": (GObject.SignalFlags.RUN_FIRST, None, (int,))}

    def __init__(self, content, right_text, left_text):
        super().__init__()
        self.offset = 0.0
        self.swiping = False
        self.armed = 0
        self.anim = None
        self.pos_id = 0
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.texts = (right_text, left_text)
        self.under = None          # the READ / ARCHIVE layer is built on the first swipe (fewer widgets)
        self.ov = Gtk.Overlay(hexpand=True)
        self.fg = content
        self.ov.add_overlay(self.fg)
        self.ov.set_measure_overlay(self.fg, True)
        self.append(self.ov)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.add_controller(drag)

    def _pos(self, ov, child, r):
        if child is not self.fg:
            return False
        r.x, r.y, r.width, r.height = int(self.offset), 0, ov.get_width(), ov.get_height()
        return True

    def _make_under(self):
        self.under = Gtk.CenterBox()
        self.under.add_css_class("swipe-under")
        self.left_hint = label(self.texts[0], "hint-read")
        self.right_hint = label(self.texts[1], "hint-archive", xalign=1.0)
        self.under.set_start_widget(self.left_hint)
        self.under.set_end_widget(self.right_hint)
        self.ov.set_child(self.under)

    def _set_offset(self, x):
        self.offset = x
        if self.under is None:
            self._make_under()
        w = max(1, self.get_width())
        self.left_hint.set_visible(x > 0)
        self.right_hint.set_visible(x < 0)
        for c in ("read", "archive"):
            self.under.remove_css_class(c)
        if x:
            self.under.add_css_class("read" if x > 0 else "archive")
        armed = (1 if x > 0 else -1) if abs(x) >= w * SWIPE_COMMIT else 0
        if armed != self.armed:
            self.armed = armed
            (self.under.add_css_class if armed else self.under.remove_css_class)("armed")
            if armed:
                buzz()
        if x and not self.pos_id:
            self.pos_id = self.ov.connect("get-child-position", self._pos)
        elif not x and self.pos_id:
            self.ov.disconnect(self.pos_id)
            self.pos_id = 0
        self.ov.queue_allocate()

    def _begin(self, g, x, y):
        if self.anim:
            self.anim.skip()
        self.swiping = False

    def _update(self, g, dx, dy):
        if not self.swiping:
            if abs(dx) > SWIPE_START and abs(dx) > abs(dy) * 1.4:
                self.swiping = True
                g.set_state(Gtk.EventSequenceState.CLAIMED)
            elif abs(dy) > SWIPE_START:
                g.set_state(Gtk.EventSequenceState.DENIED)
                return
            else:
                return
        self._set_offset(dx)

    def _end(self, g, dx, dy):
        if not self.swiping:
            return
        self.swiping = False
        w = self.get_width()
        if abs(self.offset) >= w * SWIPE_COMMIT:
            d = 1 if self.offset > 0 else -1
            if d > 0:  # read/unread: slide back, the row stays
                self.animate_to(0, lambda: self.emit("swiped", d))
            else:
                self.animate_to(-w, lambda: self.emit("swiped", d))
        else:
            self.animate_to(0)

    def animate_to(self, x, done=None):
        target = Adw.CallbackAnimationTarget.new(lambda v: self._set_offset(v))
        self.anim = Adw.TimedAnimation.new(self, self.offset, x, 180, target)
        self.anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        if done:
            self.anim.connect("done", lambda *_: done())
        self.anim.play()


class ReplySlide(Gtk.Box):
    """Wraps a message; dragging it right past REPLY_PULL emits 'reply'.
    10-17: native Gtk.Overlay layout (see SwipeRow); the hint only exists on screen while dragging."""
    __gsignals__ = {"reply": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, content):
        super().__init__()
        self.offset = 0.0
        self.swiping = False
        self.armed = False
        self.pos_id = 0
        self.ov = Gtk.Overlay(hexpand=True, child=Gtk.Box())
        self.hint = label("↩", "reply-hint", xalign=0.5, ellipsize=False)
        self.hint.set_visible(False)
        self.fg = content
        self.ov.add_overlay(self.fg)
        self.ov.set_measure_overlay(self.fg, True)
        self.ov.add_overlay(self.hint)
        self.append(self.ov)
        drag = Gtk.GestureDrag(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        drag.connect("drag-begin", lambda *_: setattr(self, "swiping", False))
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.add_controller(drag)

    def _pos(self, ov, child, r):
        w, h = ov.get_width(), ov.get_height()
        if child is self.fg:
            r.x, r.y, r.width, r.height = int(self.offset), 0, w, h
        elif child is self.hint:
            hw = min(40, w)
            r.x, r.y, r.width, r.height = int(max(0, self.offset - hw - 4)), int((h - 40) / 2), hw, 40
        else:
            return False
        return True

    def _set(self, x):
        self.offset = x
        self.hint.set_visible(x > 0)
        self.hint.set_opacity(min(1.0, x / REPLY_PULL))
        armed = x >= REPLY_PULL
        if armed != self.armed:
            self.armed = armed
            (self.hint.add_css_class if armed else self.hint.remove_css_class)("armed")
            if armed:
                buzz()
        if x and not self.pos_id:
            self.pos_id = self.ov.connect("get-child-position", self._pos)
        elif not x and self.pos_id:
            self.ov.disconnect(self.pos_id)
            self.pos_id = 0
        self.ov.queue_allocate()

    def _update(self, g, dx, dy):
        if not self.swiping:
            if dx > SWIPE_START and dx > abs(dy) * 1.5:
                self.swiping = True
                g.set_state(Gtk.EventSequenceState.CLAIMED)
            elif abs(dy) > SWIPE_START or dx < -SWIPE_START:
                g.set_state(Gtk.EventSequenceState.DENIED)
                return
            else:
                return
        self._set(max(0, min(dx * 0.7, REPLY_PULL * 1.4)))

    def _end(self, g, dx, dy):
        if not self.swiping:
            return
        self.swiping = False
        fire = self.armed
        target = Adw.CallbackAnimationTarget.new(lambda v: self._set(v))
        a = Adw.TimedAnimation.new(self, self.offset, 0, 160, target)
        a.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        a.play()
        if fire:
            self.emit("reply")


# --- avatars ------------------------------------------------------------------------

def avatar(app, name, mxc, size):
    av = Adw.Avatar(size=size, text=name or "?", show_initials=True)
    if mxc:
        def got(path):
            t = texture(path)
            if t:
                av.set_custom_image(t)
        bg(lambda: app.matrix.fetch(mxc, 96, 96, crop=True), got)
    return av


def net_chip(net):
    return label(NETS.get(net, "?"), "chip net-" + (net or "mx"), xalign=0.5, ellipsize=False)


# --- chat list -------------------------------------------------------------------------

class ChatItem(GObject.Object):
    """One entry of the chat list model: a chat id, or "archived" / "empty"."""

    def __init__(self, key):
        super().__init__()
        self.key = key


class ChatList(Adw.NavigationPage):
    def __init__(self, win):
        super().__init__(title="Chats", tag="list")
        self.win = win
        self.app = win.app
        self.filter = "all"
        self.archived_view = False
        self.hidden_view = False
        self.pending = {}         # chat id -> undo timer while an archive can be undone

        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        hb.add_css_class("vapor-bar")
        self.title = label("CHATS", "app-title", xalign=0.5, ellipsize=False)
        hb.set_title_widget(self.title)
        self.back_btn = icon_button("go-previous-symbolic", "Back to chats", self._leave_special)
        self.back_btn.set_visible(False)
        hb.pack_start(self.back_btn)
        self.search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic", tooltip_text="Search")
        self.search_btn.add_css_class("flat")
        hb.pack_start(self.search_btn)
        menu = Gio.Menu()
        menu.append("Archived chats", "win.show-archived")
        menu.append("Bridge rooms", "win.show-hidden")
        menu.append("Refresh contacts", "win.refresh-contacts")
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, css_classes=["flat"]))
        tv.add_top_bar(hb)

        self.search = Gtk.SearchEntry(placeholder_text="search chats", hexpand=True)
        self.search.add_css_class("find")
        self.search.connect("search-changed", lambda *_: self.refresh())
        sb = Gtk.SearchBar(child=self.search, show_close_button=False)
        sb.connect_entry(self.search)
        self.search_btn.bind_property("active", sb, "search-mode-enabled",
                                      GObject.BindingFlags.BIDIRECTIONAL)
        tv.add_top_bar(sb)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.chips = Gtk.Box(spacing=4, margin_start=10, margin_end=10, margin_top=6, margin_bottom=4)
        self.chips.add_css_class("filters")
        box.append(self.chips)
        self.status = label("", "net-status", xalign=0.5)
        self.status.set_visible(False)
        box.append(self.status)
        # 10-17: a virtualized ListView (was a ListBox): only the rows on screen are built, laid out
        # and drawn. With ~120 chats the ListBox redid all of them whenever the list reappeared
        # (~170 ms stall at the start of leaving a chat).
        self.model = Gio.ListStore(item_type=ChatItem)
        factory = Gtk.SignalListItemFactory()
        factory.connect("bind", self._bind)
        factory.connect("unbind", self._unbind)
        self.lb = Gtk.ListView(model=Gtk.NoSelection(model=self.model), factory=factory,
                               single_click_activate=True)
        self.lb.add_css_class("chat-list")
        self.lb.set_margin_bottom(96)
        self.lb.connect("activate", self._activated)
        self._data = {}
        self._rowcache = {}
        self._bound = set()
        self.scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.lb)
        box.append(self.scroller)
        self.empty = label("no chats here", "empty", xalign=0.5)

        ov = Gtk.Overlay(child=box)
        fab = Gtk.Button(halign=Gtk.Align.START, valign=Gtk.Align.END, margin_start=18, margin_bottom=22)
        fab.add_css_class("fab")
        fb = Gtk.Box(spacing=8)
        fb.append(Gtk.Image(icon_name="document-edit-symbolic"))
        fb.append(label("NEW", "fab-label", ellipsize=False))
        fab.set_child(fb)
        fab.connect("clicked", lambda *_: self.win.new_chat())
        ov.add_overlay(fab)
        self.toasts = Adw.ToastOverlay(child=ov)
        tv.set_content(self.toasts)
        self.set_child(tv)
        self.refresh()

    # --- views ---
    def show_archived(self):
        self.archived_view, self.hidden_view = True, False
        self.refresh()

    def show_hidden(self):
        self.hidden_view, self.archived_view = True, False
        self.refresh()

    def _leave_special(self):
        self.archived_view = self.hidden_view = False
        self.refresh()

    def _chip(self, key, text, count):
        b = Gtk.ToggleButton(label=text + (" %d" % count if count else ""))
        b.add_css_class("filter")
        if key != "all":
            b.add_css_class("net-" + key)
        b.set_active(self.filter == key)
        b.connect("clicked", lambda *_: self._set_filter(key))
        return b

    def _set_filter(self, key):
        self.filter = key
        self.refresh()

    def refresh(self):
        st = self.app.store
        special = self.archived_view or self.hidden_view
        self.back_btn.set_visible(special)
        self.search_btn.set_visible(not special)
        self.title.set_label("ARCHIVED" if self.archived_view else "BRIDGES" if self.hidden_view else "CHATS")
        chats = st.hidden_chats() if self.hidden_view else st.chats()
        chats = [c for c in chats if c["id"] not in self.pending]
        # chips: networks that have chats (unread count per net)
        self.chips.set_visible(not special)
        inbox = [c for c in chats if not c["archived"]]
        nets = []
        for c in inbox:
            if c["net"] and c["net"] not in nets:
                nets.append(c["net"])
        order = ["wa", "fb", "ig", "sms", "gv", "mx"]
        nets.sort(key=lambda n: order.index(n) if n in order else 99)
        if self.filter != "all" and self.filter not in nets:
            self.filter = "all"
        chips = [("all", "ALL", sum(1 for c in inbox if c["unread"]))] + [
            (n, NETS.get(n, n.upper()), sum(1 for c in inbox if c["net"] == n and c["unread"])) for n in nets]
        if (chips, self.filter, special) != getattr(self, "_chips_shown", None):   # unchanged: no relayout
            self._chips_shown = (chips, self.filter, special)
            while self.chips.get_first_child():
                self.chips.remove(self.chips.get_first_child())
            for key, name, n in chips:
                self.chips.append(self._chip(key, name, n))

        t = self.search.get_text().strip().lower() if not special else ""
        if self.archived_view:
            show = [c for c in chats if c["archived"]]
        elif self.hidden_view:
            show = chats
        elif t:
            show = [c for c in chats if t in (c["name"] or "").lower() or t in (c["last_text"] or "").lower()]
        else:
            show = [c for c in inbox if self.filter in ("all", c["net"])]
        # rows are cached by what they show (key -> (signature, widget)) and only built when they
        # scroll into view (_bind); an unchanged list is left completely alone
        mode = (self.archived_view, self.hidden_view)
        arch = [c for c in chats if c["archived"]] if not special and not t else []
        want = []
        data = {}
        if arch:
            want.append(("archived", (len(arch), sum(1 for c in arch if c["unread"]))))
            data["archived"] = arch
        for c in show:
            want.append((c["id"], (mode, c["name"], c["avatar"], c["net"], c["unread"], c["last_ts"],
                                   list_time(c["last_ts"]) if c["last_ts"] else "", c["last_mine"],
                                   c["last_text"], c["invite"], c["muted"])))
            data[c["id"]] = c
        if not show:
            want.append(("empty", ("no chats here" if not t else "nothing matches",)))
        if want == getattr(self, "_shown", None):
            return
        self._data = data
        self._sigs = dict(want)
        self._rowcache = {k: v for k, v in self._rowcache.items() if self._sigs.get(k) == v[0]}
        self._shown = want
        self.model.splice(0, self.model.get_n_items(), [ChatItem(k) for k, _sig in want])

    def _widget_for(self, key):
        sig = self._sigs.get(key)
        hit = self._rowcache.get(key)
        if hit and hit[0] == sig:
            return hit[1]
        if key == "archived":
            w = self._archived_row(self._data["archived"])
        elif key == "empty":
            w = label(sig[0], "empty", xalign=0.5)
        else:
            w = self._row(self._data[key])
        self._rowcache[key] = (sig, w)
        return w

    def _bind(self, _f, li):
        w = self._widget_for(li.get_item().key)
        old = w.get_parent()          # a cached row still sitting in another (recycled) list row
        if old is not None:
            for other in self._bound:
                if other.get_child() is w and other is not li:
                    other.set_child(None)
        li.set_child(w)
        self._bound.add(li)

    def _unbind(self, _f, li):
        li.set_child(None)
        self._bound.discard(li)

    def _archived_row(self, arch):
        b = Gtk.Box(spacing=14)
        b.add_css_class("archived-row")
        b.append(Gtk.Image(icon_name="folder-symbolic", pixel_size=22))
        b.append(label("ARCHIVED", "arch-title"))
        unread = sum(1 for c in arch if c["unread"])
        n = label("%d" % len(arch) + ("  · %d new" % unread if unread else ""), "arch-count", xalign=1.0)
        n.set_hexpand(True)
        b.append(n)
        b.special = "archived"
        return b

    def _row(self, c):
        box = Gtk.Box(spacing=12)
        box.add_css_class("chat-row")
        box.add_css_class("net-" + (c["net"] or "mx"))
        if c["unread"]:
            box.add_css_class("unread")
        box.append(avatar(self.app, c["name"], c["avatar"], 50))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True, valign=Gtk.Align.CENTER)
        top = Gtk.Box(spacing=8)
        name = label(c["name"] or c["id"], "chat-name")
        name.set_hexpand(True)
        top.append(name)
        top.append(net_chip(c["net"]))
        top.append(label(list_time(c["last_ts"]) if c["last_ts"] else "", "chat-time", xalign=1.0,
                         ellipsize=False))
        col.append(top)
        bottom = Gtk.Box(spacing=8)
        if c["invite"]:
            txt = "invited you · tap to join"
        else:
            txt = ("you: " if c["last_mine"] else "") + (c["last_text"] or "")
        pv = label(txt, "chat-preview")
        pv.set_hexpand(True)
        bottom.append(pv)
        if c["muted"]:
            bottom.append(Gtk.Image(icon_name="notifications-disabled-symbolic", css_classes=["muted-icon"]))
        if c["unread"]:
            bottom.append(label(str(c["unread"]) if c["unread"] < 100 else "99+", "badge", xalign=0.5,
                                ellipsize=False))
        col.append(bottom)
        box.append(col)
        if self.archived_view:
            sr = SwipeRow(box, "✓ READ" if c["unread"] else "● UNREAD", "UNARCHIVE ↩")
        elif self.hidden_view:
            sr = SwipeRow(box, "", "")
        else:
            sr = SwipeRow(box, "✓ READ" if c["unread"] else "● UNREAD", "ARCHIVE ▣")
        sr.connect("swiped", lambda _r, d, cid=c["id"]: self._swiped(cid, d))
        lp = Gtk.GestureLongPress()
        lp.connect("pressed", lambda g, x, y, cid=c["id"], w=box: self._menu(cid, w))
        box.add_controller(lp)
        sr.cid = c["id"]
        return sr

    def _activated(self, lv, pos):
        item = self.model.get_item(pos)
        key = item.key if item else None
        if key == "archived":
            return self.show_archived()
        if key and key != "empty":
            self.win.open_chat(key)

    def _swiped(self, cid, d):
        app = self.app
        c = app.store.chat(cid)
        if not c or self.hidden_view:
            return
        if d > 0:
            if c["unread"]:
                app.mark_read(cid)
            else:
                app.store.upsert_chat(cid, unread=1)
            return self.refresh()
        on = not c["archived"]
        self.pending[cid] = True
        self.refresh()

        def commit():
            if self.pending.pop(cid, None):
                app.set_archived(cid, on)
                self.refresh()
            return False
        timer = GLib.timeout_add_seconds(UNDO_S, commit)
        toast = Adw.Toast(title=("archived " if on else "back in chats: ") + (c["name"] or ""),
                          button_label="UNDO", timeout=UNDO_S)

        def undo(*_):
            if self.pending.pop(cid, None):
                GLib.source_remove(timer)
                self.refresh()
        toast.connect("button-clicked", undo)
        self.toasts.add_toast(toast)

    def _menu(self, cid, widget):
        c = self.app.store.chat(cid)
        if not c:
            return
        buzz()
        pop = Gtk.Popover()
        pop.set_parent(widget)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("menu-box")

        def item(text, cb):
            b = text_button(text, lambda: (pop.popdown(), cb()), "flat menu-item")
            b.get_child().set_xalign(0)
            box.append(b)
        item("unmute" if c["muted"] else "mute notifications",
             lambda: (self.app.store.upsert_chat(cid, muted=int(not c["muted"])), self.refresh()))
        item("unarchive" if c["archived"] else "archive", lambda: self._swiped(cid, -1))
        item("mark unread" if not c["unread"] else "mark read", lambda: self._swiped(cid, 1))
        item("delete chat…", lambda: self.win.confirm_delete(cid))
        pop.set_child(box)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()


# --- one chat ----------------------------------------------------------------------------

class ChatPage(Adw.NavigationPage):
    def __init__(self, win, cid):
        self.win, self.app, self.cid = win, win.app, cid
        c = self.app.store.chat(cid)
        super().__init__(title=c["name"] or cid)
        self.chat = c
        self.is_sms = cid.startswith("sms:")
        self.rows = []            # [(id, sig, row)]
        self.reply_to = None
        self.loading_older = False
        self.no_more = False
        # 10-17: open with the newest few only (building + laying out 50 bubbles before the slide-in
        # froze it for ~250 ms); the rest of the page comes in once the transition has finished
        self.limit = FIRST_PAGE
        self.player = None

        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        hb.add_css_class("vapor-bar")
        tbox = Gtk.Box(spacing=10)
        tbox.append(avatar(self.app, c["name"], c["avatar"], 34))
        tc = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        tc.append(label(c["name"] or cid, "head-name"))
        sub = NET_NAMES.get(c["net"], "")
        if self.is_sms:
            sub = "SMS · " + cid[4:]
        tc.append(label(sub, "head-sub net-text-" + (c["net"] or "mx")))
        tbox.append(tc)
        hb.set_title_widget(tbox)
        call = icon_button("call-start-symbolic", "Call", self.call_menu, "call-btn")
        self.call_btn = call
        hb.pack_start(call)
        menu = Gio.Menu()
        menu.append("Mute / unmute", "win.toggle-mute")
        menu.append("Archive", "win.archive-current")
        menu.append("Delete chat…", "win.delete-current")
        hb.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, css_classes=["flat"]))
        tv.add_top_bar(hb)

        self.lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.lb.add_css_class("messages")
        self.scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.lb)
        self.scroller.connect("edge-reached", self._edge)
        self.vadj = self.scroller.get_vadjustment()
        self.stick = True   # keep scrolled to the newest message
        self.vadj.connect("value-changed", self._scrolled)
        self.vadj.connect("notify::upper", self._grew)
        self.toasts = Adw.ToastOverlay(child=self.scroller)
        tv.set_content(self.toasts)
        tv.add_bottom_bar(self._composer())
        self.set_child(tv)
        self.load()

    # --- calls (hand-offs: the bridges carry no calls) ---
    def phone_number(self):
        if self.is_sms:
            return self.cid[4:]
        n = self.app.store.get("phone:" + self.cid)
        if n:
            return n
        name = (self.chat["name"] or "").strip().lower()
        picks = read_json(NEWCHAT_CHOICES, {})
        for k, v in picks.items():
            if k.lower() == name:
                return v
        for pname, nums in self.app.book.people:
            if pname.lower() == name and nums:
                return nums[0][0]
        return None

    def call_menu(self):
        pop = Gtk.Popover()
        pop.set_parent(self.call_btn)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("menu-box")

        def item(text, sub, cb):
            b = Gtk.Button()
            b.add_css_class("flat")
            b.add_css_class("menu-item")
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            col.append(label(text, "call-opt"))
            if sub:
                col.append(label(sub, "call-sub"))
            b.set_child(col)
            b.connect("clicked", lambda *_: (pop.popdown(), cb()))
            box.append(b)
        num = self.phone_number()
        net = self.chat["net"]
        if net == "wa":
            item("WhatsApp call", "opens WhatsApp Calls — tap %s there, then call" % (self.chat["name"] or ""),
                 self.whatsapp_call)
        if net == "gv":
            item("Google Voice call", "opens Voice", lambda: launch_desktop("dev.notareal.Voice.desktop"))
        if num:
            item("Phone call", num + " · mobile network", lambda: open_uri("tel:" + num, self.win))
        else:
            item("Phone call", "no number found in your contacts", lambda: None)
        pop.set_child(box)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def whatsapp_call(self):
        if launch_desktop(KARERE):
            self.toast("WhatsApp is starting (~12 s) — open %s there and tap call" % (self.chat["name"] or "the chat"))
        else:
            self.toast("WhatsApp Calls isn't installed")

    # --- composer ---
    def _composer(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.add_css_class("composer")
        self.reply_bar = Gtk.Box(spacing=8)
        self.reply_bar.add_css_class("reply-bar")
        self.reply_text = label("", "reply-bar-text")
        self.reply_text.set_hexpand(True)
        self.reply_bar.append(Gtk.Image(icon_name="mail-reply-sender-symbolic"))
        self.reply_bar.append(self.reply_text)
        self.reply_bar.append(icon_button("window-close-symbolic", "Cancel reply", lambda: self.set_reply(None)))
        self.reply_bar.set_visible(False)
        outer.append(self.reply_bar)

        row = Gtk.Box(spacing=4)
        self.send_btn = Gtk.Button(icon_name="mail-send-symbolic", tooltip_text="Send", valign=Gtk.Align.END)
        self.send_btn.add_css_class("send")
        self.send_btn.connect("clicked", lambda *_: self.send())
        self.rec = None
        if not self.is_sms:  # empty text: the button is a mic (hold = record, slide right = cancel)
            hold = Gtk.GestureDrag(propagation_phase=Gtk.PropagationPhase.CAPTURE)
            hold.connect("drag-begin", self._rec_begin)
            hold.connect("drag-update", self._rec_update)
            hold.connect("drag-end", self._rec_end)
            self.send_btn.add_controller(hold)
        row.append(self.send_btn)
        self.tv = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False,
                               input_hints=Gtk.InputHints.SPELLCHECK | Gtk.InputHints.EMOJI | Gtk.InputHints.WORD_COMPLETION,
                               input_purpose=Gtk.InputPurpose.FREE_FORM)
        self.tv.add_css_class("entry")
        self.tv.set_top_margin(10)
        self.tv.set_bottom_margin(10)
        self.tv.set_left_margin(10)
        self.tv.set_right_margin(10)
        sw = Gtk.ScrolledWindow(child=self.tv, hexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                propagate_natural_height=True, max_content_height=200)
        sw.add_css_class("entry-wrap")
        row.append(sw)
        self.placeholder = label("message" if not self.is_sms else "text message", "placeholder")
        self.placeholder.set_can_target(False)
        self.tv.get_buffer().connect("changed", self._typed)
        lp = Gtk.GestureLongPress(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        lp.connect("pressed", self._edit_menu)  # GTK's own touch menu doesn't show reliably
        self.tv.add_controller(lp)
        ov = Gtk.Overlay(child=row)
        ph = Gtk.Box(margin_start=72, valign=Gtk.Align.CENTER, can_target=False)
        ph.append(self.placeholder)
        ov.add_overlay(ph)
        self.rec_bar = Gtk.Box(spacing=12, margin_start=60, can_target=False)
        self.rec_bar.add_css_class("rec-bar")
        self.rec_dot = label("●", "rec-dot", ellipsize=False)
        self.rec_time = label("0:00", "rec-time", ellipsize=False)
        self.rec_hint = label("release to send · slide → to cancel", "rec-hint")
        self.rec_hint.set_hexpand(True)
        for w in (self.rec_dot, self.rec_time, self.rec_hint):
            self.rec_bar.append(w)
        self.rec_bar.set_visible(False)
        ov.add_overlay(self.rec_bar)
        if not self.is_sms:
            row.append(icon_button("emoji-symbols-symbolic", "GIF", self.gif_sheet, "tool gif-btn"))
            row.append(icon_button("image-x-generic-symbolic", "Photos and files", self.attach_sheet, "tool"))
        outer.append(ov)
        self._typed()
        return outer

    def _edit_menu(self, g, x, y):
        g.set_state(Gtk.EventSequenceState.CLAIMED)
        buzz()
        buf = self.tv.get_buffer()
        cb = Gdk.Display.get_default().get_clipboard()
        pop = Gtk.Popover(has_arrow=True)
        pop.set_parent(self.tv)
        r = Gdk.Rectangle()
        r.x, r.y, r.width, r.height = int(x), int(y), 1, 1
        pop.set_pointing_to(r)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("menu-box")

        def item(text, fn):
            b = text_button(text, lambda: (pop.popdown(), fn()), "flat menu-item")
            b.get_child().set_xalign(0)
            box.append(b)
        item("⎘  paste", self._paste)
        if buf.get_has_selection():
            item("✂  cut", lambda: buf.cut_clipboard(cb, True))
            item("⧉  copy", lambda: buf.copy_clipboard(cb))
        if buf.get_char_count():
            item("▤  select all", lambda: (buf.select_range(buf.get_start_iter(), buf.get_end_iter()),
                                           self.tv.grab_focus()))
        pop.set_child(box)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def _paste(self):
        cb = Gdk.Display.get_default().get_clipboard()
        fm = cb.get_formats()
        if not self.is_sms and fm.contain_gtype(Gdk.Texture) and not fm.contain_gtype(GObject.TYPE_STRING):
            def got(c, res):
                try:
                    tex = c.read_texture_finish(res)
                except GLib.Error as e:
                    return self.toast("couldn't paste: %s" % e.message)
                d = os.path.join(CACHE, "paste")
                os.makedirs(d, exist_ok=True)
                path = os.path.join(d, "pasted-%d.png" % int(time.time()))
                tex.save_to_png(path)
                dlg = Adw.AlertDialog(heading="Send pasted image?")
                dlg.add_response("no", "Cancel")
                dlg.add_response("send", "Send")
                dlg.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)
                dlg.connect("response", lambda _d, r: r == "send" and self.send_files([path]))
                dlg.present(self.win)
            return cb.read_texture_async(None, got)

        def got_text(c, res):
            try:
                t = c.read_text_finish(res)
            except GLib.Error:
                t = None
            if not t:
                return self.toast("clipboard is empty")
            buf = self.tv.get_buffer()
            buf.delete_selection(True, True)
            buf.insert_at_cursor(t)
            self.tv.grab_focus()
        cb.read_text_async(None, got_text)

    def _typed(self, *_):
        empty = self.tv.get_buffer().get_char_count() == 0
        self.placeholder.set_visible(empty)
        mic = empty and not self.is_sms
        self.send_btn.set_icon_name("audio-input-microphone-symbolic" if mic else "mail-send-symbolic")
        self.send_btn.set_tooltip_text("Hold to record a voice message" if mic else "Send")
        self.send_btn.set_sensitive(mic or not empty)

    # --- voice messages ---
    def _mic_mode(self):
        return not self.is_sms and self.tv.get_buffer().get_char_count() == 0

    def _rec_begin(self, g, x, y):
        if not self._mic_mode():
            g.set_state(Gtk.EventSequenceState.DENIED)
            return
        g.set_state(Gtk.EventSequenceState.CLAIMED)
        try:
            self.rec = VoiceRecorder()
        except OSError as e:
            self.toast("can't record: %s" % e)
            return
        GLib.timeout_add(550, lambda: (self.rec and buzz(), False)[1])  # buzz once the mic is live
        self.rec_cancel = False
        self.rec_bar.set_visible(True)
        self.rec_bar.remove_css_class("cancel")
        self.rec_hint.set_label("release to send · slide → to cancel")
        self.send_btn.add_css_class("recording")
        self._rec_tick()
        self.rec_timer = GLib.timeout_add(250, self._rec_tick)

    def _rec_tick(self):
        if not self.rec:
            return False
        t = int(self.rec.elapsed())
        self.rec_time.set_label("%d:%02d" % divmod(t, 60))
        self.rec_dot.set_opacity(1.0 if int(self.rec.elapsed() * 2) % 2 == 0 else 0.25)
        if t >= 600:  # 10 minutes is plenty
            self._rec_done(False)
            return False
        return True

    def _rec_update(self, g, dx, dy):
        if not self.rec:
            return
        cancel = dx > VOICE_CANCEL_PX
        if cancel != self.rec_cancel:
            self.rec_cancel = cancel
            (self.rec_bar.add_css_class if cancel else self.rec_bar.remove_css_class)("cancel")
            self.rec_hint.set_label("release to cancel ✕" if cancel else "release to send · slide → to cancel")
            buzz()

    def _rec_end(self, g, dx, dy):
        if self.rec:
            self._rec_done(self.rec_cancel)

    def _rec_done(self, cancel):
        rec, self.rec = self.rec, None
        if getattr(self, "rec_timer", 0):
            GLib.source_remove(self.rec_timer)
            self.rec_timer = 0
        self.rec_bar.set_visible(False)
        self.send_btn.remove_css_class("recording")
        if cancel or rec.elapsed() < VOICE_MIN_S:
            rec.cancel()
            if not cancel:
                self.toast("hold the mic to record")
            return
        rec.stop()
        buzz()
        reply = self.reply_to["id"] if self.reply_to else None
        self.set_reply(None)
        self.stick = True

        def go():
            ogg, dur, wave_ = rec.finish()
            return self.app.matrix.send_voice(self.cid, ogg, dur, wave_, reply)
        bg(go, None, lambda e: self.toast("voice message not sent: %s" % e))

    def set_reply(self, msg):
        self.reply_to = msg
        self.reply_bar.set_visible(bool(msg))
        if msg:
            self.reply_text.set_label("%s: %s" % (self.sender_name(msg), msg_summary(msg)))
            self.tv.grab_focus()

    def send(self):
        buf = self.tv.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        if not text:
            return
        buf.set_text("")
        reply = self.reply_to["id"] if self.reply_to and not self.is_sms else None
        self.set_reply(None)
        self.stick = True
        if self.is_sms:
            self.app.sms.send(self.cid, text)
        else:
            bg(lambda: self.app.matrix.send_text(self.cid, text, reply), None,
               lambda e: self.toast("not sent: %s" % e))

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    # --- messages ---
    def sender_name(self, m):
        if m["mine"]:
            return "you"
        if self.is_sms:
            return self.chat["name"]
        mem = self.app.store.member(self.cid, m["sender"])
        return mem["name"] if mem else m["sender"][1:].split(":")[0]

    def load(self):
        st = self.app.store
        msgs = st.messages(self.cid, limit=self.limit)
        msgs = [m for m in msgs if m["kind"] != "reaction"]
        reacts = {}
        if msgs:
            for r in st.q("SELECT body, sender, reply_to, mine FROM messages WHERE chat=? AND kind='reaction' "
                          "AND ts>=?", (self.cid, msgs[0]["ts"])):
                reacts.setdefault(r["reply_to"], {}).setdefault(r["body"], []).append(r["mine"])
        seen = self.app.store.chat(self.cid)["seen_ts"] or 0
        want = []
        prev = None
        for m in msgs:
            newday = not prev or day_title(prev["ts"]) != day_title(m["ts"])
            grouped = (prev and not newday and prev["sender"] == m["sender"] and m["ts"] - prev["ts"] < 300000)
            rx = reacts.get(m["id"], {})
            sig = (m["kind"], m["body"], m["status"], m["edited"], m["media"], newday, bool(grouped),
                   json.dumps(rx, sort_keys=True), m["mine"] and seen >= m["ts"])
            want.append((m["id"], sig, m, newday, grouped, rx, seen))
            prev = m
        if len(want) < self.limit and not self.is_sms:
            self.no_more = not (self.app.store.chat(self.cid) or {}).get("prev_batch")
        # diff against what's shown
        keep = {w[0] for w in want}
        for i in range(len(self.rows) - 1, -1, -1):
            if self.rows[i][0] not in keep:
                self.lb.remove(self.rows[i][2])
                del self.rows[i]
        for idx, (mid, sig, m, newday, grouped, rx, seen_ts) in enumerate(want):
            if idx < len(self.rows) and self.rows[idx][0] == mid:
                if self.rows[idx][1] != sig:
                    self.lb.remove(self.rows[idx][2])
                    row = self.message_row(m, newday, grouped, rx, seen_ts)
                    self.lb.insert(row, idx)
                    self.rows[idx] = (mid, sig, row)
                continue
            row = self.message_row(m, newday, grouped, rx, seen_ts)
            self.lb.insert(row, idx)
            self.rows.insert(idx, (mid, sig, row))
        if self.stick:
            GLib.idle_add(self.to_bottom)

    def to_bottom(self):
        self.vadj.set_value(self.vadj.get_upper())
        return False

    def _scrolled(self, adj):
        self.stick = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 120

    def _grew(self, adj, _p):
        if self.stick:
            adj.set_value(adj.get_upper())
        elif getattr(self, "anchor", None) is not None:  # older messages added on top: stay put
            adj.set_value(adj.get_upper() - self.anchor)
            self.anchor = None

    def _edge(self, sw, pos):
        if pos != Gtk.PositionType.TOP or self.loading_older or self.no_more:
            return
        self.loading_older = True
        self.anchor = self.vadj.get_upper() - self.vadj.get_value()
        have = len([1 for r in self.rows])
        local = self.app.store.q("SELECT COUNT(*) n FROM messages WHERE chat=? AND kind!='reaction'",
                                 (self.cid,))[0]["n"]
        if local > have:
            self.limit += PAGE
            self.load()
            self.loading_older = False
            return
        if self.is_sms:
            self.no_more = True
            self.loading_older = False
            return

        def done(n):
            self.loading_older = False
            if n == -1:
                self.no_more = True
            self.limit += PAGE
            self.load()
        bg(lambda: self.app.matrix.backfill(self.cid), done,
           lambda e: setattr(self, "loading_older", False))

    def message_row(self, m, newday, grouped, rx, seen_ts):
        row = Gtk.ListBoxRow(activatable=False)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        if newday:
            outer.append(label(day_title(m["ts"]), "day", xalign=0.5))
        mine = bool(m["mine"])
        if m["kind"] == "notice" and not mine:
            n = label(m["body"], "notice", xalign=0.5, ellipsize=False, wrap=True)
            outer.append(n)
            row.set_child(outer)
            return row
        line = Gtk.Box(halign=Gtk.Align.END if mine else Gtk.Align.START)
        line.add_css_class("line")
        if grouped:
            line.add_css_class("grouped")
        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bubble.add_css_class("bubble")
        bubble.add_css_class("mine" if mine else "theirs")
        net = self.chat["net"] or "mx"
        bubble.add_css_class("net-" + net)
        big_emoji = m["kind"] == "text" and only_emoji(m["body"] or "")
        if big_emoji:
            bubble.add_css_class("emoji-only")
        if not mine and not self.chat["dm"] and not grouped:
            bubble.append(label(self.sender_name(m), "sender av-%d" % (zlen(m["sender"]) % 6)))
        if m["reply_to"]:
            q = self.app.store.message(self.cid, m["reply_to"])
            qb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            qb.add_css_class("quote")
            qb.append(label(self.sender_name(q) if q else "…", "quote-name"))
            qb.append(label(msg_summary(q) if q else "earlier message", "quote-text"))
            bubble.append(qb)
        bubble.append(self.body_widget(m, big_emoji))
        foot = Gtk.Box(spacing=5, halign=Gtk.Align.END)
        if m["edited"]:
            foot.append(label("edited", "foot"))
        foot.append(label(datetime.datetime.fromtimestamp(m["ts"] / 1000).strftime("%H:%M"), "foot",
                          ellipsize=False))
        if mine:
            st = m["status"]
            tick = {"sending": "◷", "failed": "! not sent"}.get(st, "✓✓" if seen_ts >= m["ts"] else "✓")
            t = label(tick, "foot tick" + (" seen" if tick == "✓✓" else "") + (" failed" if st == "failed" else ""),
                      ellipsize=False)
            foot.append(t)
        bubble.append(foot)
        bubble.set_halign(Gtk.Align.END if mine else Gtk.Align.START)
        # the clamp caps bubble width; the label wraps at the real text width (max_width_chars guessed wrong)
        line.append(Adw.Clamp(maximum_size=BUBBLE_MAX, tightening_threshold=BUBBLE_MAX, child=bubble))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.END if mine else Gtk.Align.START)
        col.append(line)
        if rx:
            rb = Gtk.Box(spacing=4, halign=Gtk.Align.END if mine else Gtk.Align.START)
            rb.add_css_class("reactions")
            for key, who in rx.items():
                chip = Gtk.Button(label=key + (" %d" % len(who) if len(who) > 1 else ""))
                chip.add_css_class("reaction")
                if any(who):
                    chip.add_css_class("mine")
                chip.connect("clicked", lambda _b, k=key: self.react(m, k))
                rb.append(chip)
            col.append(rb)
        lp = Gtk.GestureLongPress()
        lp.connect("pressed", lambda g, x, y: self.msg_menu(m, bubble))
        bubble.add_controller(lp)
        if m["status"] == "failed" and m["kind"] == "text":
            tap = Gtk.GestureClick()
            tap.connect("released", lambda *_: self.retry(m))
            bubble.add_controller(tap)
        if self.is_sms:
            outer.append(col)
        else:
            slide = ReplySlide(col)
            slide.connect("reply", lambda *_: self.set_reply(m))
            outer.append(slide)
        row.set_child(outer)
        return row

    def body_widget(self, m, big_emoji):
        k = m["kind"]
        media = json.loads(m["media"]) if m["media"] else None
        if k in ("text", "emote"):
            t = (m["body"] or "").strip()
            if k == "emote":
                t = "* " + t
            lb = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                           use_markup=True, selectable=False)
            lb.set_markup(linkify(t))
            lb.add_css_class("emoji-big" if big_emoji else "text")
            lb.connect("activate-link", lambda _l, uri: (open_uri(uri, self.win), True)[1])
            return lb
        if k == "deleted":
            return label("message deleted", "deleted", ellipsize=False)
        if k in ("image", "sticker", "gif", "video") and media:
            return self.media_widget(m, media)
        if k == "audio" and media:
            return self.audio_widget(media)
        if k == "file" and media:
            b = Gtk.Button()
            b.add_css_class("file-btn")
            bb = Gtk.Box(spacing=8)
            bb.append(Gtk.Image(icon_name="text-x-generic-symbolic"))
            name = media.get("name") or "file"
            size = media.get("size")
            bb.append(label(name + ("  · %s" % human(size) if size else ""), "file-name"))
            b.set_child(bb)
            b.connect("clicked", lambda *_: self.open_file(media))
            return b
        if k == "location":
            g = (m["body"] or "").replace("geo:", "").split(";")[0]
            b = text_button("📍 " + g, lambda: self.open_location(g, m), "flat file-btn")
            return b
        return label(m["body"] or k, "text", ellipsize=False, wrap=True)

    def open_location(self, g, m):
        """Open a shared location in notarealMaps (pin + place card, ready for directions)."""
        name = "Location from " + self.sender_name(m).replace(",", " ")
        try:
            subprocess.Popen([MAPS, "--place", "%s,%s" % (g, name)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            open_uri("https://maps.google.com/?q=" + g, self.win)

    def media_widget(self, m, media):
        w, h = media.get("w") or 300, media.get("h") or 300
        maxw, maxh = 250, 320
        if m["kind"] == "sticker":
            maxw = maxh = 150
        s = min(maxw / w, maxh / h, 1.6)
        W, H = max(80, int(w * s)), max(60, int(h * s))
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        pic.set_size_request(W, H)
        pic.add_css_class("media")
        frame = Gtk.Overlay(child=pic)
        frame.set_size_request(W, H)
        local = media.get("local")
        kind = m["kind"]
        if kind == "gif":
            badge = label("GIF", "media-badge", ellipsize=False)
            badge.set_halign(Gtk.Align.START)
            badge.set_valign(Gtk.Align.START)
            frame.add_overlay(badge)

            def show_gif(path):
                if not path:
                    return
                if (media.get("mime") or "").startswith("video/") or path.endswith(".mp4"):
                    mf = Gtk.MediaFile.new_for_filename(path)
                    mf.set_loop(True)
                    mf.set_muted(True)
                    mf.play()
                    pic.set_paintable(mf)
                else:
                    play_gif(pic, path)
            if local and os.path.exists(local):
                show_gif(local)
            else:
                bg(lambda: self.app.matrix.fetch(media["url"]), show_gif)
        else:
            if kind == "video":
                play = label("▶", "play-badge", xalign=0.5, ellipsize=False)
                play.set_halign(Gtk.Align.CENTER)
                play.set_valign(Gtk.Align.CENTER)
                frame.add_overlay(play)
            src = local if local and kind != "video" and os.path.exists(local) else None
            if src:
                pic.set_paintable(texture(src))
            else:
                thumb = media.get("thumb") if kind == "video" else media.get("url")
                if thumb:
                    bg(lambda: self.app.matrix.fetch(thumb, 640, 640), lambda p: pic.set_paintable(texture(p)))
        tap = Gtk.GestureClick()
        tap.connect("released", lambda *_: self.win.view_media(self, m, media))
        frame.add_controller(tap)
        return frame

    def audio_widget(self, media):
        b = Gtk.Button()
        b.add_css_class("audio-btn")
        bb = Gtk.Box(spacing=10)
        icon = Gtk.Image(icon_name="media-playback-start-symbolic")
        bb.append(icon)
        dur = media.get("dur")
        bb.append(label("voice message" + ("  %d:%02d" % divmod(int(dur / 1000), 60) if dur else ""), "file-name"))
        b.set_child(bb)

        def toggle(*_):
            if getattr(b, "mf", None):
                if b.mf.get_playing():
                    b.mf.pause()
                else:
                    b.mf.play()
                return

            def got(path):
                b.mf = Gtk.MediaFile.new_for_filename(path)
                b.mf.connect("notify::playing", lambda mf, _p: icon.set_from_icon_name(
                    "media-playback-pause-symbolic" if mf.get_playing() else "media-playback-start-symbolic"))
                b.mf.play()
            local = media.get("local")
            if local and os.path.exists(local):
                return got(local)
            bg(lambda: self.app.matrix.fetch(media["url"]), got)
        b.connect("clicked", toggle)
        return b

    def open_file(self, media):
        def got(path):
            dst_dir = os.path.expanduser("~/Downloads")
            os.makedirs(dst_dir, exist_ok=True)
            dst = os.path.join(dst_dir, os.path.basename(media.get("name") or "file"))
            if not os.path.exists(dst):
                with open(path, "rb") as a, open(dst, "wb") as b:
                    b.write(a.read())
            self.toast("saved to Downloads")
            Gtk.FileLauncher.new(Gio.File.new_for_path(dst)).launch(self.win, None, None, None)
        bg(lambda: self.app.matrix.fetch(media["url"]), got, lambda e: self.toast("download failed"))

    def retry(self, m):
        self.app.store.x("DELETE FROM messages WHERE chat=? AND id=?", (self.cid, m["id"]))
        self.load()
        self.tv.get_buffer().set_text(m["body"] or "")
        self.send()

    def react(self, m, key):
        if self.is_sms or m["id"].startswith("~"):
            return
        mine = self.app.store.q("SELECT id FROM messages WHERE chat=? AND kind='reaction' AND reply_to=? "
                                "AND body=? AND mine=1", (self.cid, m["id"], key))
        if mine:  # tapping your own reaction again takes it back
            bg(lambda: self.app.matrix.redact(self.cid, mine[0]["id"]))
        else:
            bg(lambda: self.app.matrix.react(self.cid, m["id"], key), None,
               lambda e: self.toast("couldn't react"))

    def msg_menu(self, m, widget):
        buzz()
        pop = Gtk.Popover()
        pop.set_parent(widget)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("menu-box")
        if not self.is_sms:
            rx = Gtk.Box(spacing=2)
            for e in QUICK_REACTIONS:
                b = text_button(e, lambda e=e: (pop.popdown(), self.react(m, e)), "flat react-pick")
                rx.append(b)
            box.append(rx)

        def item(text, cb):
            b = text_button(text, lambda: (pop.popdown(), cb()), "flat menu-item")
            b.get_child().set_xalign(0)
            box.append(b)
        if not self.is_sms:
            item("↩  reply", lambda: self.set_reply(m))
        if m["body"] and m["kind"] in ("text", "emote", "notice"):
            item("⧉  copy", lambda: (Gdk.Display.get_default().get_clipboard().set(m["body"]),
                                      self.toast("copied")))
        if m["mine"] and not self.is_sms and m["kind"] != "deleted" and not m["id"].startswith("~"):
            item("✕  delete for everyone", lambda: bg(lambda: self.app.matrix.redact(self.cid, m["id"])))
        if self.is_sms:
            item("✕  delete", lambda: (self.app.store.x("DELETE FROM messages WHERE chat=? AND id=?",
                                                         (self.cid, m["id"])), self.load()))
        pop.set_child(box)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    # --- sheets ---
    def attach_sheet(self):
        AttachSheet(self).present(self.win)

    def gif_sheet(self):
        GifSheet(self).present(self.win)

    def send_files(self, paths):
        reply = self.reply_to["id"] if self.reply_to else None
        self.set_reply(None)
        self.stick = True
        for p in paths:
            bg(lambda p=p: send_path(self.app.matrix, self.cid, p, reply), None,
               lambda e: self.toast("not sent: %s" % e))

    def send_gif(self, g):
        reply = self.reply_to["id"] if self.reply_to else None
        self.set_reply(None)
        self.stick = True
        gifs.remember(g)

        def go():
            import requests
            path = os.path.join(CACHE, "gifs", hashlib.sha1(g["mp4"].encode()).hexdigest() + ".mp4")
            if not os.path.exists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                r = requests.get(g["mp4"], timeout=60)
                r.raise_for_status()
                with open(path, "wb") as f:
                    f.write(r.content)
            info = {"w": g.get("w") or 0, "h": g.get("h") or 0}
            return self.app.matrix.send_media(self.cid, path, "gif", "video/mp4", info, "gif.mp4", reply)
        bg(go, None, lambda e: self.toast("GIF not sent: %s" % e))


def msg_summary(m):
    if not m:
        return ""
    from store import preview
    return preview(m)


def zlen(s):
    return sum(map(ord, s or ""))


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%d %s" % (n, unit)
        n /= 1024
    return "%.1f TB" % n


MIC = "alsa_input.platform-sound.HiFi__Mic__source"   # same source voiced records from
VOICE_MIN_S = 0.7
VOICE_CANCEL_PX = 110


class VoiceRecorder:
    """parecord -> WAV while the mic button is held; finish() -> Ogg/Opus + duration + waveform."""
    def __init__(self):
        d = os.path.join(CACHE, "voice")
        os.makedirs(d, exist_ok=True)
        self.wav = os.path.join(d, "rec-%d.wav" % int(time.time() * 1000))
        self.t0 = time.monotonic()
        self.proc = subprocess.Popen(["parecord", "--device=" + MIC, "--rate=48000", "--channels=1",
                                      "--format=s16le", "--file-format=wav", self.wav],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def elapsed(self):
        return time.monotonic() - self.t0

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(2)  # SIGINT: parecord finishes the WAV header
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def cancel(self):
        self.stop()
        try:
            os.unlink(self.wav)
        except OSError:
            pass

    def finish(self):
        """(ogg path, duration ms, waveform 0..1024 x 64). Runs in a worker thread."""
        import array
        import wave
        with wave.open(self.wav) as w:
            rate, frames = w.getframerate(), w.readframes(w.getnframes())
        samples = array.array("h", frames)
        dur = int(len(samples) * 1000 / rate)
        n = 64
        step = max(1, len(samples) // n)
        peaks = []
        for i in range(n):
            chunk = samples[i * step:(i + 1) * step]
            peaks.append((sum(x * x for x in chunk[::8]) / max(1, len(chunk[::8]))) ** 0.5 if chunk else 0)
        top = max(peaks) or 1
        wave_ = [int(p / top * 1024) for p in peaks]
        ogg = self.wav[:-4] + ".ogg"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", self.wav, "-ac", "1", "-c:a", "libopus",
                        "-b:a", "32k", "-application", "voip", ogg], check=True, timeout=60)
        os.unlink(self.wav)
        return ogg, dur, wave_


def play_gif(pic, path):
    """Animate a .gif file in a Gtk.Picture (GTK4 doesn't by itself)."""
    try:
        anim = GdkPixbuf.PixbufAnimation.new_from_file(path)
    except GLib.Error:
        return
    if anim.is_static_image():
        pic.set_paintable(Gdk.Texture.new_for_pixbuf(anim.get_static_image()))
        return
    it = anim.get_iter(None)

    def tick():
        if not pic.get_root():
            return False
        it.advance(None)
        pic.set_paintable(Gdk.Texture.new_for_pixbuf(it.get_pixbuf()))
        GLib.timeout_add(max(20, it.get_delay_time()), tick)
        return False
    pic.set_paintable(Gdk.Texture.new_for_pixbuf(it.get_pixbuf()))
    GLib.timeout_add(max(20, it.get_delay_time()), tick)


def prepare_image(path):
    """Big photos are scaled to 2048 px (EXIF-rotated) JPEGs before upload. -> (path, mime, w, h)."""
    pb = GdkPixbuf.Pixbuf.new_from_file(path)
    pb = pb.apply_embedded_orientation() or pb
    w, h = pb.get_width(), pb.get_height()
    is_gif = path.lower().endswith(".gif")
    if is_gif or (max(w, h) <= 2048 and os.path.getsize(path) < 1_500_000):
        mime = Gio.content_type_guess(path, None)[0]
        return path, mime, w, h
    s = 2048 / max(w, h)
    pb = pb.scale_simple(int(w * s), int(h * s), GdkPixbuf.InterpType.BILINEAR)
    out = os.path.join(CACHE, "upload", hashlib.sha1(path.encode()).hexdigest() + ".jpg")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    pb.savev(out, "jpeg", ["quality"], ["88"])
    return out, "image/jpeg", pb.get_width(), pb.get_height()


def send_path(matrix, cid, path, reply=None):
    mime = Gio.content_type_guess(path, None)[0] or "application/octet-stream"
    name = os.path.basename(path)
    if mime.startswith("image/"):
        p, mime, w, h = prepare_image(path)
        kind = "gif" if mime == "image/gif" else "image"
        return matrix.send_media(cid, p, kind, mime, {"w": w, "h": h}, name if p == path else
                                 os.path.splitext(name)[0] + ".jpg", reply)
    if mime.startswith("video/"):
        return matrix.send_media(cid, path, "video", mime, {}, name, reply)
    if mime.startswith("audio/"):
        return matrix.send_media(cid, path, "audio", mime, {}, name, reply)
    return matrix.send_media(cid, path, "file", mime, {}, name, reply)


# --- photo picker -------------------------------------------------------------------------

class AttachSheet(Adw.Dialog):
    def __init__(self, page):
        super().__init__(title="Send")
        fit_sheet(self, page.win)
        self.set_presentation_mode(Adw.DialogPresentationMode.BOTTOM_SHEET)
        self.page = page
        self.picked = []
        self.marks = {}
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        self.send_btn = text_button("SEND", self._send, "send-text")
        self.send_btn.set_sensitive(False)
        hb.pack_start(self.send_btn)
        hb.set_title_widget(label("PHOTOS", "sheet-title", xalign=0.5))
        hb.pack_end(icon_button("window-close-symbolic", "Close", self.close))
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        files = text_button("📁  BROWSE FILES…", self._browse, "flat browse")
        files.get_child().set_xalign(0)
        box.append(files)
        self.grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                min_children_per_line=3, max_children_per_line=3, row_spacing=3,
                                column_spacing=3)
        self.grid.add_css_class("photo-grid")
        box.append(Gtk.ScrolledWindow(child=self.grid, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        tv.set_content(box)
        self.set_child(tv)
        bg(recent_media, self._fill)

    def _fill(self, paths):
        for p in paths:
            pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
            pic.set_size_request(-1, 150)
            ov = Gtk.Overlay(child=pic)
            mark = label("", "pick-mark", xalign=0.5, ellipsize=False)
            mark.set_halign(Gtk.Align.START)
            mark.set_valign(Gtk.Align.START)
            ov.add_overlay(mark)
            if p.lower().endswith((".mp4", ".webm", ".mkv", ".mov")):
                v = label("▶", "play-badge small", xalign=0.5, ellipsize=False)
                v.set_halign(Gtk.Align.CENTER)
                v.set_valign(Gtk.Align.CENTER)
                ov.add_overlay(v)
            tap = Gtk.GestureClick()
            tap.connect("released", lambda *_a, p=p, o=ov, mk=mark: self._toggle(p, o, mk))
            ov.add_controller(tap)
            self.grid.append(ov)
            bg(lambda p=p: thumb_for(p), lambda t, pic=pic: pic.set_paintable(t) if t else None)

    def _toggle(self, path, ov, mark):
        self.marks[path] = (ov, mark)
        if path in self.picked:
            self.picked.remove(path)
        else:
            self.picked.append(path)
        for p, (o, mk) in self.marks.items():  # numbers show the send order
            on = p in self.picked
            (o.add_css_class if on else o.remove_css_class)("picked")
            mk.set_label(str(self.picked.index(p) + 1) if on else "")
        self.send_btn.set_label("SEND %d" % len(self.picked) if self.picked else "SEND")
        self.send_btn.set_sensitive(bool(self.picked))

    def _send(self):
        paths = list(self.picked)
        self.close()
        self.page.send_files(paths)

    def _browse(self):
        dlg = Gtk.FileDialog(title="Send files")

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
            self.close()
            self.page.send_files([p for p in paths if p])
        dlg.open_multiple(self.page.win, None, done)


def fit_sheet(dlg, win):
    """Sheets span the window's width and most of its height (fixed sizes overflowed the phone)."""
    w, h = win.get_width(), win.get_height()
    if w > 0 and h > 0:
        dlg.set_content_width(w)
        dlg.set_content_height(int(h * 0.82))


def recent_media(limit=45):
    out = []
    for d in PHOTO_DIRS:
        d = os.path.expanduser(d)
        try:
            for n in os.listdir(d):
                if n.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm", ".mkv")):
                    p = os.path.join(d, n)
                    if os.path.isfile(p):
                        out.append((os.path.getmtime(p), p))
        except OSError:
            continue
    out = sorted(set(out), reverse=True)
    return [p for _, p in out[:limit]]


def thumb_for(path):
    """Small texture for the picker (videos via ffmpeg's first frame)."""
    key = os.path.join(CACHE, "thumbs", hashlib.sha1(("%s%s" % (path, os.path.getmtime(path))).encode()).hexdigest() + ".jpg")
    if not os.path.exists(key):
        os.makedirs(os.path.dirname(key), exist_ok=True)
        if path.lower().endswith((".mp4", ".webm", ".mkv", ".mov")):
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", path, "-frames:v", "1", "-vf",
                            "scale=300:-1", key], timeout=20)
        else:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 300, 300, True)
            pb = pb.apply_embedded_orientation() or pb
            pb.savev(key, "jpeg", ["quality"], ["80"])
    return Gdk.Texture.new_from_filename(key) if os.path.exists(key) else None


# --- GIF picker ------------------------------------------------------------------------------

class GifSheet(Adw.Dialog):
    def __init__(self, page):
        super().__init__(title="GIF")
        fit_sheet(self, page.win)
        self.set_presentation_mode(Adw.DialogPresentationMode.BOTTOM_SHEET)
        self.page = page
        self.gen = 0
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        hb.set_title_widget(label("GIF", "sheet-title", xalign=0.5))
        hb.pack_end(icon_button("window-close-symbolic", "Close", self.close))
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.search = Gtk.SearchEntry(placeholder_text="search GIFs", margin_start=12, margin_end=12)
        self.search.add_css_class("find")
        self.search.connect("search-changed", lambda *_: self.run())
        box.append(self.search)
        self.note = label("", "gif-note", xalign=0.5, ellipsize=False, wrap=True)
        box.append(self.note)
        self.grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                min_children_per_line=2, max_children_per_line=2, row_spacing=4,
                                column_spacing=4)
        self.grid.add_css_class("gif-grid")
        box.append(Gtk.ScrolledWindow(child=self.grid, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        tv.set_content(box)
        self.set_child(tv)
        self.run()

    def run(self):
        self.gen += 1
        gen = self.gen
        q = self.search.get_text().strip()
        self.grid.remove_all()
        if not gifs.available():
            self.note.set_label("GIF search needs a Tenor or GIPHY key in\n~/.config/notarealchat/config.json")
            self._show(gifs.recent(), gen)
            return
        if not q and gifs.recent():
            self.note.set_label("RECENT")
            self._show(gifs.recent(), gen)
            return
        self.note.set_label("…")
        GLib.timeout_add(350 if q else 0, lambda: (self._fetch(q, gen), False)[1])

    def _fetch(self, q, gen):
        if gen != self.gen:
            return
        bg(lambda: gifs.search(q), lambda res: (self.note.set_label("" if res else "nothing found"),
                                                 self._show(res, gen)),
           lambda e: self.note.set_label("GIF search failed: %s" % e))

    def _show(self, items, gen):
        if gen != self.gen:
            return
        for g in items:
            pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
            pic.set_size_request(-1, 150)
            pic.add_css_class("gif-cell")
            tap = Gtk.GestureClick()
            tap.connect("released", lambda *_a, g=g: (self.close(), self.page.send_gif(g)))
            pic.add_controller(tap)
            self.grid.append(pic)
            bg(lambda g=g: fetch_url(g["preview"]), lambda p, pic=pic: pic.set_paintable(texture(p)))


def fetch_url(url):
    import requests
    path = os.path.join(CACHE, "web", hashlib.sha1(url.encode()).hexdigest())
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    return path


# --- media viewer ------------------------------------------------------------------------------

class ZoomPic(Gtk.Widget):
    """10-17: a photo you can pinch / double-tap to zoom and drag around; at normal size a swipe
    down closes it. Same behaviour as Gallery's ZoomView (~/gallery/gallery.py)."""
    def __init__(self, tex, on_close):
        super().__init__()
        self.tex, self.on_close = tex, on_close
        self.scale, self.off = 1.0, [0.0, 0.0]
        self.pull = 0.0                  # finger offset while pulling down to close at scale 1
        self.zooming = False
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.set_hexpand(True)
        self.set_vexpand(True)
        z = Gtk.GestureZoom()
        z.connect("begin", self._zoom_begin)
        z.connect("scale-changed", lambda _g, k: self._zoom_to(self.z0 * k, *self.zc))
        z.connect("end", self._zoom_end)
        self.add_controller(z)
        d = Gtk.GestureDrag()
        d.connect("drag-begin", lambda *_: setattr(self, "d0", list(self.off)))
        d.connect("drag-update", self._drag_update)
        d.connect("drag-end", self._drag_end)
        self.add_controller(d)
        c = Gtk.GestureClick()
        c.connect("released", lambda _g, n, x, y: n == 2 and self._zoom_to(1.0 if self.scale > 1.01 else 2.5, x, y))
        self.add_controller(c)

    def _fit(self):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return 0, 0
        tw, th = self.tex.get_width(), self.tex.get_height()
        k = min(w / tw, h / th)
        return tw * k, th * k

    def _clamp(self):
        fw, fh = self._fit()
        for i, (full, view) in enumerate(((fw * self.scale, self.get_width()), (fh * self.scale, self.get_height()))):
            m = max(0.0, (full - view) / 2)
            self.off[i] = max(-m, min(m, self.off[i]))

    def _zoom_to(self, s, fx, fy):
        """Scale to s keeping the screen point (fx, fy) still."""
        s = max(1.0, min(6.0, s))
        cx, cy = self.get_width() / 2, self.get_height() / 2
        r = s / self.scale
        self.off = [(fx - cx) - (fx - cx - self.off[0]) * r, (fy - cy) - (fy - cy - self.off[1]) * r]
        self.scale = s
        if s == 1.0:
            self.off = [0.0, 0.0]
        self._clamp()
        self.queue_draw()

    def _zoom_begin(self, g, _seq):
        self.zooming = True
        self.z0 = self.scale
        ok, x, y = g.get_bounding_box_center()
        self.zc = (x, y) if ok else (self.get_width() / 2, self.get_height() / 2)
        self.pull = 0.0

    def _zoom_end(self, *_):
        self.zooming = False
        if self.scale < 1.05:
            self._zoom_to(1.0, 0, 0)

    def _drag_update(self, _g, dx, dy):
        if self.zooming:
            return
        if self.scale > 1.01:
            self.off = [self.d0[0] + dx, self.d0[1] + dy]
            self._clamp()
        else:
            self.pull = max(0.0, dy) if dy > abs(dx) else 0.0
        self.queue_draw()

    def _drag_end(self, _g, dx, dy):
        if self.zooming or self.scale > 1.01:
            return
        self.pull = 0.0
        self.queue_draw()
        if dy > self.get_height() * 0.18 and dy > abs(dx):
            self.on_close()

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        fw, fh = self._fit()
        sw, sh = fw * self.scale, fh * self.scale
        x = w / 2 + self.off[0] - sw / 2
        y = h / 2 + self.off[1] + self.pull - sh / 2
        if self.pull > 0:            # shrink a little while pulling down to close
            k = max(0.7, 1 - self.pull / (h * 1.5))
            x, y, sw, sh = x + sw * (1 - k) / 2, y + sh * (1 - k) / 2, sw * k, sh * k
        snap.append_scaled_texture(self.tex, Gsk.ScalingFilter.TRILINEAR, Graphene.Rect().init(x, y, sw, sh))


class Viewer(Adw.NavigationPage):
    def __init__(self, win, page, m, media):
        super().__init__(title="Media")
        self.win, self.media, self.path = win, media, None
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        hb.add_css_class("vapor-bar")
        hb.pack_end(icon_button("document-save-symbolic", "Save", self.save))
        hb.pack_end(icon_button("send-to-symbolic", "Open with…", self.open_ext))
        hb.set_title_widget(label(page.sender_name(m), "head-name", xalign=0.5))
        tv.add_top_bar(hb)
        self.stack = Gtk.Stack()
        self.stack.add_named(Adw.Spinner(), "wait")
        tv.set_content(self.stack)
        self.set_child(tv)
        local = media.get("local")
        if local and os.path.exists(local):
            self._show(local, m["kind"])
        else:
            bg(lambda: win.app.matrix.fetch(media["url"]), lambda p: self._show(p, m["kind"]))

    def _show(self, path, kind):
        self.path = path
        mime = self.media.get("mime") or ""
        if kind in ("video", "gif") and (mime.startswith("video/") or kind == "video"):
            mf = Gtk.MediaFile.new_for_filename(path)
            if kind == "gif":
                mf.set_loop(True)
                mf.set_muted(True)
                w = Gtk.Picture(paintable=mf)
            else:
                w = Gtk.Video(media_stream=mf, autoplay=True)
            mf.play()
        else:
            if kind == "gif":
                w = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN)
                play_gif(w, path)
            else:
                tex = texture(path)
                w = ZoomPic(tex, self.win.nav.pop) if tex else Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN)
        self.stack.add_named(w, "media")
        self.stack.set_visible_child_name("media")

    def save(self):
        if not self.path:
            return
        d = os.path.expanduser("~/Pictures/notarealChat")
        os.makedirs(d, exist_ok=True)
        name = os.path.basename(self.media.get("name") or "media")
        if "." not in name:
            ext = (Gio.content_type_get_mime_type(self.media.get("mime") or "") or "").split("/")[-1]
            name += "." + {"jpeg": "jpg"}.get(ext, ext or "bin")
        dst = os.path.join(d, "%d-%s" % (int(time.time()), name))
        with open(self.path, "rb") as a, open(dst, "wb") as b:
            b.write(a.read())
        self.win.list_page.toasts.add_toast(Adw.Toast(title="saved to Pictures/notarealChat", timeout=3))

    def open_ext(self):
        if self.path:
            Gtk.FileLauncher.new(Gio.File.new_for_path(self.path)).launch(self.win, None, None, None)


# --- new chat ------------------------------------------------------------------------------------

class NewChat(Adw.NavigationPage):
    """Search contacts / chats / type a number -> WhatsApp or SMS."""
    def __init__(self, win):
        super().__init__(title="New chat")
        self.win, self.app = win, win.app
        self.busy = False
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        hb.add_css_class("vapor-bar")
        hb.set_title_widget(label("NEW CHAT", "app-title", xalign=0.5, ellipsize=False))
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.search = Gtk.SearchEntry(placeholder_text="name or number", margin_start=16, margin_end=16,
                                      margin_top=8, margin_bottom=6)
        self.search.add_css_class("find")
        self.search.connect("search-changed", lambda *_: self.fill())
        box.append(self.search)
        self.status = label("", "status", xalign=0.5)
        box.append(self.status)
        self.lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, activate_on_single_click=True)
        self.lb.add_css_class("people")
        self.lb.connect("row-activated", self._picked)
        box.append(Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.lb))
        tv.set_content(box)
        self.set_child(tv)
        self.fill()
        GLib.idle_add(lambda: (self.search.grab_focus(), False)[1])

    def fill(self):
        t = self.search.get_text().strip()
        self.lb.remove_all()
        if not t:
            self.lb.append(self._section("RECENT"))
            for c in self.app.store.chats()[:12]:
                self.lb.append(self._chat_row(c))
            return
        tl = t.lower()
        chats = [c for c in self.app.store.chats() if tl in (c["name"] or "").lower()][:6]
        if re.fullmatch(r"[+\d][\d\s\-()]{4,}", t):
            num = contacts.normalize(t)
            self.lb.append(self._number_row(self.app.book.name_for(num) or num, [[num, t]]))
        if chats:
            self.lb.append(self._section("CHATS"))
            for c in chats:
                self.lb.append(self._chat_row(c))
        people = self.app.book.search(t)
        if people:
            self.lb.append(self._section("CONTACTS"))
            for name, nums in people:
                self.lb.append(self._number_row(name, nums))

    def _section(self, text):
        row = Gtk.ListBoxRow(activatable=False)
        row.set_child(label(text, "section", xalign=0))
        return row

    def _chat_row(self, c):
        row = Gtk.ListBoxRow()
        b = Gtk.Box(spacing=14)
        b.add_css_class("person")
        b.append(avatar(self.app, c["name"], c["avatar"], 44))
        n = label(c["name"], "name")
        n.set_hexpand(True)
        b.append(n)
        b.append(net_chip(c["net"]))
        row.set_child(b)
        row.cid = c["id"]
        return row

    def _number_row(self, name, nums):
        row = Gtk.ListBoxRow()
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        col.add_css_class("person")
        top = Gtk.Box(spacing=14)
        top.append(Adw.Avatar(size=44, text=name, show_initials=True))
        tc = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        tc.append(label(name, "name"))
        tc.append(label(nums[0][1] if len(nums) == 1 else "%d numbers" % len(nums), "nums"))
        top.append(tc)
        col.append(top)
        opts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        opts.set_visible(False)
        col.append(opts)
        row.set_child(col)
        row.person = (name, nums, opts)
        return row

    def _picked(self, lb, row):
        if getattr(row, "cid", None):
            self.win.nav.pop()
            return self.win.open_chat(row.cid)
        if not getattr(row, "person", None):
            return
        name, nums, opts = row.person
        if opts.get_visible():
            opts.set_visible(False)
            return
        while opts.get_first_child():
            opts.remove(opts.get_first_child())
        for num, shown in nums:
            line = Gtk.Box(spacing=8)
            line.add_css_class("num-line")
            wa = text_button("WhatsApp …", lambda n=num: self.go_whatsapp(name, n), "chip-btn net-wa")
            smsb = text_button("SMS", lambda n=num: self.go_sms(name, n), "chip-btn net-sms")
            line.append(wa)
            line.append(smsb)
            line.append(label(shown, "num-text"))
            opts.append(line)
            bg(lambda n=num: self.app.matrix.has_whatsapp(n),
               lambda ok, b=wa: (b.set_label("WhatsApp" if ok else "no WhatsApp"), b.set_sensitive(ok)),
               lambda e, b=wa: b.set_label("WhatsApp ?"))
        opts.set_visible(True)

    def go_sms(self, name, number):
        cid = self.app.sms.open_chat(number, name if name != number else None)
        self.win.nav.pop()
        self.win.open_chat(cid)

    def go_whatsapp(self, name, number):
        if self.busy:
            return
        self.busy = True
        self.status.set_label("opening WhatsApp chat with %s…" % name)

        def ok(rid):
            self.busy = False
            self.status.set_label("")
            self.app.store.upsert_chat(rid, net="wa", name=name)
            self.app.store.put("phone:" + rid, number)
            self.win.nav.pop()
            self.win.open_chat(rid)

        def fail(e):
            self.busy = False
            self.status.set_label(str(e))
        bg(lambda: self.app.matrix.start_whatsapp(number, name if name != number else ""), ok, fail)


# --- window ----------------------------------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="notreal.chat", default_width=540, default_height=1100)
        self.app = app
        self.current = None
        self.pages = {}            # chat id -> ChatPage, most recently opened last
        self.nav = Adw.NavigationView()
        self.list_page = ChatList(self)
        self.nav.add(self.list_page)
        self.nav.connect("popped", self._popped)
        self.list_page.connect("shown", self._list_shown)
        self.set_content(self.nav)
        for name, cb in (("show-archived", lambda: self.list_page.show_archived()),
                         ("show-hidden", lambda: self.list_page.show_hidden()),
                         ("refresh-contacts", app.refresh_contacts),
                         ("toggle-mute", self._toggle_mute),
                         ("archive-current", self._archive_current),
                         ("delete-current", lambda: self.current and self.confirm_delete(self.current.cid))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, cb=cb: cb())
            self.add_action(a)
        self.connect("notify::is-active", lambda *_: self._mark_current())
        try:
            test = open(TEST_FILE).read().strip()
        except OSError:
            test = None
        if test:          # timing test: open + close the chat twice
            def close():
                frame_watch(self, "close")
                self.nav.pop()
                return False
            for k in range(2):
                GLib.timeout_add(20000 + k * 5000, lambda: (self.open_chat(test), False)[1])
                GLib.timeout_add(22500 + k * 5000, close)

    def _popped(self, nav, page):
        # "popped" comes at the START of the slide back: refreshing the list and freeing the chat
        # page's widgets right then stalled the first frames (10-17). Keep the page alive and
        # refresh once the list is fully shown again (_list_shown).
        if isinstance(page, ChatPage):
            self.current = None
            self._closing = page
            self._refresh_pending = True

    def _list_shown(self, *_):
        if getattr(self, "_refresh_pending", False):
            self._refresh_pending = False
            t0 = time.time()
            self.list_page.refresh()
            timed("list refresh (after pop)", t0)
        GLib.timeout_add(300, lambda: (setattr(self, "_closing", None), False)[1])  # free it later

    def open_chat(self, cid):
        c = self.app.store.chat(cid)
        if not c:
            return
        if c["invite"]:
            self.list_page.toasts.add_toast(Adw.Toast(title="joining…", timeout=2))
            bg(lambda: self.app.matrix.join(cid))
            return
        while self.nav.get_visible_page() is not self.list_page:
            self.nav.pop()
        t0 = time.time()
        # 10-17: the last few chats stay built (PAGE_CACHE): reopening one only diffs in new
        # messages - no ~80 ms page build, and GTK keeps its styles/layout, so no heavy first frame
        page = self.pages.pop(cid, None)
        if page:
            page.stick = True
            page.load()
        else:
            page = ChatPage(self, cid)
        self.pages[cid] = page
        while len(self.pages) > PAGE_CACHE:
            self.pages.pop(next(iter(self.pages)))
        self.current = page
        timed("ChatPage build", t0)
        frame_watch(self, "open")
        self.nav.push(self.current)
        self._mark_current()

    def _mark_current(self):
        if self.current and self.is_active():
            c = self.app.store.chat(self.current.cid)
            if c and c["unread"]:
                self.app.mark_read(self.current.cid)
            self.app.withdraw_notification("chat-" + self.current.cid)

    def new_chat(self):
        self.nav.push(NewChat(self))

    def view_media(self, page, m, media):
        self.nav.push(Viewer(self, page, m, media))

    def _toggle_mute(self):
        if self.current:
            c = self.app.store.chat(self.current.cid)
            self.app.store.upsert_chat(c["id"], muted=int(not c["muted"]))
            self.current.toast("muted" if not c["muted"] else "notifications on")

    def _archive_current(self):
        if self.current:
            cid = self.current.cid
            self.nav.pop()
            self.list_page._swiped(cid, -1)

    def confirm_delete(self, cid):
        c = self.app.store.chat(cid)
        dlg = Adw.AlertDialog(heading="Delete chat?",
                              body="%s will disappear from this phone%s." % (
                                  c["name"], "" if cid.startswith("sms:") else
                                  " (you leave the Matrix room; the WhatsApp/Messenger chat itself stays)"))
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(d, r):
            if r != "delete":
                return
            if self.current and self.current.cid == cid:
                self.nav.pop()
            self.pages.pop(cid, None)
            if cid.startswith("sms:"):
                self.app.store.drop_chat(cid)
                self.list_page.refresh()
            else:
                bg(lambda: self.app.matrix.leave(cid), lambda _r: self.list_page.refresh())
        dlg.connect("response", resp)
        dlg.present(self)

    def changed(self, ids):
        if self.current and self.current.cid in ids:
            self.current.chat = self.app.store.chat(self.current.cid) or self.current.chat
            self.current.load()
            self._mark_current()
        if self.nav.get_visible_page() is self.list_page or ids:
            self.list_page.refresh()

    def set_status(self, s):
        lp = self.list_page
        lp.status.set_visible(s != "online")
        lp.status.set_label({"offline": "offline · reconnecting…", "logged out": "Matrix login expired"}.get(s, s))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None
        self.started = False
        self.lockf = None
        self.net_status = "online"
        a = Gio.SimpleAction.new("open-chat", GLib.VariantType.new("s"))
        a.connect("activate", lambda _a, v: self.open_chat(v.get_string()))
        self.add_action(a)
        a = Gio.SimpleAction.new("open-karere", None)
        a.connect("activate", lambda *_: launch_desktop(KARERE))
        self.add_action(a)
        a = Gio.SimpleAction.new("mark-read", GLib.VariantType.new("s"))
        a.connect("activate", lambda _a, v: self.mark_read(v.get_string()))
        self.add_action(a)

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        # shared look from ~/theme/theme.css, then this app's style.css (both live-reload)
        notareal_theme.apply(os.path.join(HERE, "style.css"))
        if Lfb:
            try:
                Lfb.init(APP_ID)
            except GLib.Error:
                pass
        if not os.environ.get("NOTAREALCHAT_LOCKED"):  # the service launcher waits on this lock
            self.lockf = open(LOCK, "w")
            try:
                fcntl.flock(self.lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                pass
        self.hold()  # stays in the background for notifications
        self.store = Store()
        self.book = contacts.Book()
        self.matrix = Matrix(self.store, self._changed_bg, lambda s: GLib.idle_add(self._status, s))
        self.sms = smsmod.Sms(self.store, self._changed_main, self.book.name_for)
        smsmod.import_chatty(self.store, self.book.name_for)
        self.matrix.start()
        self.sms.start()
        GLib.timeout_add_seconds(20, lambda: (self.refresh_contacts(), False)[1])

    def do_command_line(self, cl):
        args = cl.get_arguments()[1:]
        if "--service" in args:
            return 0
        if "--open" in args:
            i = args.index("--open")
            if i + 1 < len(args):
                self.open_chat(args[i + 1])
                return 0
        for flag in ("--sms", "--whatsapp"):   # notarealContacts: start/open a chat with a number
            if flag in args:
                i = args.index(flag)
                number = args[i + 1] if i + 1 < len(args) else ""
                name = args[i + 2] if i + 2 < len(args) else ""
                if number:
                    self.activate()
                    GLib.timeout_add(300, lambda f=flag, n=number, nm=name: (self.start_number(f, n, nm), False)[1])
                    return 0
        if "--send" in args:   # nrshare: --send <chat id> <file>... = open the chat and send them
            i = args.index("--send")
            if i + 2 <= len(args):
                cid, paths = args[i + 1], [p for p in args[i + 2:] if os.path.isfile(p)]
                self.open_chat(cid)

                def send():
                    page = self.win.current if self.win else None
                    if page and page.cid == cid and paths:
                        page.send_files(paths)
                    return False
                GLib.timeout_add(500, send)
                return 0
        self.activate()
        return 0

    def do_activate(self):
        if not self.win:
            self.win = Window(self)
            self.win.connect("close-request", self._closed)
            self.win.set_status(self.net_status)
        self.win.present()

    def _closed(self, win):
        self.win = None  # the service lives on without a window
        return False

    def open_chat(self, cid):
        self.activate()
        self.win.open_chat(cid)

    def start_number(self, flag, number, name):
        """--sms / --whatsapp from notarealContacts: the same paths as New Chat."""
        page = NewChat(self.win)
        if flag == "--sms":
            cid = self.sms.open_chat(number, name or None)
            self.win.open_chat(cid)
            return
        self.win.nav.push(page)          # shows "opening WhatsApp chat with …" while it resolves
        page.go_whatsapp(name or number, number)

    def refresh_contacts(self):
        def done(people):
            self.book.set(people)
            for c in self.store.chats():  # SMS chats get contact names
                if c["net"] == "sms" and c["name"] == c["id"][4:]:
                    n = self.book.name_for(c["id"][4:])
                    if n:
                        self.store.upsert_chat(c["id"], name=n)
        bg(contacts.fetch, done)

    def _status(self, s):
        self.net_status = s
        if self.win:
            self.win.set_status(s)

    def _changed_bg(self, ids, fresh):
        GLib.idle_add(lambda: (self._changed_main(ids, fresh), False)[1])

    def _changed_main(self, ids, fresh):
        if self.win:
            self.win.changed(ids)
        by_chat = {}
        for m in fresh:
            by_chat.setdefault(m["chat"], []).append(m)
        for cid, msgs in by_chat.items():
            self.notify(cid, msgs)

    def notify(self, cid, msgs):
        c = self.store.chat(cid)
        if not c or c["muted"] or c["hidden"]:
            return
        w = self.win
        if w and w.is_active() and w.current and w.current.cid == cid:
            return  # you're looking at it
        if not c["unread"] and not cid.startswith("sms:"):
            return  # already read elsewhere
        m = msgs[-1]
        if c["net"] == "wa" and (m["body"] or "").startswith(CALL_NOTICE):
            return self.notify_call(cid, c)
        from store import preview
        text = preview(m)
        if not c["dm"]:
            mem = self.store.member(cid, m["sender"])
            text = "%s: %s" % (mem["name"] if mem else m["sender"][1:].split(":")[0], text)
        n = Gio.Notification.new("%s · %s" % (c["name"], NETS.get(c["net"], "")))
        n.set_body(text if c["unread"] <= 1 else "%s  (+%d more)" % (text, c["unread"] - 1))
        n.set_priority(Gio.NotificationPriority.HIGH)
        n.set_default_action("app.open-chat::" + cid)
        n.add_button("Mark read", "app.mark-read::" + cid)
        if hasattr(n, "set_category"):
            n.set_category("im.received")
        self.send_notification("chat-" + cid, n)
        if Lfb:
            try:
                Lfb.Event.new("message-new-instant").trigger_feedback_async(None, None)
            except Exception:  # noqa: BLE001
                buzz("message-new-instant")
        else:
            buzz("message-new-instant")

    def notify_call(self, cid, c):
        """WhatsApp rings: the bridge can't take calls, so this hands off to WhatsApp Web (WhatsApp Calls)."""
        n = Gio.Notification.new("📞 WhatsApp call · %s" % c["name"])
        n.set_body("Tap to answer in WhatsApp Calls (~12 s to open)")
        n.set_priority(Gio.NotificationPriority.URGENT)
        n.set_default_action("app.open-karere")
        n.add_button("Open WhatsApp", "app.open-karere")
        n.add_button("Open chat", "app.open-chat::" + cid)
        if hasattr(n, "set_category"):
            n.set_category("call.incoming")
        self.send_notification("call-" + cid, n)
        buzz("phone-incoming-call")
        GLib.timeout_add_seconds(60, lambda: (self.withdraw_notification("call-" + cid), False)[1])

    def mark_read(self, cid):
        self.withdraw_notification("chat-" + cid)
        if cid.startswith("sms:"):
            self.store.upsert_chat(cid, unread=0)
        else:
            self.store.upsert_chat(cid, unread=0)
            bg(lambda: self.matrix.mark_read(cid))
        if self.win:
            self.win.list_page.refresh()

    def set_archived(self, cid, on):
        if cid.startswith("sms:"):
            self.store.upsert_chat(cid, archived=int(on))
        else:
            self.store.upsert_chat(cid, archived=int(on))
            bg(lambda: self.matrix.set_archived(cid, on))


LOG_CAP = 20 * 1024 * 1024


def _cap_log():
    # 10-12: a GTK warning loop wrote 736 MB and filled $XDG_RUNTIME_DIR (the overview widget died with it)
    try:
        if os.fstat(2).st_size > LOG_CAP:
            os.ftruncate(2, 0)
            os.write(2, b"[log over 20 MB, truncated]\n")
    except OSError:
        pass
    return True


if __name__ == "__main__":
    GLib.timeout_add_seconds(30, _cap_log)
    sys.exit(App().run(sys.argv))
