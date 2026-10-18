#!/usr/bin/env python3
"""notarealCalls: dialer, recents and an in-call screen, themed, left-handed.

GNOME Calls keeps doing the actual call work (ModemManager, callaudiod, our earpiece/speaker
helpers all hang off it), so calls keep working exactly as before. This app:
  - dials through GNOME Calls' "dial" action (tel: URI),
  - follows calls on its D-Bus object manager (/org/gnome/Calls, org.gnome.Calls.Call:
    Accept / Hangup / SendDtmf, State, Inbound, Id, DisplayName),
  - mute + speaker through callaudiod (org.mobian_project.CallAudio), the same calls GNOME Calls
    makes, so callmode-user sees the speaker switch as before,
  - reads the call history from GNOME Calls' records.db, names from notarealChat's contact cache.
`--service` (autostart) runs hidden and brings the in-call screen up for every call.
"""
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.expanduser("~/theme"))
import notareal_theme  # noqa: E402

APP_ID = "dev.notareal.Calls"
RECORDS = os.path.expanduser("~/.local/share/calls/records.db")
CONTACTS = os.path.expanduser("~/.cache/notarealchat/contacts.json")
CALLS_BUS, CALLS_PATH = "org.gnome.Calls", "/org/gnome/Calls"
CALL_IFACE = "org.gnome.Calls.Call"
# CallsCallState
UNKNOWN, ACTIVE, HELD, DIALING, ALERTING, INCOMING, WAITING, DISCONNECTED = range(8)
STATE_TEXT = {ACTIVE: "", HELD: "ON HOLD", DIALING: "CALLING…", ALERTING: "RINGING…",
              INCOMING: "INCOMING CALL", WAITING: "WAITING", DISCONNECTED: "CALL ENDED"}
