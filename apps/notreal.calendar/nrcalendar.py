#!/usr/bin/env python3
"""notarealCalendar: Google + Outlook + Microsoft 365 calendars in one themed app.

AGENDA (upcoming, by day) and MONTH (grid + the chosen day's events). Left-handed: TODAY and the
month arrows sit on the left, NEW is bottom-left. New event = big full-screen steps:
WHAT -> WHEN -> WHICH CALENDAR -> (WHERE / NOTES), FINISH from WHEN onwards.
Data via Evolution (cal.py); Evolution syncs our changes back to Google / Outlook.
"""
import datetime
import json
import os
import sys
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.expanduser("~/theme"))
import notareal_theme  # noqa: E402
import cal  # noqa: E402

APP_ID = "dev.notareal.Calendar"
CONF = os.path.expanduser("~/.config/nrcalendar/config.json")
D = datetime.timedelta
AGENDA_DAYS = 90


def bg(work, done=None, fail=None):
    def run():
        try:
            r = work()
        except Exception as e:     # noqa: BLE001 - shown to the user
            print("nrcalendar:", repr(e), flush=True)
            if fail:
                GLib.idle_add(fail, e)
            return
        if done:
            GLib.idle_add(done, r)
    threading.Thread(target=run, daemon=True).start()


def label(text="", css=None, xalign=0.0, ellipsize=True, wrap=False):
    lb = Gtk.Label(label=text, xalign=xalign)
    if ellipsize and not wrap:
        lb.set_ellipsize(Pango.EllipsizeMode.END)
    if wrap:
        lb.set_wrap(True)
        lb.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if css:
        for c in css.split():
            lb.add_css_class(c)
    return lb


def dot(color, size=10):
    """Calendar colour dot (a "●" label: no pycairo on the phone for a DrawingArea)."""
    color = color if (color or "").startswith("#") else "#8fe0f2"
    lb = Gtk.Label(valign=Gtk.Align.CENTER)
    lb.set_markup(f'<span foreground="{GLib.markup_escape_text(color)}" size="{int(size * 1.15)}pt">●</span>')
    return lb


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


def when_text(ev):
    s, e = ev["start"], ev["end"]
    if ev["all_day"]:
        days = (e.date() - s.date()).days
        return "all day" if days <= 1 else f"all day · {days} days"
    if e.date() != s.date():
        return f"{s:%H:%M} – {e:%a %d %b %H:%M}"
    return f"{s:%H:%M} – {e:%H:%M}"


def day_title(d):
    today = datetime.date.today()
    if d == today:
        return "TODAY · " + d.strftime("%a %d %b").upper()
    if d == today + D(days=1):
        return "TOMORROW · " + d.strftime("%a %d %b").upper()
    return d.strftime("%A %d %B").upper()


# --- main page -----------------------------------------------------------------------------------

