#!/usr/bin/env python3
"""vaporMail: a swipeable Gmail client for the phone, in the vapor/keyboard look.

Swipe a mail right -> snooze (quick time picker), left -> Trash (with Undo).
Pull down at the top of the list (or the refresh button) fetches; vapormaild notifies
every 15 minutes in the background.
"""
import datetime
import email.utils
import hashlib
import os
import signal
import subprocess
import sys
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango, WebKit  # noqa: E402
import notareal_theme  # noqa: E402  ~/theme/theme.css

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gmail  # noqa: E402
import graph  # noqa: E402
import contacts  # noqa: E402
import translate  # noqa: E402

APP_ID = "dev.notareal.VaporMail"
HERE = os.path.dirname(os.path.abspath(__file__))
SWIPE_COMMIT = 0.33        # fraction of the row width that commits a swipe
SWIPE_START = 14           # px of sideways travel before a drag becomes a swipe
UNDO_S = 5
AVATAR_COLORS = 6  # .av0 … .av5 in style.css
OUTLOOK_DOMAINS = ("outlook.", "hotmail.", "live.", "msn.")
FOLDER_LISTS = ("sent", "spam", "trash")       # tabs listed straight from the server
RESTORE_HINT = {"spam": "NOT SPAM ↩", "trash": "RESTORE ↩"}


def tint_class(acct):
    """Rows from the non-Gmail accounts get their own tint (style.css .tint-*)."""
    if not acct or acct == "gmail":
        return None
    domain = acct.split("@")[-1].lower()
    return "tint-outlook" if domain.startswith(OUTLOOK_DOMAINS) else "tint-exchange"


def folder_cache(folder):
    return gmail.SENT_CACHE if folder == "sent" else os.path.join(gmail.CACHE_DIR, folder + ".json")


def bg(fn, done=None, fail=None):
    """Run fn() in a thread, deliver the result (or exception) on the main loop."""
    def run():
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            print("bg error:", repr(e), flush=True)
            if fail:
                GLib.idle_add(lambda: (fail(e), False)[1])
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


def short_date(ts):
    d = datetime.datetime.fromtimestamp(ts)
    now = datetime.datetime.now()
    if d.date() == now.date():
        return d.strftime("%H:%M")
    if d.year == now.year:
        return d.strftime("%-d %b")
    return d.strftime("%-d/%-m/%y")


def due_text(ts):
    d = datetime.datetime.fromtimestamp(ts)
    now = datetime.datetime.now()
    if d.date() == now.date():
        return d.strftime("today %H:%M")
    if d.date() == (now + datetime.timedelta(days=1)).date():
        return d.strftime("tomorrow %H:%M")
    if (d - now).days < 6:
        return d.strftime("%a %H:%M")
    return d.strftime("%a %-d %b %H:%M")


