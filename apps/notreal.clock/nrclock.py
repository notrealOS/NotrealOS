#!/usr/bin/env python3
"""notreal.clock: alarms, a countdown timer and a stopwatch in the shared notareal theme (10-19).

Nothing here has to keep running: alarmd (~/alarm) rings everything, also from deep sleep.
- ALARM: entries in alarmd's alarms.json with "hour", "minute", "repeat" [0=Mon..6] and "off".
  alarmd moves a repeating alarm to its next day after ringing and switches a one-off one off.
  Voice alarms ("wake me up at 7") show up here too.
- TIMER: a running countdown is an alarmd alarm with "kind": "timer"; pause/total live in
  ~/.local/share/nrclock/state.json. Voice timers ("Timer · …") are listed under it.
- STOPWATCH: wall-clock start + accumulated time + laps in the same state file, so it keeps
  running with the app closed.
Left-handed: new alarm (+), START / PAUSE and SAVE sit on the left; delete / reset on the right.
"""

import json
import math
import os
import sys
import time
import uuid

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402

import notareal_theme  # noqa: E402

sys.path.insert(0, os.path.expanduser("~/alarm"))
import alarmd  # noqa: E402  load/save/next_at of the alarm file

HERE = os.path.dirname(os.path.abspath(__file__))
APP_ID = "dev.notareal.Clock"
DATA = os.path.expanduser("~/.local/share/nrclock")
STATE = os.path.join(DATA, "state.json")
DAYS = "MTWTFSS"
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
PRESETS = (1, 3, 5, 10, 15, 30)       # timer quick picks, minutes


def label(text, css=None, xalign=0.0, **kw):
    lb = Gtk.Label(label=text, xalign=xalign, **kw)
    for c in (css or "").split():
        lb.add_css_class(c)
    return lb


def button(text, cb, css=""):
    b = Gtk.Button(label=text)
    for c in css.split():
        b.add_css_class(c)
    b.connect("clicked", lambda *_: cb())
    return b