class MainPage(Adw.NavigationPage):
    def __init__(self, win):
        super().__init__(title="Calendar", tag="main")
        self.win = win
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        hb.add_css_class("vapor-bar")
        today = Gtk.Button(label="TODAY", css_classes=["chip"])
        today.connect("clicked", lambda *_: self.go_today())
        hb.pack_start(today)
        self.view_btn = Gtk.Button(css_classes=["chip"])
        self.view_btn.connect("clicked", lambda *_: self.toggle_view())
        hb.pack_start(self.view_btn)
        hb.set_title_widget(label("CALENDAR", "app-title", 0.5, False))
        cals = Gtk.Button(icon_name="view-list-bullet-symbolic", css_classes=["flat"], tooltip_text="Calendars")
        cals.connect("clicked", lambda *_: CalendarsSheet(win).present(win))
        hb.pack_end(cals)
        tv.add_top_bar(hb)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.agenda_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["agenda"])
        self.agenda_sw = Gtk.ScrolledWindow(child=self.agenda_box, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack.add_named(self.agenda_sw, "agenda")
        mbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        nav = Gtk.Box(spacing=6, margin_start=10, margin_end=10, margin_top=6)
        for icon, step in (("go-previous-symbolic", -1), ("go-next-symbolic", 1)):
            b = Gtk.Button(icon_name=icon, css_classes=["flat", "circular"])
            b.connect("clicked", lambda *_, s=step: self.shift_month(s))
            nav.append(b)
        self.month_label = label("", "month-title")
        nav.append(self.month_label)
        mbox.append(nav)
        self.grid = Gtk.Grid(column_homogeneous=True, row_homogeneous=True, css_classes=["month"],
                             margin_start=6, margin_end=6)
        for i, wd in enumerate(("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")):
            self.grid.attach(label(wd, "wd", 0.5, False), i, 0, 1, 1)
        swipe = Gtk.GestureSwipe()
        swipe.connect("swipe", lambda _g, vx, vy: abs(vx) > 600 and abs(vx) > 2 * abs(vy) and
                      self.shift_month(-1 if vx > 0 else 1))
        self.grid.add_controller(swipe)
        mbox.append(self.grid)
        self.day_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["agenda"])
        mbox.append(Gtk.ScrolledWindow(child=self.day_box, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.stack.add_named(mbox, "month")

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.status = label("", "status", 0.5)
        self.status.set_visible(False)
        body.append(self.status)
        body.append(self.stack)
        ov = Gtk.Overlay(child=body)
        new = Gtk.Button(css_classes=["fab"], halign=Gtk.Align.START, valign=Gtk.Align.END,
                         margin_start=18, margin_bottom=22)
        nb = Gtk.Box(spacing=8)
        nb.append(Gtk.Image(icon_name="list-add-symbolic"))
        nb.append(label("NEW", None, 0.5, False))
        new.set_child(nb)
        new.connect("clicked", lambda *_: EventWizard(win, day=self.selected))
        ov.add_overlay(new)
        tv.set_content(ov)
        self.set_child(tv)

        self.month = datetime.date.today().replace(day=1)
        self.selected = datetime.date.today()
        self.month_events = []
        self.per_day = {}
        self.set_view(win.conf.get("view", "agenda"))

    def set_view(self, v):
        self.view = v
        self.stack.set_visible_child_name(v)
        self.view_btn.set_label("MONTH" if v == "agenda" else "AGENDA")
        self.win.conf["view"] = v
        save_conf(self.win.conf)

    def toggle_view(self):
        self.set_view("month" if self.view == "agenda" else "agenda")
        self.reload()

    def go_today(self):
        self.month = datetime.date.today().replace(day=1)
        self.selected = datetime.date.today()
        self.reload()

    def shift_month(self, step):
        y, m = self.month.year, self.month.month + step
        y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
        self.month = datetime.date(y, m, 1)
        today = datetime.date.today()
        self.selected = today if self.month == today.replace(day=1) else self.month
        self.reload()
        return True

    def reload(self):
        if not self.win.ready:
            return
        if self.view == "agenda":
            start = datetime.datetime.combine(datetime.date.today(), datetime.time())
            rng = (start, start + D(days=AGENDA_DAYS))
        else:
            first = self.month - D(days=self.month.weekday())
            start = datetime.datetime.combine(first, datetime.time())
            rng = (start, start + D(days=42))
        hidden = set(self.win.conf.get("hidden", []))
        self.status.set_label("loading…")
        self.status.set_visible(True)
        bg(lambda: self.win.store.events(*rng, hidden=hidden), self._loaded, self.win.failed)

    def _loaded(self, events):
        self.status.set_visible(False)
        if self.view == "agenda":
            self.fill_agenda(events)
        else:
            self.month_events = events
            self.fill_month()

    @staticmethod
    def _clear(box):
        while box.get_first_child():
            box.remove(box.get_first_child())

    def event_row(self, ev):
        c = self.win.store.by_uid(ev["cal"])
        b = Gtk.Button(css_classes=["event"])
        h = Gtk.Box(spacing=10)
        h.append(dot(c.color if c else "#8fe0f2", 10))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        col.append(label(ev["title"], "ev-title"))
        sub = when_text(ev) + (f" · {ev['location']}" if ev["location"] else "")
        col.append(label(sub, "ev-sub"))
        h.append(col)
        b.set_child(h)
        b.connect("clicked", lambda *_: self.win.open_event(ev))
        return b

    def fill_agenda(self, events):
        self._clear(self.agenda_box)
        by_day = {}
        today = datetime.date.today()
        for ev in events:
            by_day.setdefault(max(ev["start"].date(), today), []).append(ev)
        by_day.setdefault(today, [])
        for d in sorted(by_day):
            self.agenda_box.append(label(day_title(d), "day-head" + (" today" if d == today else "")))
            if not by_day[d]:
                self.agenda_box.append(label("nothing planned", "empty"))
            for ev in by_day[d]:
                self.agenda_box.append(self.event_row(ev))
        self.agenda_box.append(Gtk.Box(height_request=90))     # room under the NEW button

    def fill_month(self):
        self.month_label.set_label(self.month.strftime("%B %Y").upper())
        for r in range(1, 7):
            for col in range(7):
                w = self.grid.get_child_at(col, r)
                if w:
                    self.grid.remove(w)
        first = self.month - D(days=self.month.weekday())
        per_day = {}
        for ev in self.month_events:
            d = ev["start"].date()
            end = ev["end"].date()
            last = end - D(days=1) if ev["all_day"] and end > d else end
            while d <= max(last, ev["start"].date()):
                per_day.setdefault(d, []).append(ev)
                d += D(days=1)
        today = datetime.date.today()
        for i in range(42):
            d = first + D(days=i)
            cell = Gtk.Button(css_classes=["day"])
            if d.month != self.month.month:
                cell.add_css_class("other")
            if d == today:
                cell.add_css_class("today")
            if d == self.selected:
                cell.add_css_class("selected")
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            v.append(label(str(d.day), "day-num", 0.5, False))
            dots = Gtk.Box(spacing=2, halign=Gtk.Align.CENTER)
            for ev in per_day.get(d, [])[:4]:
                c = self.win.store.by_uid(ev["cal"])
                dots.append(dot(c.color if c else "#8fe0f2", 6))
            v.append(dots)
            cell.set_child(v)
            cell.connect("clicked", lambda *_, d=d: self.pick_day(d))
            self.grid.attach(cell, i % 7, 1 + i // 7, 1, 1)
        self.per_day = per_day
        self.fill_day()

    def pick_day(self, d):
        if d.month != self.month.month:
            self.month = d.replace(day=1)
            self.selected = d
            self.reload()
            return
        self.selected = d
        self.fill_month()

    def fill_day(self):
        self._clear(self.day_box)
        d = self.selected
        self.day_box.append(label(day_title(d), "day-head"))
        evs = self.per_day.get(d, [])
        if not evs:
            self.day_box.append(label("nothing planned", "empty"))
        for ev in evs:
            self.day_box.append(self.event_row(ev))
        self.day_box.append(Gtk.Box(height_request=90))


class CalendarsSheet(Adw.Dialog):
    def __init__(self, win):
        super().__init__(title="Calendars")
        self.set_presentation_mode(Adw.DialogPresentationMode.BOTTOM_SHEET)
        self.win = win
        self.set_content_height(700)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=16, margin_end=16,
                      margin_top=10, margin_bottom=16)
        box.append(label("SHOW CALENDARS", "sheet-title", 0.5))
        hidden = set(win.conf.get("hidden", []))
        last_service = None
        for c in sorted(win.store.cals, key=lambda c: (c.service, c.account, c.name.lower())):
            if c.service != last_service:
                box.append(label(f"{c.service.upper()} · {c.account}", "svc-head"))
                last_service = c.service
            row = Gtk.Box(spacing=10, css_classes=["cal-row"])
            sw = Gtk.Switch(active=c.uid not in hidden, valign=Gtk.Align.CENTER)
            sw.connect("notify::active", lambda s, _p, uid=c.uid: self.toggle(uid, s.get_active()))
            row.append(sw)                          # switch on the left (left-handed)
            row.append(dot(c.color, 12))
            row.append(label(c.name + ("" if c.client and not c.readonly else "  · read-only"), "cal-name"))
            box.append(row)
        self.set_child(Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.connect("closed", lambda *_: win.main.reload())

    def toggle(self, uid, on):
        hidden = set(self.win.conf.get("hidden", []))
        (hidden.discard if on else hidden.add)(uid)
        self.win.conf["hidden"] = sorted(hidden)
        save_conf(self.win.conf)


class EventPage(Adw.NavigationPage):
    def __init__(self, win, ev):
        super().__init__(title=ev["title"])
        self.win, self.ev = win, ev
        c = win.store.by_uid(ev["cal"])
        writable = bool(c and c.client and not c.readonly)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        if writable:
            edit = Gtk.Button(label="EDIT", css_classes=["chip"])
            edit.connect("clicked", lambda *_: EventWizard(win, ev=ev))
            hb.pack_start(edit)
            delete = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat"])
            delete.connect("clicked", lambda *_: self.confirm_delete())
            hb.pack_end(delete)
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=20, margin_end=20,
                      margin_top=14, margin_bottom=20)
        box.append(label(ev["title"], "big-title", 0.0, wrap=True))
        box.append(label(ev["start"].strftime("%A %d %B %Y"), "ev-date"))
        box.append(label(when_text(ev) + ("  ·  repeats" if ev["recurring"] else ""), "ev-time"))
        if c:
            r = Gtk.Box(spacing=8)
            r.append(dot(c.color, 12))
            r.append(label(f"{c.name} · {c.service}", "ev-cal"))
            box.append(r)
        if ev["location"]:
            box.append(label("WHERE", "d-kind"))
            box.append(label(ev["location"], "d-value", wrap=True))
        if ev["notes"]:
            box.append(label("NOTES", "d-kind"))
            box.append(label(ev["notes"], "d-notes", wrap=True))
        if not writable:
            box.append(label("read-only calendar", "empty"))
        tv.set_content(Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.set_child(tv)

    def confirm_delete(self):
        ev = self.ev
        dlg = Adw.AlertDialog(heading=f"Delete “{ev['title']}”?",
                              body="It is removed from the online calendar too.")
        dlg.add_response("cancel", "Cancel")
        if ev["rid"]:
            dlg.add_response("one", "This one")
            dlg.add_response("all", "All repeats")
        else:
            dlg.add_response("all", "Delete")
        dlg.set_response_appearance("all", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", lambda _d, r: r in ("one", "all") and self.win.delete_event(ev, r == "all"))
        dlg.present(self.win)


class StepPage(Adw.NavigationPage):
    """One question per screen; buttons bottom-LEFT (left-handed)."""

    def __init__(self, title, question, content, buttons):
        super().__init__(title=title)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        hb.set_title_widget(label(title, "sheet-title", 0.5, False))
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=20, margin_end=20,
                      margin_top=18, margin_bottom=24)
        box.append(label(question, "question", 0.0, False))
        box.append(content)
        box.append(Gtk.Box(vexpand=True))
        row = Gtk.Box(spacing=12, halign=Gtk.Align.START)
        self.buttons = []
        for text, css, cb in buttons:
            b = Gtk.Button(label=text, css_classes=["step-btn"] + css.split())
            b.connect("clicked", lambda *_, cb=cb: cb())
            row.append(b)
            self.buttons.append(b)
        box.append(row)
        tv.set_content(Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.set_child(tv)


class EventWizard:
    def __init__(self, win, day=None, ev=None):
        self.win, self.ev = win, ev
        self.title_text = "EDIT EVENT" if ev else "NEW EVENT"
        now = datetime.datetime.now()
        if ev:
            self.title, self.start, self.all_day = ev["title"], ev["start"], ev["all_day"]
            self.minutes = 60 if ev["all_day"] else max(15, int((ev["end"] - ev["start"]).total_seconds() // 60))
            self.cal_uid, self.location, self.notes = ev["cal"], ev["location"], ev["notes"]
        else:
            d = day or now.date()
            hour = now.hour + 1 if d == now.date() else 9
            self.title, self.all_day, self.minutes = "", False, 60
            self.start = datetime.datetime.combine(d, datetime.time(min(hour, 23), 0))
            writable = [c.uid for c in win.store.writable()]
            google = [c.uid for c in win.store.writable() if c.service == "Google"]
            last = win.conf.get("last_cal")
            self.cal_uid = last if last in writable else (google[0] if google else (writable[0] if writable else None))
            self.location, self.notes = "", ""
        self.step_what()

    def step_what(self):
        self.what = Gtk.Entry(placeholder_text="e.g. Dinner with Ana", css_classes=["big-entry"])
        self.what.set_text(self.title)
        self.what.connect("activate", lambda *_: self.what_next())
        page = StepPage(self.title_text, "What's happening?", self.what, [("NEXT", "suggested", self.what_next)])
        page.connect("shown", lambda *_: self.what.grab_focus())
        self.win.nav.push(page)

    def what_next(self):
        self.title = self.what.get_text().strip()
        if not self.title:
            self.what.add_css_class("error")
            return
        self.step_when()

    def step_when(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.cal_w = Gtk.Calendar(css_classes=["pick-date"])
        self.cal_w.select_day(GLib.DateTime.new_local(self.start.year, self.start.month, self.start.day, 0, 0, 0))
        box.append(self.cal_w)
        ad = Gtk.Box(spacing=10)
        self.allday_sw = Gtk.Switch(active=self.all_day, valign=Gtk.Align.CENTER)
        ad.append(self.allday_sw)
        ad.append(label("ALL DAY", "d-kind"))
        box.append(ad)
        self.time_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        trow = Gtk.Box(spacing=8)
        for text, delta in (("−1h", -60), ("−15", -15)):
            b = Gtk.Button(label=text, css_classes=["chip", "big-chip"])
            b.connect("clicked", lambda *_, dl=delta: self.nudge(dl))
            trow.append(b)
        self.time_lbl = label("", "big-time", 0.5, False)
        self.time_lbl.set_hexpand(True)
        trow.append(self.time_lbl)
        for text, delta in (("+15", 15), ("+1h", 60)):
            b = Gtk.Button(label=text, css_classes=["chip", "big-chip"])
            b.connect("clicked", lambda *_, dl=delta: self.nudge(dl))
            trow.append(b)
        self.time_box.append(trow)
        drow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=5, column_spacing=6)
        self.dur_btns = {}
        for mins, text in ((30, "30 min"), (60, "1 h"), (90, "1½ h"), (120, "2 h"), (180, "3 h")):
            b = Gtk.ToggleButton(label=text, css_classes=["chip"])
            b.connect("clicked", lambda *_, m=mins: self.set_dur(m))
            drow.append(b)
            self.dur_btns[mins] = b
        self.time_box.append(drow)
        self.end_lbl = label("", "ev-sub")
        self.time_box.append(self.end_lbl)
        box.append(self.time_box)
        self.allday_sw.connect("notify::active", lambda *_: self.time_box.set_visible(not self.allday_sw.get_active()))
        self.time_box.set_visible(not self.all_day)
        self.refresh_time()
        self.win.nav.push(StepPage(self.title_text, "When?", box,
                                   [("NEXT", "suggested", self.when_next), ("FINISH", "", self.when_finish)]))

    def nudge(self, mins):
        self.start = self.start + D(minutes=mins)
        self.refresh_time()

    def set_dur(self, mins):
        self.minutes = mins
        self.refresh_time()

    def refresh_time(self):
        self.time_lbl.set_label(self.start.strftime("%H:%M"))
        end = self.start + D(minutes=self.minutes)
        extra = f" (+{(end.date() - self.start.date()).days} day)" if end.date() != self.start.date() else ""
        self.end_lbl.set_label(f"until {end:%H:%M}{extra}")
        for m, b in self.dur_btns.items():
            b.set_active(m == self.minutes)

    def _read_when(self):
        dt = self.cal_w.get_date()
        self.start = self.start.replace(year=dt.get_year(), month=dt.get_month(), day=dt.get_day_of_month())
        self.all_day = self.allday_sw.get_active()

    def when_next(self):
        self._read_when()
        self.step_which()

    def when_finish(self):
        self._read_when()
        self.save()

    def step_which(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        group = None
        last_service = None
        for c in sorted(self.win.store.writable(), key=lambda c: (c.service, c.account, c.name.lower())):
            if self.ev and c.uid != self.ev["cal"]:
                continue                 # moving an event to another calendar isn't supported
            if c.service != last_service:
                box.append(label(f"{c.service.upper()} · {c.account}", "svc-head"))
                last_service = c.service
            r = Gtk.Box(spacing=10, css_classes=["cal-row"])
            cb = Gtk.CheckButton(active=c.uid == self.cal_uid)
            if group:
                cb.set_group(group)
            group = group or cb
            cb.connect("toggled", lambda b, uid=c.uid: b.get_active() and setattr(self, "cal_uid", uid))
            r.append(cb)
            r.append(dot(c.color, 12))
            r.append(label(c.name, "cal-name"))
            box.append(r)
        self.win.nav.push(StepPage(self.title_text, "Which calendar?", box,
                                   [("FINISH", "suggested", self.save), ("NEXT", "", self.step_where)]))

    def step_where(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.where = Gtk.Entry(placeholder_text="Where (optional)", css_classes=["big-entry"])
        self.where.set_text(self.location)
        box.append(self.where)
        box.append(label("NOTES", "d-kind"))
        self.notes_tv = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, css_classes=["notes"])
        self.notes_tv.get_buffer().set_text(self.notes)
        self.notes_tv.set_size_request(-1, 160)
        box.append(self.notes_tv)
        self.win.nav.push(StepPage(self.title_text, "Where? Notes?", box, [("FINISH", "suggested", self.where_finish)]))

    def where_finish(self):
        self.location = self.where.get_text().strip()
        b = self.notes_tv.get_buffer()
        self.notes = b.get_text(b.get_start_iter(), b.get_end_iter(), False).strip()
        self.save()

    def save(self):
        if not self.cal_uid:
            self.win.toast("no writable calendar")
            return
        start = self.start.replace(hour=0, minute=0) if self.all_day else self.start
        end = start + (D(days=1) if self.all_day else D(minutes=self.minutes))
        self.win.save_event(self.ev, self.cal_uid, self.title, start, end, self.all_day, self.location, self.notes)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Calendar", default_width=540, default_height=1100)
        self.conf = load_conf()
        self.ready = False
        self.store = None
        self.main = None
        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)
        bg(self._connect, self._connected, self.failed)

    @staticmethod
    def _connect():
        s = cal.Store()
        s.connect_all()
        return s

    def _connected(self, store):
        self.store = store
        self.ready = True
        if "hidden" not in self.conf:
            # first start: Outlook/M365 "Birthdays" = every contact's birthday (~50 a month), noise
            self.conf["hidden"] = [c.uid for c in store.cals if c.name == "Birthdays" and c.backend == "microsoft365"]
            save_conf(self.conf)
        self.main = MainPage(self)
        self.nav.add(self.main)
        self.main.reload()

    def failed(self, e):
        self.toast(f"calendar error: {e}")

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    def open_event(self, ev):
        self.nav.push(EventPage(self, ev))

    def save_event(self, ev, cal_uid, title, start, end, all_day, location, notes):
        self.conf["last_cal"] = cal_uid
        save_conf(self.conf)

        def work():
            if ev:
                self.store.update(ev, title, start, end, all_day, location, notes)
            else:
                self.store.create(cal_uid, title, start, end, all_day, location, notes)

        def done(_r):
            self.toast(f"“{title}” saved")
            self.nav.pop_to_tag("main")
            self.main.selected = start.date()
            self.main.month = start.date().replace(day=1)
            self.main.reload()
        bg(work, done, lambda e: self.toast(f"not saved: {e}"))

    def delete_event(self, ev, all_occurrences):
        def done(_r):
            self.toast(f"“{ev['title']}” deleted")
            self.nav.pop_to_tag("main")
            self.main.reload()
        bg(lambda: self.store.delete(ev, all_occurrences), done, lambda e: self.toast(f"not deleted: {e}"))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.win = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        notareal_theme.apply(os.path.join(HERE, "style.css"))

    def do_activate(self):
        if not self.win:
            self.win = Window(self)
        self.win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