def buzz(event="button-pressed"):
    subprocess.Popen(["fbcli", "-E", event], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def snooze_options():
    """Gmail-style quick picks: [(label, datetime)]."""
    now = datetime.datetime.now().replace(second=0, microsecond=0)
    at = lambda day, h: day.replace(hour=h, minute=0)  # noqa: E731
    opts = []
    hour = now + datetime.timedelta(hours=1)
    opts.append(("In an hour", hour.replace(minute=(hour.minute // 5) * 5)))
    if now.hour < 16:
        opts.append(("Later today", at(now, 18)))
    if now.hour < 19:
        opts.append(("This evening", at(now, 20)))
    opts.append(("Tomorrow", at(now + datetime.timedelta(days=1), 8)))
    wd = now.weekday()  # Mon=0
    if wd < 4:
        opts.append(("This weekend", at(now + datetime.timedelta(days=5 - wd), 9)))
    opts.append(("Next week", at(now + datetime.timedelta(days=7 - wd), 8)))
    return opts


# --- swipeable row -------------------------------------------------------


NRLINK = os.path.expanduser("~/link/nrlink")


def open_link(uri, parent=None):
    """Web links -> notarealLink (light WebKit viewer); mailto:, tel: etc. -> the system."""
    if uri.lower().startswith(("http://", "https://")) and os.path.exists(NRLINK):
        subprocess.Popen([NRLINK, uri], start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    Gtk.UriLauncher.new(uri).launch(parent, None, None, None)


class SwipeRow(Gtk.Widget):
    """A mail row that slides sideways. Right = later (cyan), left = trash (pink)."""
    __gsignals__ = {"swiped": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
                    "avatar-tapped": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, msg, snoozed=False, sent=False, restore=None):
        super().__init__()
        self.msg = msg
        self.sent = sent
        self.left_only = sent or bool(restore)   # sent / spam / trash: no snooze swipe
        self.offset = 0.0
        self.swiping = False
        self.armed = 0
        self.anim = None
        self.swipe_enabled = True
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.add_css_class("swipe-row")

        self.under = Gtk.CenterBox()
        self.under.add_css_class("swipe-under")
        self.left_hint = label("" if self.left_only else "⏰ LATER", "hint-later")
        self.right_hint = label(restore or ("INBOX ↩" if snoozed else "TRASH ✕"), "hint-trash", xalign=1.0)
        self.under.set_start_widget(self.left_hint)
        self.under.set_end_widget(self.right_hint)
        self.under.set_parent(self)

        self.fg = self._content(msg, snoozed, sent)
        self.fg.set_parent(self)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.add_controller(drag)

    def _content(self, m, snoozed, sent):
        box = Gtk.Box(spacing=12)
        box.add_css_class("mail-row")
        tint = tint_class(m.get("acct"))
        if tint:
            box.add_css_class(tint)
        if not m.get("seen", True) and not snoozed and not sent:
            box.add_css_class("unread")
        if sent:  # who it went to, not us
            to = email.utils.getaddresses([m.get("to", "")])
            name = (to[0][0] or to[0][1]) if to and any(to[0]) else "?"
            if len(to) > 1:
                name += " +%d" % (len(to) - 1)
        else:
            name = m.get("from_name") or m.get("from_addr") or "?"
        av = label(name[:1].upper(), "avatar", xalign=0.5, ellipsize=False)
        av.add_css_class("av%d" % (int(hashlib.md5(name.encode()).hexdigest(), 16) % AVATAR_COLORS))
        av.set_valign(Gtk.Align.START)
        self.avatar, self.initial = av, name[:1].upper()
        tap = Gtk.GestureClick()
        tap.connect("released", self._avatar_tap)
        av.add_controller(tap)
        box.append(av)

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        top = Gtk.Box(spacing=6)
        top.append(label(("→ " if sent else "") + name, "from", ellipsize=True))
        top.get_first_child().set_hexpand(True)
        when = due_text(m["due"]) if snoozed else short_date(m.get("date", time.time()))
        top.append(label(("⏰ " if snoozed else "") + when, "when" + (" due" if snoozed else ""),
                         xalign=1.0, ellipsize=False))
        col.append(top)
        col.append(label(m.get("subject", ""), "subject"))
        sn = label(m.get("snippet", ""), "snippet")
        col.append(sn)
        self.snippet_label = sn
        box.append(col)
        return box

    def set_snippet(self, text):
        self.snippet_label.set_label(text)

    def _avatar_tap(self, g, n, x, y):
        g.set_state(Gtk.EventSequenceState.CLAIMED)  # no row activation (= open) for this tap
        self.emit("avatar-tapped")

    def set_picked(self, picked):
        self.avatar.set_label("✓" if picked else self.initial)
        for w in (self.avatar, self.fg):
            (w.add_css_class if picked else w.remove_css_class)("picked")

    # layout -------------------------------------------------------------
    def do_measure(self, orientation, for_size):
        return self.fg.measure(orientation, for_size)

    def do_size_allocate(self, w, h, baseline):
        self.under.allocate(w, h, baseline, None)
        t = Gsk.Transform().translate(Graphene.Point().init(self.offset, 0))
        self.fg.allocate(w, h, baseline, t)

    def do_dispose(self):
        for c in (getattr(self, "under", None), getattr(self, "fg", None)):
            if c is not None and c.get_parent() is self:
                c.unparent()
        Gtk.Widget.do_dispose(self)

    # gesture ------------------------------------------------------------
    def _set_offset(self, x):
        if self.left_only:
            x = min(x, 0)  # sent / spam / trash only swipe left
        self.offset = x
        w = max(1, self.get_width())
        self.left_hint.set_visible(x > 0)
        self.right_hint.set_visible(x < 0)
        for c in ("later", "trash"):
            self.under.remove_css_class(c)
        if x > 0:
            self.under.add_css_class("later")
        elif x < 0:
            self.under.add_css_class("trash")
        armed = (1 if x > 0 else -1) if abs(x) >= w * SWIPE_COMMIT else 0
        if armed != self.armed:
            self.armed = armed
            if armed:
                self.under.add_css_class("armed")
                buzz()
            else:
                self.under.remove_css_class("armed")
        self.queue_allocate()

    def _begin(self, g, x, y):
        if self.anim:
            self.anim.skip()
        self.swiping = False

    def _update(self, g, dx, dy):
        if not self.swipe_enabled:
            g.set_state(Gtk.EventSequenceState.DENIED)
            return
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
            direction = 1 if self.offset > 0 else -1
            self.animate_to(direction * w, lambda: self.emit("swiped", direction))
        else:
            self.animate_to(0)

    def animate_to(self, x, done=None):
        target = Adw.CallbackAnimationTarget.new(lambda v: self._set_offset(v))
        self.anim = Adw.TimedAnimation.new(self, self.offset, x, 180, target)
        self.anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        if done:
            self.anim.connect("done", lambda *_: done())
        self.anim.play()

    def reset(self):
        self.anim = None
        self._set_offset(0)


# --- snooze picker -------------------------------------------------------

class SnoozeDialog(Adw.Dialog):
    """Bottom sheet with Gmail-style quick picks; callback(ts or None)."""

    def __init__(self, callback):
        super().__init__(title="Remind me", content_width=420)
        self.callback = callback
        self.chosen = None
        self.connect("closed", lambda *_: self.callback(self.chosen))
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_title_widget(label("REMIND ME", "sheet-title", xalign=0.5))
        tv.add_top_bar(hb)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT)
        tv.set_content(self.stack)
        self.set_child(tv)

        lb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        lb.add_css_class("snooze-list")
        for text, when in snooze_options():
            lb.append(self._option(text, when.strftime("%a %H:%M"),
                                   lambda *_, w=when: self._pick(w)))
        lb.append(self._option("Pick date & time", "…", lambda *_: self.stack.set_visible_child_name("pick")))
        self.stack.add_named(lb, "quick")
        self.stack.add_named(self._picker(), "pick")

    def _option(self, text, right, cb):
        b = Gtk.Button()
        b.add_css_class("flat")
        b.add_css_class("snooze-opt")
        row = Gtk.Box(spacing=8)
        row.append(label(text, "opt-text", ellipsize=False))
        row.get_first_child().set_hexpand(True)
        row.append(label(right, "opt-time", xalign=1.0, ellipsize=False))
        b.set_child(row)
        b.connect("clicked", cb)
        return b

    def _picker(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=16,
                      margin_end=16, margin_bottom=16)
        tomorrow = GLib.DateTime.new_now_local().add_days(1)
        self.cal = Gtk.Calendar()
        self.cal.select_day(tomorrow)
        box.append(self.cal)
        tbox = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self.hour = Gtk.SpinButton.new_with_range(0, 23, 1)
        self.hour.set_value(8)
        self.minute = Gtk.SpinButton.new_with_range(0, 55, 5)
        self.minute.set_value(0)
        for sp in (self.hour, self.minute):
            sp.set_orientation(Gtk.Orientation.VERTICAL)
            sp.set_wrap(True)
            sp.connect("output", self._two_digits)
        tbox.append(self.hour)
        tbox.append(label(":", "opt-time", xalign=0.5, ellipsize=False))
        tbox.append(self.minute)
        box.append(tbox)
        ok = Gtk.Button(label="SET")
        ok.add_css_class("primary")
        ok.connect("clicked", self._pick_custom)
        box.append(ok)
        return box

    @staticmethod
    def _two_digits(sp):
        sp.set_text("%02d" % sp.get_value_as_int())
        return True

    def _pick_custom(self, *_):
        d = self.cal.get_date()
        when = datetime.datetime(d.get_year(), d.get_month(), d.get_day_of_month(),
                                 self.hour.get_value_as_int(), self.minute.get_value_as_int())
        if when <= datetime.datetime.now():
            self.cal.add_css_class("error")
            return
        self._pick(when)

    def _pick(self, when):
        self.chosen = when.timestamp()
        self.close()


# --- message view --------------------------------------------------------

# HTML mail is written for white paper (black default text); give it a light page from the
# active theme so it stays readable (vapor keeps its pale lavender). Mails that set their own
# colours keep them. Plain text / translations use the theme's own background and ink.
def paper():
    if notareal_theme.active() == "vapor":
        return "#f4f1ff"
    r, g, b = notareal_theme.rgb("bg")
    light = 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.5
    return notareal_theme.hex("surface", "#ffffff") if light else "#ffffff"


def reader_css():
    return """
:root { color-scheme: light; background: %s; color: #000; }
body { margin: 0; padding: 12px; font-family: sans-serif; overflow-wrap: anywhere; }
img { max-width: 100%%; height: auto; }
table { max-width: 100%% !important; }
""" % paper()


def plain_css():
    t = notareal_theme
    q = "rgba(%d,%d,%d,.70)" % tuple(round(c * 255) for c in t.rgb("accent"))
    return """
body { margin: 0; padding: 14px 16px; background: %s; color: %s;
       font: 15px/1.45 "JetBrains Mono", monospace; overflow-wrap: anywhere; }
pre { white-space: pre-wrap; font: inherit; margin: 0; }
a { color: %s; }
.q { color: %s; }
""" % (t.hex("bg"), t.hex("fg"), t.hex("primary"), q)


def rgba(spec):
    c = Gdk.RGBA()
    c.parse(spec)
    return c


def plain_html(text):
    esc = GLib.markup_escape_text(text or "")
    import re
    esc = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', esc)
    lines = ['<span class="q">%s</span>' % l if l.lstrip().startswith("&gt;") else l
             for l in esc.split("\n")]
    return ("<html><head><meta name=viewport content='width=device-width'><style>%s</style></head>"
            "<body><pre>%s</pre></body></html>" % (plain_css(), "\n".join(lines)))


class MessagePage(Adw.NavigationPage):
    def __init__(self, win, msg, snoozed=False, lst=None):
        super().__init__(title=msg.get("subject", ""))
        self.win, self.msg, self.snoozed, self.lst = win, msg, snoozed, lst
        sent = lst is not None and lst.sent
        self.folder = lst.folder if lst is not None else None
        self.parsed = None
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_title_widget(Gtk.Box())
        # left-handed: Reply sits next to the back arrow, Trash farthest from the thumb
        for icon, tip, cb in (("mail-reply-sender-symbolic", "Reply", lambda *_: self._reply("one")),
                              ("mail-reply-all-symbolic", "Reply all", lambda *_: self._reply("all")),
                              ("mail-forward-symbolic", "Forward", lambda *_: self._reply("fwd")),
                              ("preferences-desktop-locale-symbolic", "Translate", self._translate),
                              ("alarm-symbolic", "Remind me later", self._snooze)):
            if (sent or self.folder in RESTORE_HINT) and icon == "alarm-symbolic":
                continue
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.add_css_class("flat")
            b.connect("clicked", cb)
            hb.pack_start(b)
            if tip == "Translate":
                self.tr_button = b
        if self.folder in RESTORE_HINT:   # spam / trash: the way out is back to the inbox
            b = Gtk.Button(icon_name="mail-inbox-symbolic",
                           tooltip_text="Not spam" if self.folder == "spam" else "Restore to inbox")
            b.connect("clicked", self._restore)
        else:
            b = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Trash")
            b.connect("clicked", self._trash)
        b.add_css_class("flat")
        hb.pack_end(b)
        tv.add_top_bar(hb)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        head.add_css_class("read-head")
        self.subject = label(msg.get("subject", ""), "read-subject", ellipsize=False, wrap=True)
        head.append(self.subject)
        self.translated = None  # (subject, body, source language) once fetched
        self.showing_tr = False
        who = "%s  <%s>" % (msg.get("from_name", ""), msg.get("from_addr", ""))
        head.append(label(who, "read-from"))
        to = msg.get("to", "")
        if msg.get("cc"):
            to += "  cc " + msg["cc"]
        head.append(label("to " + to, "read-to"))
        stamp = datetime.datetime.fromtimestamp(msg.get("date", time.time())).strftime("%a %-d %b %Y  %H:%M")
        if snoozed:
            stamp += "   ⏰ " + due_text(msg["due"])
        if msg.get("acct", "gmail") != "gmail":
            stamp += "   ·   " + msg["acct"].split(":", 1)[-1]
        head.append(label(stamp, "read-date"))
        self.att_box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3,
                                   visible=False)
        head.append(self.att_box)
        box.append(head)

        self.web = WebKit.WebView(vexpand=True)
        s = self.web.get_settings()
        s.set_enable_javascript(False)
        s.set_enable_developer_extras(False)
        self.web.set_background_color(notareal_theme.color("bg"))
        self.web.connect("decide-policy", self._policy)
        self.web.connect("load-changed", self._load_changed)
        self.spinner = Adw.Spinner(height_request=40, vexpand=True)
        self.body = Gtk.Stack(vexpand=True)
        self.body.add_named(self.spinner, "wait")
        self.body.add_named(self.web, "web")
        box.append(self.body)
        tv.set_content(box)
        self.set_child(tv)

        bg(lambda: gmail.parse(win.acct(msg).get_raw(msg)), self._loaded, self._failed)

    def _loaded(self, parsed):
        self.parsed = parsed
        self._loaded_body()
        # keep the spinner until WebKit has drawn something (its first start takes a few s)
        GLib.timeout_add_seconds(8, lambda: (self.body.set_visible_child_name("web"), False)[1])
        for name, ctype, data in parsed["attachments"]:
            row = Gtk.Box(spacing=4)
            # left-handed: the "save to a folder" button sits left of the name
            sv = Gtk.Button(icon_name="document-save-symbolic", tooltip_text="Save to a folder")
            sv.add_css_class("attachment")
            sv.connect("clicked", lambda _b, n=name, d=data: self._save_attachment_as(n, d))
            row.append(sv)
            b = Gtk.Button(label="📎 " + name)
            b.add_css_class("attachment")
            b.get_child().set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            b.connect("clicked", lambda _b, n=name, d=data: self._open_attachment(n, d))
            row.append(b)
            self.att_box.append(row)
        self.att_box.set_visible(bool(parsed["attachments"]))
        if not self.msg.get("seen", True):
            self.msg["seen"] = True
            self.win.inbox.refresh_row(self.msg)
            bg(lambda: self.win.acct(self.msg).mark_seen(self.msg["msgid"]))

    def show_original(self):
        self.subject.set_label(self.msg.get("subject", ""))
        self._loaded_body()

    def _loaded_body(self):
        p = self.parsed
        if p["html"]:
            self.web.set_background_color(rgba(paper()))
            self.web.load_html("<meta name=viewport content='width=device-width'><style>%s</style>%s"
                               % (reader_css(), p["html"]), "about:blank")
        else:
            self.web.set_background_color(notareal_theme.color("bg"))
            self.web.load_html(plain_html(p["plain"] or ""), "about:blank")

    def _translate(self, *_):
        """Tap = translate to English (plain text); tap again = back to the original."""
        if self.parsed is None:
            return
        if self.showing_tr:
            self.showing_tr = False
            self.tr_button.remove_css_class("accent")
            return self.show_original()
        if self.translated:
            return self._show_translation(self.translated)
        text = self.parsed["plain"] or gmail.html_to_text(self.parsed["html"] or "")
        self.tr_button.set_sensitive(False)
        self.win.toast("Translating…")

        def done(res):
            self.tr_button.set_sensitive(True)
            (subj, body), src = res
            self.translated = (subj, body, src)
            self._show_translation(self.translated)

        def fail(e):
            self.tr_button.set_sensitive(True)
            self.win.toast("Translate failed: %s" % e)

        bg(lambda: translate.translate([self.msg.get("subject", ""), text], "en"), done, fail)

    def _show_translation(self, tr):
        subj, body, src = tr
        self.showing_tr = True
        self.tr_button.add_css_class("accent")
        self.subject.set_label(subj)
        self.web.set_background_color(notareal_theme.color("bg"))
        note = "[ translated from %s · tap translate again for the original ]\n\n" % (src or "?")
        self.web.load_html(plain_html(note + body), "about:blank")

    def _load_changed(self, web, event):
        if event in (WebKit.LoadEvent.COMMITTED, WebKit.LoadEvent.FINISHED):
            self.body.set_visible_child_name("web")

    def _failed(self, e):
        self.body.set_visible_child_name("web")
        self.web.load_html(plain_html("Couldn't load this mail:\n%s" % e), "about:blank")

    def _policy(self, web, decision, kind):
        if kind == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri and not uri.startswith(("about:", "data:")):
                decision.ignore()
                open_link(uri, self.win)
                return True
        elif kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            decision.ignore()
            if uri:
                open_link(uri, self.win)
            return True
        return False

    def _open_attachment(self, name, data):
        d = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or os.path.expanduser("~/Downloads")
        os.makedirs(d, exist_ok=True)
        base, ext = os.path.splitext(os.path.basename(name))
        path, n = os.path.join(d, base + ext), 1
        while os.path.exists(path):
            path, n = os.path.join(d, "%s (%d)%s" % (base, n, ext)), n + 1
        with open(path, "wb") as f:
            f.write(data)
        self.win.toast("Saved to Downloads: " + os.path.basename(path))
        Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(self.win, None, None, None)

    def _save_attachment_as(self, name, data):
        cfg = gmail.load_config()
        folder = cfg.get("save_dir")
        if not folder or not os.path.isdir(folder):
            folder = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or os.path.expanduser("~")
        dlg = Gtk.FileDialog(title="Save attachment", initial_name=os.path.basename(name),
                             initial_folder=Gio.File.new_for_path(folder))

        def done(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return  # cancelled
            path = f.get_path()
            try:
                with open(path, "wb") as fh:
                    fh.write(data)
            except OSError as e:
                self.win.toast("Couldn't save: " + e.strerror)
                return
            cfg = gmail.load_config()
            cfg["save_dir"] = os.path.dirname(path)
            gmail.save_config(cfg)
            self.win.toast("Saved: " + path.replace(os.path.expanduser("~"), "~", 1))
        dlg.save(self.win, None, done)

    def _trash(self, *_):
        self.win.nav.pop()
        self.win.trash(self.msg, self.snoozed, self.lst)

    def _restore(self, *_):
        self.win.nav.pop()
        self.win.restore(self.msg, self.lst)

    def _snooze(self, *_):
        def chosen(ts):
            if ts:
                self.win.nav.pop()
                self.win.snooze(self.msg, ts, self.snoozed)
        self.win.present_snooze(chosen)

    def _reply(self, mode):
        self.win.compose(reply_to=self.msg, mode=mode, parsed=self.parsed)


# --- compose -------------------------------------------------------------

class ComposePage(Adw.NavigationPage):
    def __init__(self, win, to="", cc="", subject="", body="", in_reply_to=None, references=None,
                 focus_body=False, account=None):
        super().__init__(title="Compose")
        self.win = win
        self.accounts = win.account_list()
        self.account = account or win.mail
        self.in_reply_to, self.references = in_reply_to, references
        self.attachments = []
        self.sent = False
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_title_widget(label("COMPOSE", "sheet-title", xalign=0.5))
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Discard")
        close.add_css_class("flat")
        close.connect("clicked", lambda *_: self.leave())
        hb.pack_end(close)  # left-handed: Send on the left, close out of the thumb's way
        att = Gtk.Button(icon_name="mail-attachment-symbolic", tooltip_text="Attach")
        att.add_css_class("flat")
        att.connect("clicked", self._attach)
        self.send_btn = Gtk.Button(icon_name="mail-send-symbolic", tooltip_text="Send")
        self.send_btn.add_css_class("send")
        self.send_btn.connect("clicked", self._send)
        hb.pack_start(self.send_btn)
        hb.pack_start(att)
        tv.add_top_bar(hb)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("compose")
        self.suggests = []
        if len(self.accounts) > 1:   # which mailbox it goes out from
            row = Gtk.Box(spacing=8)
            row.add_css_class("field")
            row.append(label("FROM", "field-name", ellipsize=False))
            self.from_dd = Gtk.DropDown.new_from_strings([a.address for a in self.accounts])
            self.from_dd.add_css_class("from-pick")
            self.from_dd.set_hexpand(True)
            self.from_dd.set_selected(self.accounts.index(self.account) if self.account in self.accounts else 0)
            self.from_dd.connect("notify::selected",
                                 lambda dd, _p: setattr(self, "account", self.accounts[dd.get_selected()]))
            row.append(self.from_dd)
            box.append(row)
        self.to = self._field(box, "TO", to, Gtk.InputPurpose.EMAIL, suggest=True)
        self.cc = self._field(box, "CC", cc, Gtk.InputPurpose.EMAIL, suggest=True)
        self.subject = self._field(box, "RE", subject, Gtk.InputPurpose.FREE_FORM)
        self.att_box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, visible=False,
                                   max_children_per_line=3)
        box.append(self.att_box)
        self.text = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, vexpand=True,
                                 top_margin=12, left_margin=16, right_margin=16, bottom_margin=24)
        self.text.add_css_class("body")
        self.text.set_input_hints(Gtk.InputHints.SPELLCHECK | Gtk.InputHints.WORD_COMPLETION
                                  | Gtk.InputHints.UPPERCASE_SENTENCES)
        buf = self.text.get_buffer()
        buf.set_text(body)
        buf.place_cursor(buf.get_start_iter())
        self.initial = (to, cc, subject, body)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: self._hide_suggests())
        self.text.add_controller(focus)
        sc = Gtk.ScrolledWindow(vexpand=True, child=self.text)
        box.append(sc)
        tv.set_content(box)
        self.set_child(tv)
        self.set_can_pop(False)  # Adw's swipe-back would drop a draft; _swipe asks first

        # swipe left-to-right anywhere = close (asks Discard / Continue when there's a draft)
        drag = Gtk.GestureDrag(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        drag.connect("drag-begin", lambda *_: setattr(self, "_swiping", None))
        drag.connect("drag-update", self._swipe_update)
        drag.connect("drag-end", self._swipe_end)
        self.add_controller(drag)
        self.connect("showing", lambda *_: (self.text if focus_body else self.to).grab_focus())

    def _field(self, box, name, value, purpose, suggest=False):
        row = Gtk.Box(spacing=10)
        row.add_css_class("field")
        row.append(label(name, "field-name", ellipsize=False))
        e = Gtk.Entry(text=value, hexpand=True, input_purpose=purpose)
        e.add_css_class("flat")
        row.append(e)
        box.append(row)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: self._hide_suggests(keep=getattr(e, "suggest", None)))
        e.add_controller(focus)
        if suggest:
            lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, visible=False)
            lb.add_css_class("suggest")
            lb.connect("row-activated", self._suggest_picked, e)
            e.suggest = lb
            e.connect("changed", self._suggest_update)
            e.connect("activate", lambda *_: self._suggest_first(e))
            box.append(lb)
            self.suggests.append(lb)
        return e

    # address suggestions -------------------------------------------------
    @staticmethod
    def _split(text):
        """'a@x, Bo <b@y>, ca' -> ('a@x, Bo <b@y>, ', 'ca')"""
        i = text.rfind(",")
        return (text[:i + 1] + " ", text[i + 1:].strip()) if i >= 0 else ("", text.strip())

    def _suggest_update(self, e):
        lb = e.suggest
        lb.remove_all()
        head, token = self._split(e.get_text())
        have = [a for _n, a in email.utils.getaddresses([head])]
        hits = self.win.book.match(token, exclude=have) if token else []
        for name, addr in hits:
            r = Gtk.Box(spacing=10)
            r.add_css_class("suggest-row")
            r.append(label(name or addr, "suggest-name"))
            if name:
                r.append(label(addr, "suggest-addr", xalign=1.0))
                r.get_last_child().set_hexpand(True)
            row = Gtk.ListBoxRow(child=r)
            row.contact = (name, addr)
            lb.append(row)
        lb.set_visible(bool(hits))

    def _suggest_picked(self, lb, row, e):
        head, _token = self._split(e.get_text())
        e.set_text(head + email.utils.formataddr(row.contact) + ", ")
        e.set_position(-1)
        lb.set_visible(False)
        e.grab_focus_without_selecting()

    def _suggest_first(self, e):
        row = e.suggest.get_row_at_index(0)
        if row and e.suggest.get_visible():
            self._suggest_picked(e.suggest, row, e)
        else:
            e.emit("move-focus", Gtk.DirectionType.TAB_FORWARD)

    def _hide_suggests(self, keep=None):
        for lb in self.suggests:
            if lb is not keep:
                lb.set_visible(False)

    # swipe to close --------------------------------------------------------
    def _swipe_update(self, g, dx, dy):
        if self._swiping is None:
            if dx > 24 and dx > abs(dy) * 1.6:
                self._swiping = True
                g.set_state(Gtk.EventSequenceState.CLAIMED)
            elif abs(dx) > 24 or abs(dy) > 24:
                self._swiping = False  # a scroll or a leftward drag: not ours
                g.set_state(Gtk.EventSequenceState.DENIED)
            return
        if self._swiping:
            far = dx > self.get_width() * 0.25
            if far != ("swipe-armed" in self.get_css_classes()):
                (self.add_css_class if far else self.remove_css_class)("swipe-armed")
                if far:
                    buzz()

    def _swipe_end(self, g, dx, dy):
        armed = "swipe-armed" in self.get_css_classes()
        self.remove_css_class("swipe-armed")
        if self._swiping and armed:
            self.leave()
        self._swiping = None

    def leave(self):
        if self.sent or not self.dirty():
            self.sent = True
            self.set_can_pop(True)
            self.win.nav.pop()
            return
        dlg = Adw.AlertDialog(heading="Discard this mail?", body="It hasn't been sent.")
        dlg.add_response("continue", "Continue")
        dlg.add_response("discard", "Discard")
        dlg.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("continue")
        dlg.set_close_response("continue")
        dlg.connect("response", lambda d, r: r == "discard" and (setattr(self, "sent", True),
                                                                  self.leave()))
        dlg.present(self.win)

    def body_text(self):
        buf = self.text.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def dirty(self):
        now = (self.to.get_text(), self.cc.get_text(), self.subject.get_text(), self.body_text())
        return not self.sent and (now != self.initial or bool(self.attachments))

    def _attach(self, *_):
        dlg = Gtk.FileDialog(title="Attach files")
        dlg.open_multiple(self.win, None, self._attached)

    def _attached(self, dlg, res):
        try:
            files = dlg.open_multiple_finish(res)
        except GLib.Error:
            return
        for i in range(files.get_n_items()):
            self.add_attachment(files.get_item(i).get_path())

    def add_attachment(self, path):
        if path and path not in self.attachments:
            self.attachments.append(path)
            b = Gtk.Button(label="📎 %s  ✕" % os.path.basename(path))
            b.add_css_class("attachment")
            b.connect("clicked", self._unattach, path)
            self.att_box.append(b)
        self.att_box.set_visible(bool(self.attachments))

    def _unattach(self, b, path):
        self.attachments.remove(path)
        self.att_box.remove(b.get_parent())
        self.att_box.set_visible(bool(self.attachments))

    def _send(self, *_):
        to = self.to.get_text().strip()
        if "@" not in to:
            self.to.add_css_class("error")
            self.to.grab_focus()
            return
        self.send_btn.set_sensitive(False)
        self.send_btn.set_child(Adw.Spinner())
        args = (to, self.cc.get_text().strip(), self.subject.get_text().strip(), self.body_text(),
                self.in_reply_to, self.references, list(self.attachments))

        def ok(_):
            self.sent = True
            self.set_can_pop(True)
            self.win.nav.pop()
            self.win.toast("Sent ✓")
            GLib.timeout_add_seconds(2, lambda: (self.win.refresh_sent(), False)[1])

        def fail(e):
            self.send_btn.set_child(None)
            self.send_btn.set_icon_name("mail-send-symbolic")
            self.send_btn.set_sensitive(True)
            self.win.toast(str(e))
        acct = self.account
        bg(lambda: acct.send(*args), ok, fail)


# --- mail lists ----------------------------------------------------------

class MailList(Gtk.Box):
    def __init__(self, win, snoozed=False, sent=False, folder=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.snoozed, self.sent = win, snoozed, sent
        self.folder = folder or ("sent" if sent else None)   # sent / spam / trash list from the server
        self.rows = {}          # msgid -> ListBoxRow
        self.msgs = []
        self.picked = set()     # msgids in multi-select
        self.lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, activate_on_single_click=True)
        self.lb.add_css_class("mail-list")
        self.lb.set_margin_bottom(84)  # room to scroll the last mail out from under COMPOSE
        self.lb.connect("row-activated", self._activated)
        self.empty = label("✦ nothing snoozed ✦" if snoozed else "✦ nothing sent ✦" if sent
                           else "✦ no spam ✦" if folder == "spam" else "✦ trash is empty ✦"
                           if folder == "trash" else "✦ inbox zero ✦", "empty",
                           xalign=0.5, ellipsize=False)
        self.empty.set_vexpand(True)
        self.stack = Gtk.Stack(vexpand=True)
        self.sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.lb)
        self.sc.connect("edge-overshot", self._overshot)
        self.stack.add_named(self.sc, "list")
        self.stack.add_named(self.empty, "empty")
        self.append(self.stack)

    FIRST_ROWS, ROW_CHUNK = 30, 40   # 10-13: 580 rows up front took ~2 s of a cold start

    def set_msgs(self, msgs):
        """First FIRST_ROWS rows now (what the screen shows), the rest in idle chunks."""
        self.msgs = list(msgs)
        self.lb.remove_all()
        self.rows = {}
        self._fill_gen = getattr(self, "_fill_gen", 0) + 1
        for m in self.msgs[:self.FIRST_ROWS]:
            self._add_row(m)
        self.picked &= {m["msgid"] for m in self.msgs}
        self._update_empty()
        self._picked_changed()
        if len(self.msgs) > self.FIRST_ROWS:
            GLib.idle_add(self._fill_rest, self._fill_gen, self.msgs[self.FIRST_ROWS:],
                          priority=GLib.PRIORITY_LOW)

    def _fill_rest(self, gen, pending):
        if gen != self._fill_gen:          # a newer set_msgs replaced the list
            return False
        for m in pending[:self.ROW_CHUNK]:
            # skip mails taken (swiped away) or already re-added while we were waiting
            if m["msgid"] not in self.rows and m in self.msgs:
                self._add_row(m)
        if len(pending) > self.ROW_CHUNK:
            GLib.idle_add(self._fill_rest, gen, pending[self.ROW_CHUNK:], priority=GLib.PRIORITY_LOW)
        return False

    def _add_row(self, m, position=-1):
        sr = SwipeRow(m, self.snoozed, self.sent, RESTORE_HINT.get(self.folder))
        sr.connect("swiped", self._swiped)
        sr.connect("avatar-tapped", lambda r: self.toggle(r.msg))
        if m["msgid"] in self.picked:
            sr.set_picked(True)
        sr.swipe_enabled = not self.picked
        row = Gtk.ListBoxRow(child=sr)
        row.msg = m
        self.lb.insert(row, position)
        self.rows[m["msgid"]] = row
        return row

    def _update_empty(self):
        self.stack.set_visible_child_name("list" if self.rows else "empty")

    def refresh_row(self, m):
        row = self.rows.get(m["msgid"])
        if row:
            idx = row.get_index()
            self.lb.remove(row)
            self._add_row(m, idx)

    def set_snippet(self, m):
        row = self.rows.get(m["msgid"])
        if row:
            row.get_child().set_snippet(m.get("snippet", ""))

    def take(self, m):
        """Remove a mail's row; returns a function that puts it back (for Undo)."""
        row = self.rows.pop(m["msgid"], None)
        idx = row.get_index() if row else -1
        if row:
            self.lb.remove(row)
        if m in self.msgs:
            self.msgs.remove(m)
        self.picked.discard(m["msgid"])
        self._update_empty()

        def put_back():
            if m["msgid"] in self.rows:
                return
            pos = min(idx, len(self.msgs)) if idx >= 0 else 0
            self.msgs.insert(pos, m)
            self._add_row(m, pos)
            self._update_empty()
        return put_back

    def _activated(self, lb, row):
        if self.picked:  # selecting: a tap anywhere on the row toggles it
            self.toggle(row.msg)
        else:
            self.win.open_message(row.msg, self.snoozed, self)

    # multi-select ----------------------------------------------------------
    def toggle(self, m):
        mid = m["msgid"]
        if mid in self.picked:
            self.picked.discard(mid)
        else:
            self.picked.add(mid)
        row = self.rows.get(mid)
        if row:
            row.get_child().set_picked(mid in self.picked)
        self._picked_changed()

    def pick_all(self):
        everything = set(self.rows) - self.picked
        self.picked = set(self.rows) if everything else set()
        for mid, row in self.rows.items():
            row.get_child().set_picked(mid in self.picked)
        self._picked_changed()

    def clear_picked(self):
        for mid in self.picked:
            row = self.rows.get(mid)
            if row:
                row.get_child().set_picked(False)
        self.picked = set()
        self._picked_changed()

    def picked_msgs(self):
        return [m for m in self.msgs if m["msgid"] in self.picked]

    def _picked_changed(self):
        for row in self.rows.values():
            row.get_child().swipe_enabled = not self.picked
        self.win.selection_changed(self)

    def _swiped(self, sr, direction):
        m = sr.msg
        if direction > 0:
            def chosen(ts):
                if ts:
                    self.win.snooze(m, ts, self.snoozed)
                else:
                    sr.animate_to(0)
            self.win.present_snooze(chosen)
        elif self.folder in RESTORE_HINT:
            self.win.restore(m, self)
        elif self.snoozed:
            self.win.unsnooze_now(m)
        else:
            self.win.trash(m, False, self)

    def _overshot(self, sc, pos):
        if pos == Gtk.PositionType.TOP and self.folder:
            self.win.refresh_folder(self.folder)
        elif pos == Gtk.PositionType.TOP and not self.snoozed:
            self.win.refresh()