KEYS = [("1", ""), ("2", "ABC"), ("3", "DEF"), ("4", "GHI"), ("5", "JKL"), ("6", "MNO"),
        ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"), ("*", ""), ("0", "+"), ("#", "")]
DEFAULT_CC = "+46"


def label(text="", css=None, xalign=0.0, ellipsize=True):
    lb = Gtk.Label(label=text, xalign=xalign)
    if ellipsize:
        lb.set_ellipsize(Pango.EllipsizeMode.END)
    if css:
        for c in css.split():
            lb.add_css_class(c)
    return lb


def digits(n):
    return re.sub(r"[^\d+]", "", n or "")


def e164(n):
    d = digits(n)
    if d.startswith("00"):
        d = "+" + d[2:]
    if d and not d.startswith("+"):
        d = DEFAULT_CC + d.lstrip("0")
    return d


class Names:
    """+e164 -> contact name (notarealChat's Google contacts cache)."""

    def __init__(self):
        self.by_num, self.mtime = {}, 0
        self.reload()

    def reload(self):
        try:
            m = os.path.getmtime(CONTACTS)
            if m == self.mtime:
                return
            with open(CONTACTS) as f:
                people = json.load(f)
            self.by_num = {}
            for name, nums in people:
                for num, _shown in nums:
                    self.by_num[e164(num)] = name
            self.mtime = m
        except (OSError, ValueError):
            pass

    def __call__(self, number):
        return self.by_num.get(e164(number))


def history(limit=200):
    try:
        db = sqlite3.connect(f"file:{RECORDS}?mode=ro", uri=True, timeout=2)
        rows = db.execute("SELECT target, inbound, start, answered, end, protocol FROM calls "
                          "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        db.close()
    except sqlite3.Error as e:
        print("nrcalls: history:", e, flush=True)
        return []
    out = []
    for target, inbound, start, answered, end, proto in rows:
        def ts(v):
            try:
                return datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone()
            except ValueError:
                return None
        s, a, e = ts(start), ts(answered) if answered else None, ts(end)
        out.append({"number": target or "", "inbound": bool(inbound), "start": s,
                    "missed": bool(inbound) and a is None,
                    "secs": int((e - a).total_seconds()) if a and e else 0})
    return out


def when(dt):
    if not dt:
        return ""
    now = datetime.datetime.now().astimezone()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if (now.date() - dt.date()).days == 1:
        return "yesterday " + dt.strftime("%H:%M")
    if (now.date() - dt.date()).days < 7:
        return dt.strftime("%a %H:%M")
    return dt.strftime("%d %b")


def callaudio(method, value):
    """EnableSpeaker / MuteMic on callaudiod, like GNOME Calls does."""
    try:
        Gio.bus_get_sync(Gio.BusType.SESSION).call_sync(
            "org.mobian_project.CallAudio", "/org/mobian_project/CallAudio", "org.mobian_project.CallAudio",
            method, GLib.Variant("(b)", (value,)), None, Gio.DBusCallFlags.NONE, 3000, None)
        return True
    except GLib.Error as e:
        print("nrcalls: callaudio", method, e.message, flush=True)
        return False


def dial(number):
    """Place a cellular call through GNOME Calls (it owns the modem + call audio)."""
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    bus.call_sync(CALLS_BUS, CALLS_PATH, "org.gtk.Actions", "Activate",
                  GLib.Variant("(sava{sv})", ("dial", [GLib.Variant("s", "tel:" + number)], {})),
                  None, Gio.DBusCallFlags.NONE, 5000, None)


def raise_window(app_id):
    for ms in (250, 800, 1600):     # GNOME Calls brings its own window up: come back on top
        GLib.timeout_add(ms, lambda: (subprocess.run(["wlrctl", "toplevel", "focus", "app_id:" + app_id],
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
                                      False)[1])


# --- pages ----------------------------------------------------------------------------------------

class Keypad(Gtk.Box):
    def __init__(self, on_key, big=True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER)
        grid = Gtk.Grid(row_spacing=10, column_spacing=18, halign=Gtk.Align.CENTER)
        for i, (k, letters) in enumerate(KEYS):
            b = Gtk.Button(css_classes=["key"] + (["big"] if big else []))
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            v.append(label(k, "key-digit", 0.5, False))
            v.append(label(letters, "key-letters", 0.5, False))
            b.set_child(v)
            b.connect("clicked", lambda *_, k=k: on_key(k))
            if k == "0":                     # hold 0 = +
                lp = Gtk.GestureLongPress()
                lp.connect("pressed", lambda g, *_: (g.set_state(Gtk.EventSequenceState.CLAIMED), on_key("+")))
                b.add_controller(lp)
            grid.attach(b, i % 3, i // 3, 1, 1)
        self.append(grid)


class DialPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10, margin_bottom=16)
        self.win = win
        self.number = label("", "dial-number", 0.5)
        self.number.set_ellipsize(Pango.EllipsizeMode.START)
        self.who = label("", "dial-who", 0.5)
        self.append(self.number)
        self.append(self.who)
        self.append(Gtk.Box(vexpand=True))
        self.append(Keypad(self.key))
        row = Gtk.Box(spacing=16, margin_start=28, margin_end=28, margin_top=10)
        call = Gtk.Button(css_classes=["call-btn"])
        call.set_child(Gtk.Image(icon_name="call-start-symbolic", pixel_size=30))
        call.connect("clicked", lambda *_: self.call())
        row.append(call)                      # left-handed: call button on the left
        row.append(Gtk.Box(hexpand=True))
        back = Gtk.Button(icon_name="edit-clear-symbolic", css_classes=["flat", "circular", "back-key"],
                          valign=Gtk.Align.CENTER)
        back.connect("clicked", lambda *_: self.backspace())
        lp = Gtk.GestureLongPress()
        lp.connect("pressed", lambda *_: self.set_number(""))
        back.add_controller(lp)
        row.append(back)
        self.append(row)
        self.text = ""

    def key(self, k):
        if k == "+" and self.text.endswith("0"):
            self.text = self.text[:-1]
        self.set_number(self.text + k)

    def backspace(self):
        self.set_number(self.text[:-1])

    def set_number(self, t):
        self.text = t
        self.number.set_label(t)
        self.who.set_label(self.win.names(t) or "" if len(digits(t)) >= 6 else "")

    def call(self):
        if not self.text:          # empty: fill in the last number, like most phones
            h = history(1)
            if h:
                self.set_number(h[0]["number"])
            return
        self.win.place_call(self.text)


class RecentsPage(Gtk.ScrolledWindow):
    def __init__(self, win):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.win = win
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["recents"])
        self.set_child(self.box)

    def fill(self):
        while self.box.get_first_child():
            self.box.remove(self.box.get_first_child())
        rows = history()
        # collapse repeated calls with the same number in a row (like "Ana (3)")
        grouped = []
        for r in rows:
            if grouped and e164(grouped[-1][0]["number"]) == e164(r["number"]) and \
                    grouped[-1][0]["missed"] == r["missed"]:
                grouped[-1].append(r)
            else:
                grouped.append([r])
        if not grouped:
            self.box.append(label("no calls yet", "empty", 0.5))
        for g in grouped:
            self.box.append(self.row(g))
        self.box.append(Gtk.Box(height_request=20))

    def row(self, group):
        r = group[0]
        name = self.win.names(r["number"])
        b = Gtk.Button(css_classes=["recent"] + (["missed"] if r["missed"] else []))
        h = Gtk.Box(spacing=12)
        arrow = "call-missed-symbolic" if r["missed"] else ("call-incoming-symbolic" if r["inbound"]
                                                            else "call-outgoing-symbolic")
        h.append(Gtk.Image(icon_name=arrow, css_classes=["dir"]))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        title = (name or r["number"] or "unknown") + (f"  ({len(group)})" if len(group) > 1 else "")
        col.append(label(title, "r-name"))
        sub = when(r["start"])
        if name:
            sub = r["number"] + " · " + sub
        if r["secs"]:
            sub += f" · {r['secs'] // 60}:{r['secs'] % 60:02d}"
        col.append(label(sub, "r-sub"))
        h.append(col)
        b.set_child(h)
        b.connect("clicked", lambda *_: self.win.place_call(r["number"]))
        return b


class InCall(Gtk.Box):
    """The call screen: who, state + timer, MUTE / SPEAKER / KEYS, big HANG UP bottom-left."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=40, margin_bottom=26,
                         margin_start=22, margin_end=22, css_classes=["incall"])
        self.win = win
        self.proxy = None
        self.answered_at = None
        self.name = label("", "ic-name", 0.5)
        self.num = label("", "ic-num", 0.5)
        self.state = label("", "ic-state", 0.5)
        for w in (self.name, self.num, self.state):
            self.append(w)
        self.dtmf_text = label("", "ic-dtmf", 0.5)
        self.append(self.dtmf_text)
        self.append(Gtk.Box(vexpand=True))
        self.pad = Keypad(self.dtmf, big=False)
        self.pad.set_visible(False)
        self.append(self.pad)
        self.toggles = Gtk.Box(spacing=14, halign=Gtk.Align.CENTER, margin_top=10)
        self.mute = self._toggle("microphone-sensitive-symbolic", "MUTE", self.set_mute)
        self.spk = self._toggle("audio-speakers-symbolic", "SPEAKER", self.set_speaker)
        self.keys = self._toggle("input-dialpad-symbolic", "KEYS", lambda on: self.pad.set_visible(on))
        self.append(self.toggles)
        row = Gtk.Box(spacing=18, margin_top=18)
        self.hang = Gtk.Button(css_classes=["hang-btn"])
        self.hang.set_child(Gtk.Image(icon_name="call-stop-symbolic", pixel_size=30))
        self.hang.connect("clicked", lambda *_: self.call("Hangup"))
        row.append(self.hang)                  # left-handed: hang up bottom-left
        row.append(Gtk.Box(hexpand=True))
        self.answer = Gtk.Button(css_classes=["call-btn"])
        self.answer.set_child(Gtk.Image(icon_name="call-start-symbolic", pixel_size=30))
        self.answer.connect("clicked", lambda *_: self.call("Accept"))
        row.append(self.answer)
        self.append(row)
        GLib.timeout_add(1000, self._tick)

    def _toggle(self, icon, text, cb):
        b = Gtk.ToggleButton(css_classes=["ic-toggle"])
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        v.append(Gtk.Image(icon_name=icon, pixel_size=24))
        v.append(label(text, "ic-tlabel", 0.5, False))
        b.set_child(v)
        b.connect("toggled", lambda btn: cb(btn.get_active()))
        self.toggles.append(b)
        return b

    def bind(self, proxy):
        self.proxy = proxy
        self.answered_at = None
        self.dtmf_text.set_label("")
        for b in (self.mute, self.spk, self.keys):
            b.set_active(False)
        proxy.connect("g-properties-changed", lambda *_: self.update())
        self.update()

    def prop(self, name, default=None):
        v = self.proxy.get_cached_property(name) if self.proxy else None
        return v.unpack() if v is not None else default

    def update(self):
        if not self.proxy:
            return
        st = self.prop("State", UNKNOWN)
        number = self.prop("Id", "") or ""
        name = self.prop("DisplayName", "") or self.win.names(number) or ""
        self.name.set_label(name or number or "unknown")
        self.num.set_label(number if name else "")
        if st == ACTIVE and not self.answered_at:
            self.answered_at = time.monotonic()
        incoming = st in (INCOMING, WAITING)
        self.answer.set_visible(incoming)
        self.toggles.set_visible(not incoming)
        self._tick()

    def _tick(self):
        if self.proxy:
            st = self.prop("State", UNKNOWN)
            if st == ACTIVE and self.answered_at:
                s = int(time.monotonic() - self.answered_at)
                self.state.set_label(f"{s // 60}:{s % 60:02d}")
            else:
                self.state.set_label(STATE_TEXT.get(st, ""))
        return True

    def call(self, method):
        if self.proxy:
            self.proxy.call(method, None, Gio.DBusCallFlags.NONE, 5000, None, None)

    def dtmf(self, k):
        if self.proxy and k != "+":
            self.proxy.call("SendDtmf", GLib.Variant("(s)", (k,)), Gio.DBusCallFlags.NONE, 3000, None, None)
            self.dtmf_text.set_label((self.dtmf_text.get_label() + k)[-16:])

    def set_mute(self, on):
        callaudio("MuteMic", on)

    def set_speaker(self, on):
        callaudio("EnableSpeaker", on)


# --- window ----------------------------------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Phone", default_width=540, default_height=1100)
        self.app = app
        self.names = app.names
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        hb.add_css_class("vapor-bar")
        hb.set_title_widget(label("PHONE", "app-title", 0.5, False))
        tv.add_top_bar(hb)
        self.tabs = Adw.ViewStack()
        self.dialer = DialPage(self)
        self.recents = RecentsPage(self)
        self.tabs.add_titled_with_icon(self.recents, "recents", "Recents", "document-open-recent-symbolic")
        self.tabs.add_titled_with_icon(self.dialer, "keypad", "Keypad", "input-dialpad-symbolic")
        contacts = Gtk.Box()
        self.tabs.add_titled_with_icon(contacts, "contacts", "Contacts", "contact-new-symbolic")
        self.tabs.connect("notify::visible-child-name", self._tab)
        tv.set_content(self.tabs)
        bar = Adw.ViewSwitcherBar(stack=self.tabs, reveal=True)
        tv.add_bottom_bar(bar)
        self.stack.add_named(tv, "main")
        self.incall = InCall(self)
        self.stack.add_named(self.incall, "call")
        self.toasts = Adw.ToastOverlay(child=self.stack)
        self.set_content(self.toasts)
        self.recents.fill()
        self.connect("notify::is-active", lambda *_: self.is_active() and self.recents.fill())

    def _tab(self, *_):
        if self.tabs.get_visible_child_name() == "contacts":
            self.tabs.set_visible_child_name("recents")
            subprocess.Popen([os.path.expanduser("~/contacts/nrcontacts")], start_new_session=True)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    def place_call(self, number):
        number = number.strip()
        if not digits(number):
            return
        try:
            dial(number)
        except GLib.Error as e:
            self.toast("couldn't call: " + e.message)
            return
        self.dialer.set_number("")
        raise_window(APP_ID)

    def show_call(self, proxy):
        self.incall.bind(proxy)
        self.stack.set_visible_child_name("call")
        self.present()
        raise_window(APP_ID)

    def call_over(self):
        self.incall.proxy = None
        self.stack.set_visible_child_name("main")
        self.recents.fill()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None
        self.names = Names()
        self.calls = {}
        self.manager = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        notareal_theme.apply(os.path.join(HERE, "style.css"))
        self.hold()                    # stay around to show the call screen for every call
        self._watch_calls()

    def _watch_calls(self):
        def connect():
            try:
                self.manager = Gio.DBusObjectManagerClient.new_for_bus_sync(
                    Gio.BusType.SESSION, Gio.DBusObjectManagerClientFlags.NONE, CALLS_BUS, CALLS_PATH,
                    None, None, None)
            except GLib.Error as e:
                print("nrcalls: GNOME Calls not reachable yet:", e.message, flush=True)
                return True            # retry
            self.manager.connect("object-added", lambda _m, o: self._added(o))
            self.manager.connect("object-removed", lambda _m, o: self._removed(o))
            for o in self.manager.get_objects():
                self._added(o)
            return False
        if connect():
            GLib.timeout_add_seconds(10, connect)

    def _added(self, obj):
        proxy = obj.get_interface(CALL_IFACE)
        if not proxy:
            return
        self.calls[obj.get_object_path()] = proxy
        self.names.reload()
        print("nrcalls: call", obj.get_object_path(), flush=True)
        self._window().show_call(proxy)

    def _removed(self, obj):
        self.calls.pop(obj.get_object_path(), None)
        if self.win:
            if self.calls:
                self.win.show_call(next(iter(self.calls.values())))
            else:
                GLib.timeout_add(1200, lambda: (self.win.call_over(), False)[1])

    def _window(self):
        if not self.win:
            self.win = Window(self)
        return self.win

    def do_command_line(self, cl):
        args = cl.get_arguments()[1:]
        if "--service" in args:
            return 0
        self.activate()
        if "--dial" in args:
            i = args.index("--dial")
            if i + 1 < len(args):
                self.win.place_call(args[i + 1])
        return 0

    def do_activate(self):
        self.names.reload()
        self._window().present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