def span(secs):
    """'8 h 12 min' / '4 min' / 'less than a minute'."""
    m = int(secs // 60)
    if m < 1:
        return "less than a minute"
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = (["%d d" % d] if d else []) + (["%d h" % h] if h else []) + (["%d min" % m] if m else [])
    return " ".join(parts)


def hms(secs, cs=False):
    secs = max(0.0, secs)
    whole = int(secs)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    out = ("%d:%02d:%02d" % (h, m, s)) if h else ("%02d:%02d" % (m, s))
    if cs:
        out += ".%02d" % int((secs - whole) * 100)
    return out


def load_state():
    try:
        with open(STATE) as f:
            st = json.load(f)
            return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(DATA, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE)


def is_timer(a):
    return a.get("kind") == "timer" or (a.get("label") or "").startswith("Timer")


def hm_of(a):
    if "hour" in a:
        return a["hour"], a["minute"]
    t = time.localtime(a["at"])
    return t.tm_hour, t.tm_min


def repeat_text(a):
    r = sorted(a.get("repeat") or [])
    if not r:
        if "hour" not in a or a.get("off"):
            return "once"
        t = time.localtime(a["at"])
        today = time.localtime()
        return "today" if t.tm_yday == today.tm_yday else "tomorrow" if (t.tm_yday - today.tm_yday) % 366 == 1 \
            else DAY_NAMES[t.tm_wday]
    if r == list(range(7)):
        return "every day"
    if r == list(range(5)):
        return "weekdays"
    if r == [5, 6]:
        return "weekends"
    return " ".join(DAY_NAMES[d] for d in r)


# --- alarm editor ------------------------------------------------------------------------------

class AlarmSheet(Adw.Dialog):
    def __init__(self, win, alarm=None):
        super().__init__(title="Alarm")
        self.win, self.alarm = win, alarm
        if alarm:
            h, m = hm_of(alarm)
        else:
            t = time.localtime(time.time() + 3600)
            h, m = t.tm_hour, 0
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=18, margin_bottom=22,
                      margin_start=18, margin_end=18)
        box.append(label("EDIT ALARM" if alarm else "NEW ALARM", "sheet-title"))

        picker = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self.hour = self.spin(0, 23, h)
        self.minute = self.spin(0, 59, m)
        picker.append(self.hour)
        picker.append(label(":", "pick-colon", xalign=0.5, valign=Gtk.Align.CENTER))
        picker.append(self.minute)
        box.append(picker)
        self.when = label("", "pick-when", xalign=0.5)
        box.append(self.when)

        days = Gtk.Box(homogeneous=True, spacing=4)
        self.day_btns = []
        rep = set(alarm.get("repeat") or []) if alarm else set()
        for d, ch in enumerate(DAYS):
            b = Gtk.ToggleButton(label=ch, active=d in rep, tooltip_text=DAY_NAMES[d])
            b.add_css_class("day-btn")
            b.connect("toggled", lambda *_: self.update_when())
            self.day_btns.append(b)
            days.append(b)
        box.append(days)
        quick = Gtk.Box(spacing=6, homogeneous=True)
        for text, ds in (("ONCE", ()), ("WEEKDAYS", range(5)), ("EVERY DAY", range(7))):
            quick.append(button(text, lambda ds=ds: self.set_days(ds), "chip"))
        box.append(quick)

        self.name = Gtk.Entry(placeholder_text="label (optional)",
                              text=(alarm.get("label") or "") if alarm else "")
        box.append(self.name)

        acts = Gtk.Box(spacing=10, margin_top=6)
        save = self.save_btn = button("SAVE", self.save, "suggested-action pill-btn")
        save.set_hexpand(True)
        acts.append(save)
        if alarm:
            acts.append(button("DELETE", self.delete, "destructive-action pill-btn"))
        box.append(acts)
        self.set_child(box)
        self.update_when()
        self.connect("map", lambda *_: GLib.timeout_add(250, lambda: (self.set_focus(self.save_btn), False)[1]))  # not the hour field (it opens selected)

    def spin(self, lo, hi, val):
        sp = Gtk.SpinButton.new_with_range(lo, hi, 1)
        sp.set_orientation(Gtk.Orientation.VERTICAL)
        sp.set_wrap(True)
        sp.set_numeric(True)
        sp.add_css_class("pick")
        sp.connect("output", self.two_digits)
        sp.set_value(val)
        sp.connect("value-changed", lambda *_: self.update_when())
        return sp

    @staticmethod
    def two_digits(sp):
        sp.set_text("%02d" % sp.get_value_as_int())
        return True

    def set_days(self, ds):
        ds = set(ds)
        for d, b in enumerate(self.day_btns):
            b.set_active(d in ds)

    def entry(self):
        a = dict(self.alarm) if self.alarm else {"id": uuid.uuid4().hex[:8]}
        a.update(hour=self.hour.get_value_as_int(), minute=self.minute.get_value_as_int(),
                 repeat=[d for d, b in enumerate(self.day_btns) if b.get_active()],
                 label=self.name.get_text().strip(), off=False)
        a["at"] = alarmd.next_at(a, time.time())
        return a

    def update_when(self):
        a = self.entry()
        self.when.set_label("rings in " + span(a["at"] - time.time()) + " · " + repeat_text(a))

    def save(self):
        a = self.entry()
        alarms = [x for x in alarmd.load() if x["id"] != a["id"]]
        alarmd.save(alarms + [a])
        self.win.toast("alarm in " + span(a["at"] - time.time()))
        self.close()

    def delete(self):
        alarmd.save([x for x in alarmd.load() if x["id"] != self.alarm["id"]])
        self.win.toast("alarm deleted")
        self.close()


# --- pages -------------------------------------------------------------------------------------

class AlarmPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=18, margin_bottom=10)
        self.now_lbl = label("", "now-time", xalign=0.5)
        self.date_lbl = label("", "now-date", xalign=0.5)
        self.next_lbl = label("", "next-alarm", xalign=0.5)
        for w in (self.now_lbl, self.date_lbl, self.next_lbl):
            head.append(w)
        self.append(head)

        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12, margin_end=12,
                            margin_bottom=110)
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.list)
        ov = Gtk.Overlay(child=sc)
        fab = Gtk.Button(halign=Gtk.Align.START, valign=Gtk.Align.END, margin_start=18, margin_bottom=18,
                         tooltip_text="New alarm")
        fab.add_css_class("fab")
        fb = Gtk.Box(spacing=8)
        fb.append(Gtk.Image(icon_name="list-add-symbolic"))
        fb.append(label("ALARM", "fab-label"))
        fab.set_child(fb)
        fab.connect("clicked", lambda *_: AlarmSheet(self.win).present(self.win))
        ov.add_overlay(fab)
        self.append(ov)
        self.sig = None
        self.tick()

    def tick(self):
        now = time.time()
        self.now_lbl.set_label(time.strftime("%H:%M"))
        self.date_lbl.set_label(time.strftime("%A %-d %B").upper())
        alarms = alarmd.load()
        nxt = [a["at"] for a in alarms if not a.get("off") and not is_timer(a) and a["at"] > now]
        self.next_lbl.set_label(("next alarm in " + span(min(nxt) - now)) if nxt else "no alarms set")
        sig = json.dumps([a for a in alarms if not is_timer(a)], sort_keys=True) + time.strftime("%j")
        if sig != self.sig:
            self.sig = sig
            self.build([a for a in alarms if not is_timer(a)])

    def build(self, alarms):
        while (c := self.list.get_first_child()):
            self.list.remove(c)
        if not alarms:
            self.list.append(label("tap ＋ ALARM to add one", "empty", xalign=0.5, margin_top=40))
        for a in sorted(alarms, key=lambda a: hm_of(a)):
            self.list.append(self.row(a))

    def row(self, a):
        h, m = hm_of(a)
        card = Gtk.Box(spacing=14)
        card.add_css_class("alarm-card")
        if a.get("off"):
            card.add_css_class("off")
        sw = Gtk.Switch(active=not a.get("off"), valign=Gtk.Align.CENTER)
        sw.connect("state-set", lambda _s, on, a=a: self.toggle(a, on))
        card.append(sw)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        col.append(label("%02d:%02d" % (h, m), "alarm-time"))
        sub = repeat_text(a) + ("  ·  " + a["label"] if a.get("label") else "")
        col.append(label(sub, "alarm-sub", ellipsize=3))
        card.append(col)
        b = Gtk.Button(child=card)
        b.add_css_class("flat")
        b.add_css_class("alarm-row")
        b.connect("clicked", lambda *_: AlarmSheet(self.win, a).present(self.win))
        return b

    def toggle(self, a, on):
        alarms = alarmd.load()
        for x in alarms:
            if x["id"] == a["id"]:
                if "hour" not in x:          # a voice alarm becomes a clock alarm
                    x["hour"], x["minute"] = hm_of(x)
                x["off"] = not on
                if on:
                    x["at"] = alarmd.next_at(x, time.time())
                    self.win.toast("alarm in " + span(x["at"] - time.time()))
        alarmd.save(alarms)
        return False


class Ring(Gtk.Widget):
    """Countdown ring: track + remaining arc, in theme colours (GSK paths, no cairo on the phone)."""
    SIZE = 260

    def __init__(self):
        super().__init__(halign=Gtk.Align.CENTER)
        self.frac = 1.0

    def do_measure(self, orientation, for_size):
        return self.SIZE, self.SIZE, -1, -1

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        r = min(w, h) / 2 - 10
        cx, cy = w / 2, h / 2
        round_caps = notareal_theme.value("corners", "1") != "0"
        stroke = Gsk.Stroke.new(10)
        if round_caps:
            stroke.set_line_cap(Gsk.LineCap.ROUND)

        def arc(frac):
            pb = Gsk.PathBuilder.new()
            steps = max(2, int(180 * frac))
            for k in range(steps + 1):
                a = -math.pi / 2 + 2 * math.pi * frac * k / steps
                pt = Graphene.Point().init(cx + r * math.cos(a), cy + r * math.sin(a))
                (pb.move_to if k == 0 else pb.line_to)(pt.x, pt.y)
            return pb.to_path()

        def rgba(name):
            c = Gdk.RGBA()
            c.parse(notareal_theme.hex(name))
            return c
        snap.append_stroke(arc(1.0), stroke, rgba("surface"))
        if self.frac > 0.002:
            snap.append_stroke(arc(self.frac), stroke, rgba("primary"))


class TimerPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.digits = ""                    # typed h/m/s digits, shifted in from the right
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.stack.add_named(self.setup_view(), "setup")
        self.stack.add_named(self.run_view(), "run")
        self.append(self.stack)
        self.others = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=12, margin_end=12,
                              margin_bottom=8)
        self.append(self.others)
        self.others_sig = None
        self.tick()

    # setup: keypad + presets
    def setup_view(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=14, margin_end=14,
                      margin_top=14, margin_bottom=14)
        self.entry_lbl = label("", "timer-entry", xalign=0.5)
        box.append(self.entry_lbl)
        pre = Gtk.Box(homogeneous=True, spacing=6)
        for m in PRESETS:
            pre.append(button("%d′" % m, lambda m=m: self.start(m * 60), "chip"))
        box.append(pre)
        pad = Gtk.Grid(column_homogeneous=True, row_homogeneous=True, column_spacing=8, row_spacing=8,
                       vexpand=True)
        for n, d in enumerate("123456789"):
            pad.attach(button(d, lambda d=d: self.type(d), "tk"), n % 3, n // 3, 1, 1)
        pad.attach(button("00", lambda: self.type("00"), "tk"), 0, 3, 1, 1)
        pad.attach(button("0", lambda: self.type("0"), "tk"), 1, 3, 1, 1)
        bk = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text="Delete")
        bk.add_css_class("tk")
        bk.connect("clicked", lambda *_: self.type(None))
        pad.attach(bk, 2, 3, 1, 1)
        box.append(pad)
        self.start_btn = button("START", lambda: self.start(self.entered()), "suggested-action pill-btn big-btn")
        self.start_btn.set_halign(Gtk.Align.START)
        self.start_btn.set_size_request(170, -1)
        box.append(self.start_btn)
        self.show_entry()
        return box

    def type(self, d):
        if d is None:
            self.digits = self.digits[:-1]
        else:
            self.digits = (self.digits + d).lstrip("0")[:6]
        self.show_entry()

    def entered(self):
        s = self.digits.rjust(6, "0")
        return int(s[:2]) * 3600 + int(s[2:4]) * 60 + int(s[4:])

    def show_entry(self):
        s = self.digits.rjust(6, "0")
        self.entry_lbl.set_label("%sh %sm %ss" % (s[:2], s[2:4], s[4:]))
        self.entry_lbl.set_opacity(1.0 if self.digits else 0.45)
        if hasattr(self, "start_btn"):
            self.start_btn.set_sensitive(bool(self.digits))

    # running: ring + controls
    def run_view(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, valign=Gtk.Align.CENTER, margin_start=14,
                      margin_end=14)
        ov = Gtk.Overlay()
        self.ring = Ring()
        ov.set_child(self.ring)
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.left_lbl = label("", "timer-left", xalign=0.5)
        self.ends_lbl = label("", "timer-ends", xalign=0.5)
        mid.append(self.left_lbl)
        mid.append(self.ends_lbl)
        ov.add_overlay(mid)
        box.append(ov)
        acts = Gtk.Box(spacing=10, margin_top=10)
        self.pause_btn = button("PAUSE", self.pause_resume, "suggested-action pill-btn big-btn")
        self.pause_btn.set_hexpand(True)
        acts.append(self.pause_btn)
        acts.append(button("+1 MIN", lambda: self.add(60), "pill-btn"))
        acts.append(button("CANCEL", self.cancel, "pill-btn"))
        box.append(acts)
        return box

    def timer_state(self):
        return load_state().get("timer")

    def set_timer_state(self, t):
        st = load_state()
        if t:
            st["timer"] = t
        else:
            st.pop("timer", None)
        save_state(st)

    def start(self, secs):
        if secs <= 0:
            return
        tid = uuid.uuid4().hex[:8]
        end = time.time() + secs
        alarmd.save(alarmd.load() + [{"id": tid, "at": end, "label": "Timer · " + span(secs), "kind": "timer"}])
        self.set_timer_state({"id": tid, "end": end, "total": secs})
        self.digits = ""
        self.show_entry()
        self.tick()

    def pause_resume(self):
        t = self.timer_state()
        if not t:
            return
        if "paused" in t:
            end = time.time() + t["paused"]
            alarmd.save(alarmd.load() + [{"id": t["id"], "at": end, "label": "Timer · " + span(t["total"]),
                                          "kind": "timer"}])
            t = {"id": t["id"], "end": end, "total": t["total"]}
        else:
            alarmd.save([a for a in alarmd.load() if a["id"] != t["id"]])
            t = {"id": t["id"], "paused": max(0, t["end"] - time.time()), "total": t["total"]}
        self.set_timer_state(t)
        self.tick()

    def add(self, secs):
        t = self.timer_state()
        if not t:
            return
        t["total"] += secs
        if "paused" in t:
            t["paused"] += secs
        else:
            t["end"] += secs
            alarms = alarmd.load()
            for a in alarms:
                if a["id"] == t["id"]:
                    a["at"] = t["end"]
            alarmd.save(alarms)
        self.set_timer_state(t)
        self.tick()

    def cancel(self):
        t = self.timer_state()
        if t:
            alarmd.save([a for a in alarmd.load() if a["id"] != t["id"]])
        self.set_timer_state(None)
        self.tick()

    def tick(self):
        now = time.time()
        t = self.timer_state()
        alarms = alarmd.load()
        if t and "end" in t and now >= t["end"] and not any(a["id"] == t["id"] for a in alarms):
            self.set_timer_state(None)       # it rang (alarmd removed it)
            t = None
        if t:
            left = t["paused"] if "paused" in t else t["end"] - now
            self.left_lbl.set_label(hms(left))
            self.ends_lbl.set_label("PAUSED" if "paused" in t else
                                    "ends " + time.strftime("%H:%M", time.localtime(t["end"])))
            self.pause_btn.set_label("RESUME" if "paused" in t else "PAUSE")
            self.ring.frac = max(0.0, min(1.0, left / max(1, t["total"])))
            self.ring.queue_draw()
            self.stack.set_visible_child_name("run")
        else:
            self.stack.set_visible_child_name("setup")
        # voice timers ("set a timer for 10 minutes") and anything else counting down
        others = [a for a in alarms if is_timer(a) and not (t and a["id"] == t["id"]) and a["at"] > now]
        sig = [a["id"] for a in others]
        if sig != self.others_sig:
            self.others_sig = sig
            while (c := self.others.get_first_child()):
                self.others.remove(c)
            self.other_lbls = []
            for a in others:
                row = Gtk.Box(spacing=10)
                row.add_css_class("other-timer")
                lb = label("", "other-left")
                self.other_lbls.append((lb, a))
                row.append(lb)
                row.append(label(a.get("label", ""), "alarm-sub", hexpand=True, ellipsize=3))
                x = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Cancel timer")
                x.add_css_class("flat")
                x.connect("clicked", lambda *_, i=a["id"]: (alarmd.save([b for b in alarmd.load() if b["id"] != i]),
                                                             self.tick()))
                row.append(x)
                self.others.append(row)
        for lb, a in getattr(self, "other_lbls", []):
            lb.set_label(hms(a["at"] - now))


class StopwatchPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.win = win
        self.time_lbl = label("", "sw-time", xalign=0.5, margin_top=40)
        self.lap_lbl = label("", "sw-lap", xalign=0.5)
        self.append(self.time_lbl)
        self.append(self.lap_lbl)
        self.laps = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=24, margin_end=24)
        self.append(Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.laps))
        acts = Gtk.Box(spacing=10, margin_start=14, margin_end=14, margin_bottom=14, homogeneous=True)
        self.go_btn = button("START", self.start_stop, "suggested-action pill-btn big-btn")
        self.lap_btn = button("LAP", self.lap_reset, "pill-btn big-btn")
        acts.append(self.go_btn)
        acts.append(self.lap_btn)
        self.append(acts)
        self.laps_sig = None
        self.tick()

    def sw(self):
        return load_state().get("sw") or {"start": None, "acc": 0.0, "laps": []}

    def save_sw(self, s):
        st = load_state()
        st["sw"] = s
        save_state(st)

    @staticmethod
    def elapsed(s):
        return s["acc"] + ((time.time() - s["start"]) if s["start"] else 0.0)

    def running(self):
        return bool(self.sw()["start"])

    def start_stop(self):
        s = self.sw()
        if s["start"]:
            s["acc"], s["start"] = self.elapsed(s), None
        else:
            s["start"] = time.time()
        self.save_sw(s)
        self.win.ensure_fast_tick()
        self.tick()

    def lap_reset(self):
        s = self.sw()
        if s["start"]:
            s["laps"].append(self.elapsed(s))
        else:
            s = {"start": None, "acc": 0.0, "laps": []}
        self.save_sw(s)
        self.tick()

    def tick(self):
        s = self.sw()
        el = self.elapsed(s)
        self.time_lbl.set_label(hms(el, cs=True))
        laps = s["laps"]
        self.lap_lbl.set_label(("LAP %d  %s" % (len(laps) + 1, hms(el - (laps[-1] if laps else 0), cs=True)))
                               if laps else "")
        self.go_btn.set_label("STOP" if s["start"] else ("RESUME" if el else "START"))
        self.lap_btn.set_label("LAP" if s["start"] else "RESET")
        self.lap_btn.set_sensitive(bool(s["start"] or el))
        if laps != self.laps_sig:
            self.laps_sig = list(laps)
            while (c := self.laps.get_first_child()):
                self.laps.remove(c)
            splits = [b - a for a, b in zip([0.0] + laps, laps)]
            best = min(splits) if len(splits) > 1 else None
            worst = max(splits) if len(splits) > 1 else None
            for n in range(len(laps), 0, -1):
                sp = splits[n - 1]
                row = Gtk.Box(spacing=10)
                row.add_css_class("lap-row")
                if sp == best:
                    row.add_css_class("best")
                elif sp == worst:
                    row.add_css_class("worst")
                row.append(label("LAP %d" % n, "lap-n"))
                row.append(label(hms(sp, cs=True), "lap-split", hexpand=True, xalign=0.5))
                row.append(label(hms(laps[n - 1], cs=True), "lap-total", xalign=1.0))
                self.laps.append(row)