# --- sign in -------------------------------------------------------------

class SetupPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14, valign=Gtk.Align.CENTER,
                         margin_start=28, margin_end=28)
        self.win = win
        self.add_css_class("setup")
        self.append(label("NOTREAL.MAIL", "logo", xalign=0.5, ellipsize=False))
        self.append(label("Gmail address + app password\n(myaccount.google.com/apppasswords)",
                          "setup-hint", xalign=0.5, ellipsize=False, wrap=True))
        cfg = gmail.load_config()
        self.addr = Gtk.Entry(placeholder_text="you@gmail.com", text=cfg.get("address", ""),
                              input_purpose=Gtk.InputPurpose.EMAIL)
        self.pw = Gtk.PasswordEntry(placeholder_text="app password", show_peek_icon=True)
        self.pw.connect("activate", self._go)
        for e in (self.addr, self.pw):
            e.add_css_class("setup-entry")
            self.append(e)
        self.btn = Gtk.Button(label="CONNECT")
        self.btn.add_css_class("primary")
        self.btn.connect("clicked", self._go)
        self.append(self.btn)
        self.err = label("", "setup-error", xalign=0.5, ellipsize=False, wrap=True)
        self.append(self.err)

    def _go(self, *_):
        addr = self.addr.get_text().strip()
        pw = self.pw.get_text().replace(" ", "")  # Google shows it in groups of four
        if "@" not in addr or not pw:
            self.err.set_label("Both fields, please.")
            return
        self.btn.set_sensitive(False)
        self.err.set_label("connecting…")
        cfg = gmail.load_config()
        mail = gmail.Gmail(addr, pw, cfg.get("name"))

        def ok(_):
            gmail.store_password(addr, pw)
            cfg["address"] = addr
            gmail.save_config(cfg)
            self.win.signed_in(mail)
            subprocess.Popen(["pkill", "-USR1", "-f", "vapormaild.py"])

        def fail(e):
            self.btn.set_sensitive(True)
            self.err.set_label(str(e))
        bg(mail.check_login, ok, fail)


# --- window --------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="notreal.mail", default_width=540, default_height=1100)
        self.mail = None
        self.accounts = {}    # key -> Gmail / Graph backend
        self.loading = False
        self.pending = []     # [(toast, commit fn)] waiting for their Undo window to pass
        self.cfg = gmail.load_config()
        self.book = contacts.Book()
        bg(contacts.fetch, self.book.set_contacts)
        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)
        self.connect("close-request", self._close)

        mail = gmail.Gmail.from_config()
        if mail:
            self.signed_in(mail)
        else:
            page = Adw.NavigationPage(title="notreal.mail", child=SetupPage(self))
            self.nav.add(page)

    # main page ----------------------------------------------------------
    def acct(self, m):
        return self.accounts.get(m.get("acct") or "gmail") or self.mail

    def account_list(self):
        return [self.mail] + [a for k, a in self.accounts.items() if k != "gmail"]

    def _load_accounts(self):
        try:
            found = graph.goa_accounts()
        except Exception as e:  # noqa: BLE001 - no GOA: Gmail only
            print("goa:", e, flush=True)
            return []
        return [graph.Graph(ident, self.cfg.get("name")) for ident, _ in found]

    def _accounts_loaded(self, extra):
        for a in extra:
            self.accounts[a.key] = a
        if not extra:
            return
        if self.loading:
            self.refresh_again = True    # the Gmail-only fetch is still running
        else:
            self.refresh()
        name = self.stack.get_visible_child_name()
        if name in self.folder_lists:
            self.folder_loading.discard(name)
            self.refresh_folder(name)

    def signed_in(self, mail):
        self.mail = mail
        self.accounts = {"gmail": mail}
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.add_css_class("vapor-bar")
        hb.set_title_widget(label("NOTREAL.MAIL", "app-title", xalign=0.5, ellipsize=False))
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Fetch")
        self.refresh_btn.add_css_class("flat")
        self.refresh_btn.connect("clicked", lambda *_: self.refresh())
        hb.pack_start(self.refresh_btn)

        # selection header (left-handed: actions on the left, cancel on the right)
        sel = Adw.HeaderBar(show_back_button=False, show_end_title_buttons=False)
        sel.add_css_class("vapor-bar")
        sel.add_css_class("select-bar")
        self.sel_title = label("", "sel-count", xalign=0.5, ellipsize=False)
        sel.set_title_widget(self.sel_title)
        for icon, tip, cb in (("user-trash-symbolic", "Trash selected", lambda *_: self.trash_picked()),
                              ("mail-inbox-symbolic", "Back to the inbox", lambda *_: self.restore_picked()),
                              ("alarm-symbolic", "Snooze selected", lambda *_: self.snooze_picked()),
                              ("edit-select-all-symbolic", "Select all", lambda *_: self._cur_list().pick_all())):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.add_css_class("flat")
            b.connect("clicked", cb)
            sel.pack_start(b)
            if icon == "alarm-symbolic":
                self.sel_snooze = b
            elif icon == "user-trash-symbolic":
                self.sel_trash = b
            elif icon == "mail-inbox-symbolic":
                self.sel_restore = b
        cancel = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Done")
        cancel.add_css_class("flat")
        cancel.connect("clicked", lambda *_: self._cur_list().clear_picked())
        sel.pack_end(cancel)
        self.top = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        self.top.add_named(hb, "normal")
        self.top.add_named(sel, "select")
        tv.add_top_bar(self.top)

        self.stack = Adw.ViewStack()
        self.inbox = MailList(self)
        self.snoozed = MailList(self, snoozed=True)
        self.sent = MailList(self, sent=True)
        self.spam = MailList(self, folder="spam")
        self.trashed = MailList(self, folder="trash")
        self.folder_lists = {"sent": self.sent, "spam": self.spam, "trash": self.trashed}
        self.folder_loading = set()
        self.stack.add_titled_with_icon(self.inbox, "inbox", "INBOX", "mail-inbox-symbolic")
        self.stack.add_titled_with_icon(self.snoozed, "snoozed", "SNOOZED", "alarm-symbolic")
        self.stack.add_titled_with_icon(self.sent, "sent", "SENT", "mail-send-symbolic")
        self.stack.add_titled_with_icon(self.spam, "spam", "SPAM", "dialog-warning-symbolic")
        self.stack.add_titled_with_icon(self.trashed, "trash", "TRASH", "user-trash-symbolic")
        self.stack.connect("notify::visible-child-name", self._tab_changed)

        ov = Gtk.Overlay(child=self.stack)
        fab = Gtk.Button(halign=Gtk.Align.START, valign=Gtk.Align.END, margin_start=18, margin_bottom=18)
        fab.set_child(label("✎ COMPOSE", "fab-label", ellipsize=False))
        fab.add_css_class("fab")
        fab.connect("clicked", lambda *_: self.compose())
        ov.add_overlay(fab)
        self.fab = fab
        tv.set_content(ov)
        bar = Adw.ViewSwitcherBar(stack=self.stack, reveal=True)
        tv.add_bottom_bar(bar)

        page = Adw.NavigationPage(title="notreal.mail", child=tv, tag="main")
        self.nav.replace([page])

        cached = gmail._read_json(gmail.INBOX_CACHE, [])
        if cached:
            self.inbox.set_msgs(cached)
        for f, lst in self.folder_lists.items():
            lst.set_msgs(gmail._read_json(folder_cache(f), []))
        self._load_snoozed()
        self.refresh()
        bg(self._load_accounts, self._accounts_loaded)

    def _cur_list(self):
        name = self.stack.get_visible_child_name()
        return self.snoozed if name == "snoozed" else self.folder_lists.get(name, self.inbox)

    def _tab_changed(self, *_):
        for lst in [self.inbox, self.snoozed] + list(self.folder_lists.values()):
            if lst.picked:
                lst.clear_picked()
        self._load_snoozed()
        name = self.stack.get_visible_child_name()
        if name in self.folder_lists:
            self.refresh_folder(name)

    def selection_changed(self, lst):
        if not hasattr(self, "top"):
            return
        if not hasattr(self, "fab"):
            return
        n = len(self._cur_list().picked)
        self.top.set_visible_child_name("select" if n else "normal")
        self.sel_title.set_label("%d SELECTED" % n)
        cur = self._cur_list()
        self.sel_snooze.set_visible(cur in (self.inbox, self.snoozed))
        self.sel_trash.set_visible(cur.folder not in RESTORE_HINT)
        self.sel_restore.set_visible(cur.folder in RESTORE_HINT)
        self.fab.set_visible(not n)

    def trash_picked(self):
        lst = self._cur_list()
        msgs = lst.picked_msgs()
        if not msgs:
            return
        puts = [lst.take(m) for m in msgs]
        lst.clear_picked()
        snoozed = lst is self.snoozed

        def commit():
            for a, ids in self._by_account(msgs):
                a.trash_many(ids)
            if snoozed:
                for m in msgs:
                    gmail.remove_snoozed(m["msgid"])
        self._deferred("Moved %d to Trash" % len(msgs), msgs, lambda: [p() for p in reversed(puts)], commit)

    def snooze_picked(self):
        lst = self._cur_list()
        msgs = lst.picked_msgs()
        if not msgs:
            return

        def chosen(ts):
            if not ts:
                return
            if lst is self.snoozed:  # new time for all of them
                for m in msgs:
                    m["due"] = ts
                    gmail.add_snoozed(m, ts)
                lst.clear_picked()
                self._load_snoozed()
                self.toast("%d snoozed until %s" % (len(msgs), due_text(ts)))
                return
            puts = [lst.take(m) for m in msgs]
            lst.clear_picked()

            def commit():
                for a, ids in self._by_account(msgs):
                    a.snooze_many(ids)
                for m in msgs:
                    gmail.add_snoozed(m, ts)
                GLib.idle_add(lambda: (self._load_snoozed(), False)[1])
            self._deferred("%d snoozed until %s" % (len(msgs), due_text(ts)), msgs,
                           lambda: [p() for p in reversed(puts)], commit)
        self.present_snooze(chosen)

    def _by_account(self, msgs):
        groups = {}
        for m in msgs:
            groups.setdefault(m.get("acct") or "gmail", []).append(m["msgid"])
        return [(self.accounts.get(k) or self.mail, ids) for k, ids in groups.items()]

    def restore_picked(self):
        lst = self._cur_list()
        msgs = lst.picked_msgs()
        if not msgs:
            return
        puts = [lst.take(m) for m in msgs]
        lst.clear_picked()

        def commit():
            for m in msgs:
                self.acct(m).to_inbox(m["msgid"], m.get("folder"))
            GLib.idle_add(lambda: (self.refresh(), False)[1])
        self._deferred("%d back in the inbox" % len(msgs), msgs, lambda: [p() for p in reversed(puts)], commit)

    def restore(self, m, lst):
        put_back = lst.take(m)

        def commit():
            self.acct(m).to_inbox(m["msgid"], m.get("folder"))
            GLib.idle_add(lambda: (self.refresh(), False)[1])
        self._deferred("Not spam — back in the inbox" if lst.folder == "spam" else "Back in the inbox",
                       m, put_back, commit)

    def _load_snoozed(self):
        items = sorted(gmail.load_snoozed(), key=lambda s: s["due"])
        self.snoozed.set_msgs(items)

    def _fetch_all(self, folder):
        """One folder from every account, merged newest first. Returns (msgs, errors, gmail validity)."""
        msgs, errors, validity = [], [], None
        for a in list(self.accounts.values()):
            try:
                v, got = a.list_folder(folder)
            except Exception as e:  # noqa: BLE001 - one broken account must not hide the others
                errors.append(str(e))
                continue
            if a.kind == "gmail":
                validity = v
                for m in got:
                    raw = gmail.cached_raw(m["msgid"])
                    if raw:
                        m["snippet"] = gmail.snippet(raw)
            msgs += got
        msgs.sort(key=lambda m: m.get("date", 0), reverse=True)
        return msgs, errors, validity

    def refresh(self):
        name = self.stack.get_visible_child_name()
        if name in self.folder_lists:
            return self.refresh_folder(name)
        if not self.mail or self.loading:
            return
        self.loading = True
        self.refresh_btn.set_child(Adw.Spinner())

        def done(res):
            msgs, errors, validity = res
            hidden = {m["msgid"] for e in self.pending for m in e["msgs"]}
            msgs = [m for m in msgs if m["msgid"] not in hidden]
            self.inbox.set_msgs(msgs)
            self.book.note_inbox(msgs)
            uids = [m["uid"] for m in msgs if m.get("acct", "gmail") == "gmail"]
            if uids:
                gmail.mark_listed(validity, max(uids))
            for k in self.accounts:
                if k != "gmail":
                    dates = [m["date"] for m in msgs if m.get("acct") == k]
                    if dates:
                        gmail.mark_listed_other(k, max(dates))
            self._loaded()
            for e in errors:
                self.toast(e)
            bg(lambda: self._prefetch(msgs, self.inbox, gmail.INBOX_CACHE))

        def fail(e):
            self._loaded()
            self.toast(str(e))
        bg(lambda: self._fetch_all("INBOX"), done, fail)

    def refresh_sent(self):
        self.refresh_folder("sent")

    def refresh_folder(self, folder):
        if not self.mail or folder in self.folder_loading:
            return
        self.folder_loading.add(folder)
        lst = self.folder_lists[folder]

        def done(res):
            self.folder_loading.discard(folder)
            msgs, errors, _v = res
            hidden = {m["msgid"] for e in self.pending for m in e["msgs"]}
            msgs = [m for m in msgs if m["msgid"] not in hidden]
            lst.set_msgs(msgs)
            for e in errors:
                self.toast(e)
            bg(lambda: self._prefetch(msgs, lst, folder_cache(folder)))

        def fail(e):
            self.folder_loading.discard(folder)
            self.toast(str(e))
        bg(lambda: self._fetch_all(folder), done, fail)

    def _prefetch(self, msgs, lst, cache):
        missing = [m for m in msgs if not m.get("snippet")]
        for a, _ids in self._by_account(missing):
            a.prefetch([m for m in missing if (m.get("acct") or "gmail") == a.key])
        for m in missing:
            GLib.idle_add(lambda m=m: (lst.set_snippet(m), False)[1])
        gmail._write_json(cache, msgs)
        keep = [s["msgid"] for s in gmail.load_snoozed()] + [m["msgid"] for m in msgs]
        for c in [gmail.INBOX_CACHE] + [folder_cache(f) for f in FOLDER_LISTS]:
            if c != cache:
                keep += [m["msgid"] for m in gmail._read_json(c, [])]
        gmail.prune_cache(keep)

    def _loaded(self):
        self.loading = False
        self.refresh_btn.set_child(None)
        self.refresh_btn.set_icon_name("view-refresh-symbolic")
        if getattr(self, "refresh_again", False):
            self.refresh_again = False
            GLib.idle_add(lambda: (self.refresh(), False)[1])

    # actions ------------------------------------------------------------
    def toast(self, text, undo=None, on_dismiss=None):
        t = Adw.Toast(title=GLib.markup_escape_text(text), timeout=UNDO_S if undo else 3,
                      priority=Adw.ToastPriority.HIGH)
        if undo:
            t.set_button_label("UNDO")
            t.connect("button-clicked", lambda *_: undo())
        if on_dismiss:
            t.connect("dismissed", lambda *_: on_dismiss())
        self.toasts.add_toast(t)
        return t

    def _deferred(self, text, msgs, put_back, commit):
        """Hide now, run commit() once the Undo window has passed (or a newer toast replaces it)."""
        entry = {"msgs": msgs if isinstance(msgs, list) else [msgs], "commit": commit, "done": False}

        def undo():
            entry["done"] = True
            put_back()

        def dismissed():
            if entry in self.pending:
                self.pending.remove(entry)
            if not entry["done"]:
                entry["done"] = True
                bg(commit, None, lambda e: (self.toast(str(e)), put_back()))
        self.pending.append(entry)
        self.toast(text, undo, dismissed)

    def trash(self, m, from_snoozed, lst=None):
        lst = lst or (self.snoozed if from_snoozed else self.inbox)
        put_back = lst.take(m)

        def commit():
            self.acct(m).trash(m["msgid"])
            if from_snoozed:
                gmail.remove_snoozed(m["msgid"])
        self._deferred("Moved to Trash", m, put_back, commit)

    def snooze(self, m, ts, from_snoozed):
        if from_snoozed:  # just a new time
            m["due"] = ts
            gmail.add_snoozed(m, ts)
            self._load_snoozed()
            self.toast("Snoozed until " + due_text(ts))
            return
        put_back = self.inbox.take(m)

        def commit():
            self.acct(m).snooze(m["msgid"])
            gmail.add_snoozed(m, ts)
            GLib.idle_add(lambda: (self._load_snoozed(), False)[1])
        self._deferred("Snoozed until " + due_text(ts), m, put_back, commit)

    def unsnooze_now(self, m):
        put_back = self.snoozed.take(m)

        def commit():
            self.acct(m).unsnooze(m["msgid"])
            gmail.remove_snoozed(m["msgid"])
            GLib.idle_add(lambda: (self.refresh(), False)[1])
        self._deferred("Back in the inbox", m, put_back, commit)

    def present_snooze(self, callback):
        SnoozeDialog(callback).present(self)

    def open_message(self, m, snoozed=False, lst=None):
        self.nav.push(MessagePage(self, m, snoozed, lst))

    def open_msgid(self, msgid):
        for lst in (self.inbox, self.snoozed):
            row = lst.rows.get(msgid)
            if row:
                self.open_message(row.msg, lst.snoozed)
                return
        # not listed yet (fresh mail): fetch the inbox, then open it
        def done(res):
            self.inbox.set_msgs(res[0])
            row = self.inbox.rows.get(msgid)
            if row:
                self.open_message(row.msg)
        bg(lambda: self._fetch_all("INBOX"), done, lambda e: self.toast(str(e)))

    def compose_with(self, paths):
        """A new mail with files attached (from the Share sheet)."""
        sig = self.cfg.get("signature", gmail.DEFAULT_SIGNATURE)
        page = ComposePage(self, body="\n\n" + sig)
        self.nav.push(page)
        for p in paths:
            page.add_attachment(p)

    def compose(self, reply_to=None, mode="one", parsed=None):
        sig = self.cfg.get("signature", gmail.DEFAULT_SIGNATURE)
        acct = self.acct(reply_to) if reply_to else self.mail
        if not reply_to:
            self.nav.push(ComposePage(self, body="\n\n" + sig))
            return
        m = reply_to
        orig = (parsed or {}).get("plain") or gmail.html_to_text((parsed or {}).get("html") or "")
        stamp = datetime.datetime.fromtimestamp(m.get("date", time.time())).strftime("%a %-d %b %Y %H:%M")
        subj = m.get("subject", "")
        if mode == "fwd":
            subj = subj if subj.lower().startswith(("fwd:", "fw:")) else "Fwd: " + subj
            body = ("\n\n%s\n\n---------- Forwarded message ----------\nFrom: %s <%s>\nDate: %s\n"
                    "Subject: %s\nTo: %s\n\n%s" % (sig, m.get("from_name", ""), m.get("from_addr", ""),
                                                   stamp, m.get("subject", ""), m.get("to", ""), orig.strip()))
            self.nav.push(ComposePage(self, subject=subj, body=body, account=acct))
            return
        subj = subj if subj.lower().startswith("re:") else "Re: " + subj
        to = m.get("reply_to") or email.utils.formataddr((m.get("from_name", ""), m.get("from_addr", "")))
        cc = ""
        if mode == "all":
            me = acct.address.lower()
            others = [a for a in email.utils.getaddresses([m.get("to", ""), m.get("cc", "")])
                      if a[1] and a[1].lower() != me and a[1].lower() != m.get("from_addr", "").lower()]
            cc = ", ".join(email.utils.formataddr(a) for a in others)
        quoted = "\n".join("> " + l for l in orig.strip().splitlines())
        body = "\n\n%s\n\nOn %s, %s wrote:\n%s" % (sig, stamp, m.get("from_name", ""), quoted)
        self.nav.push(ComposePage(self, to=to, cc=cc, subject=subj, body=body,
                                  in_reply_to=m.get("message_id"), references=m.get("references"),
                                  focus_body=True, account=acct))

    # closing ------------------------------------------------------------
    def _close(self, *_):
        page = self.nav.get_visible_page()
        if isinstance(page, ComposePage) and page.dirty():
            dlg = Adw.AlertDialog(heading="Discard this mail?", body="It hasn't been sent.")
            dlg.add_response("continue", "Continue")
            dlg.add_response("discard", "Discard")
            dlg.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_close_response("continue")
            dlg.connect("response", lambda d, r: r == "discard" and (setattr(page, "sent", True), self.close()))
            dlg.present(self)
            return True
        live = [e for e in self.pending if not e["done"]]
        if live:
            # flush the Undo queue before quitting: the swipes must still happen
            for e in live:
                e["done"] = True
            commits = [e["commit"] for e in live]
            self.pending.clear()
            app = self.get_application()
            app.hold()
            self.set_visible(False)

            def run():
                for c in commits:
                    try:
                        c()
                    except Exception as e:  # noqa: BLE001
                        print("commit failed:", e, flush=True)
                GLib.idle_add(lambda: (app.release(), app.quit(), False)[2])
            threading.Thread(target=run, daemon=True).start()
            return True
        return False


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
        if "--attach" in args:   # nrshare: new mail with these files attached
            paths = [p for p in args[args.index("--attach") + 1:] if os.path.isfile(p)]
            GLib.timeout_add(300, lambda: (self.win.compose_with(paths), False)[1])
        if "--open" in args:
            i = args.index("--open")
            if i + 1 < len(args) and self.win.mail:
                msgid = args[i + 1]
                GLib.timeout_add(300, lambda: (self.win.open_msgid(msgid), False)[1])
        return 0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    sys.exit(App().run(sys.argv))