# --- window ------------------------------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Clock", default_width=360, default_height=760)
        self.add_css_class("nrclock")
        self.fast = 0                       # stopwatch fast-tick source
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        hb.add_css_class("vapor-bar")
        hb.set_title_widget(label("NOTREAL.CLOCK", "app-title", xalign=0.5))

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.alarm = AlarmPage(self)
        self.timer = TimerPage(self)
        self.sw = StopwatchPage(self)
        self.stack.add_named(self.alarm, "alarm")
        self.stack.add_named(self.timer, "timer")
        self.stack.add_named(self.sw, "stopwatch")

        # tab bar at the bottom, in thumb reach
        bar = Gtk.Box(homogeneous=True)
        bar.add_css_class("tabbar")
        self.tabs = {}
        group = None
        for name, text, icon in (("alarm", "ALARM", "alarm-symbolic"),
                                 ("timer", "TIMER", "preferences-system-time-symbolic"),
                                 ("stopwatch", "STOPWATCH", "appointment-soon-symbolic")):
            b = Gtk.ToggleButton(group=group)
            group = group or b
            b.add_css_class("flat")
            b.add_css_class("tab")
            c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            c.append(Gtk.Image(icon_name=icon))
            c.append(label(text, "tab-label", xalign=0.5))
            b.set_child(c)
            b.connect("toggled", lambda b, n=name: b.get_active() and self.show(n))
            self.tabs[name] = b
            bar.append(b)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(self.stack)
        root.append(bar)
        self.toasts = Adw.ToastOverlay(child=root)
        tv = Adw.ToolbarView()
        tv.add_top_bar(hb)
        tv.set_content(self.toasts)
        self.set_content(tv)

        st = load_state()
        start = st.get("tab", "alarm")
        if st.get("timer"):
            start = "timer"
        self.tabs.get(start, self.tabs["alarm"]).set_active(True)
        self.show(start)

        self.mon = Gio.File.new_for_path(alarmd.ALARMS).monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.mon.connect("changed", lambda *a: GLib.timeout_add(200, lambda: (self.refresh(), False)[1]))
        notareal_theme.on_change(self.on_theme)
        self.on_theme()
        GLib.timeout_add(500, self.slow_tick)
        self.connect("map", lambda *_: self.ensure_fast_tick())
        self.ensure_fast_tick()

    def show(self, name):
        self.stack.set_visible_child_name(name)
        st = load_state()
        st["tab"] = name
        save_state(st)
        self.refresh()
        self.ensure_fast_tick()

    def refresh(self):
        page = self.stack.get_visible_child()
        if page is not None:
            page.tick()

    def slow_tick(self):
        # only the visible page, only while the window is on screen
        if self.get_mapped() and self.stack.get_visible_child() is not self.sw:
            self.refresh()
        return True

    def ensure_fast_tick(self):
        """Stopwatch centiseconds: ~25 fps while it runs and is shown, nothing otherwise."""
        if self.fast or self.stack.get_visible_child() is not self.sw:
            return

        def step():
            if self.stack.get_visible_child() is not self.sw or not self.sw.running():
                self.fast = 0
                self.sw.tick()
                return False
            if not self.get_mapped():
                self.fast = 0                # restarted by "map"
                return False
            self.sw.tick()
            return True
        self.sw.tick()
        if self.sw.running():
            self.fast = GLib.timeout_add(40, step)

    def on_theme(self, *_):
        (self.add_css_class if notareal_theme.value("corners", "1") == "0" else self.remove_css_class)("square")
        self.timer.ring.queue_draw()

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = self.get_active_window()
        if not win:
            notareal_theme.apply(os.path.join(HERE, "style.css"))
            win = Window(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
