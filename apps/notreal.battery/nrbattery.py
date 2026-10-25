#!/usr/bin/env python3
"""notreal.battery: where the battery went - you using the phone, or the phone on its own.

Reads nrbatteryd's segments (~/.local/share/nrbattery/*.jsonl). Each segment's charge (mAh
from the fuel gauge's charge counter) goes to one bucket: SCREEN ON (per app), SCREEN OFF
but awake, ASLEEP (suspended) or CHARGING. Shows the last 24 h (or a past day), a chart of
battery % with screen-on / asleep / charging bands, drain per hour of each bucket, pickups,
apps and heat. Left-handed: day arrows + refresh on the left.
"""
import datetime
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
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402
try:
    gi.require_version("GioUnix", "2.0")
    from gi.repository.GioUnix import DesktopAppInfo  # noqa: E402
except (ValueError, ImportError):
    DesktopAppInfo = Gio.DesktopAppInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.expanduser("~/theme"))
sys.path.insert(0, os.path.expanduser("~/weather"))
import notareal_theme  # noqa: E402
import draw  # noqa: E402 - weather's GSK helpers (no cairo on the phone)
from gi.repository import Graphene, Gsk  # noqa: E402

APP_ID = "dev.notareal.Battery"
DATA = os.environ.get("NRBAT_DATA") or os.path.expanduser("~/.local/share/nrbattery")   # NRBAT_DATA: test data
BAT = "/sys/class/power_supply/qcom-battmgr-bat/"
DESIGN_MAH = 4500
HIDE_APPS = {"overview"}
SAVER = os.path.expanduser("~/saver/nrsaver")                 # battery saver mode (10-24)
SAVER_FLAG = os.path.expanduser("~/.local/state/nrsaver/on")


def label(text="", css=None, xalign=0.0, ellipsize=True, **kw):
    lb = Gtk.Label(label=text, xalign=xalign, **kw)
    if ellipsize:
        lb.set_ellipsize(Pango.EllipsizeMode.END)
    for c in (css or "").split():
        lb.add_css_class(c)
    return lb


def vbox(spacing=0, css=None, **kw):
    b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kw)
    for c in (css or "").split():
        b.add_css_class(c)
    return b


def hbox(spacing=0, css=None, **kw):
    b = Gtk.Box(spacing=spacing, **kw)
    for c in (css or "").split():
        b.add_css_class(c)
    return b


def dur(s):
    m = int(s // 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def pct(mah):
    return 100 * mah / DESIGN_MAH


def rd(name):
    try:
        return open(BAT + name).read().strip()
    except OSError:
        return None


_names = {}


def app_name(app_id):
    if app_id not in _names:
        info = None
        alias = {"chromium-browser": "chromium"}.get(app_id.lower())   # window id without a .desktop
        for cand in (app_id, app_id.lower(), alias or app_id):
            try:
                info = DesktopAppInfo.new(cand + ".desktop")
            except TypeError:
                info = None
            if info:
                break
        _names[app_id] = info.get_name() if info else app_id.split(".")[-1]
    return _names[app_id]


def load(start, end):
    recs = []
    d = datetime.date.fromtimestamp(start)
    while d <= datetime.date.fromtimestamp(end):
        try:
            with open(os.path.join(DATA, d.isoformat() + ".jsonl")) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if start < r["t"] <= end:
                        recs.append(r)
        except OSError:
            pass
        d += datetime.timedelta(days=1)
    return recs


def summarize(recs):
    s = {k: {"t": 0.0, "mah": 0.0} for k in ("on", "off", "sleep", "chg")}
    apps, procs = {}, {}
    pickups, prev_scr = 0, None
    hot = (None, None)
    for r in recs:
        dt, mah = r.get("dt", 0), r.get("mah") or 0
        if r.get("chg"):
            s["chg"]["t"] += dt
        else:
            slept = min(r.get("slept", 0), dt)
            awake = dt - slept
            # suspended time draws ~nothing compared to awake, so split charge by a 1:8 weight
            w_sleep = slept / (slept + 8 * awake) if dt else 0
            s["sleep"]["t"] += slept
            s["sleep"]["mah"] += mah * w_sleep
            k = "on" if r.get("scr") else "off"
            s[k]["t"] += awake
            s[k]["mah"] += mah * (1 - w_sleep)
            if r.get("scr"):
                a = r.get("app") or "overview"
                e = apps.setdefault(a, [0.0, 0.0])
                e[0] += awake
                e[1] += mah * (1 - w_sleep)
        if r.get("scr") and prev_scr is False:
            pickups += 1
        prev_scr = r.get("scr")
        for name, cpu in r.get("top", []):
            procs[name] = procs.get(name, 0) + cpu * dt / 100   # core-seconds
        skin = r.get("skin")
        if skin and (hot[0] is None or skin > hot[0]):
            hot = (skin, r["t"])
    return s, apps, procs, pickups, hot


class Chart(draw.Canvas):
    """Battery % line over a time window; bands below it: screen on, asleep, charging."""

    def __init__(self):
        super().__init__(-1, 260)
        self.set_hexpand(True)
        self.recs, self.t0, self.t1 = [], 0, 1

    def set(self, recs, t0, t1):
        self.recs, self.t0, self.t1 = recs, t0, t1
        self.queue_draw()

    def paint(self, snap, w, h):
        th = notareal_theme
        pad_l, pad_b, band = 38, 22, 12
        cw, ch = w - pad_l - 6, h - pad_b - 3 * band - 18
        x = lambda t: pad_l + cw * (t - self.t0) / (self.t1 - self.t0)  # noqa: E731
        y = lambda p: 8 + ch * (1 - p / 100)  # noqa: E731
        rect = lambda x0, y0, ww, hh, col: snap.append_color(col, Graphene.Rect().init(x0, y0, ww, hh))  # noqa: E731
        for p in (0, 50, 100):
            rect(pad_l, y(p), cw, 1, th.color("fg", 0.15))
            draw.text(self, snap, f"{p}%", 0, y(p), 10, th.color("fg", 0.6), center=False)
        span = self.t1 - self.t0
        step = 3600 * (6 if span > 20 * 3600 else 3 if span > 8 * 3600 else 1)
        lt = time.localtime(self.t0)
        t = self.t0 - lt.tm_min * 60 - lt.tm_sec + 3600
        while t <= self.t1:
            if time.localtime(t).tm_hour % (step // 3600) == 0:
                draw.text(self, snap, time.strftime("%H:%M", time.localtime(t)), x(t), h - 8, 10, th.color("fg", 0.6))
            t += 3600
        by = 8 + ch + 12
        tests = [("accent", lambda r: r.get("scr")),
                 ("violet", lambda r: r.get("slept", 0) > r.get("dt", 1) / 2),
                 ("mint", lambda r: r.get("chg"))]
        for i, (col, hit) in enumerate(tests):
            yy = by + i * (band + 2)
            rect(pad_l, yy, cw, band - 2, th.color("fg", 0.06))
            c = th.color(col, 0.9)
            for r in self.recs:
                if hit(r):
                    a = x(r["t"] - r.get("dt", 0))
                    rect(a, yy, max(1.0, x(r["t"]) - a), band - 2, c)
        pts = [(x(r["t"]), y(r["pct"])) for r in self.recs if r.get("pct") is not None]
        if len(pts) > 1:
            pb = Gsk.PathBuilder.new()
            pb.move_to(*pts[0])
            for px, py in pts[1:]:
                pb.line_to(px, py)
            draw.stroke(snap, pb.to_path(), th.color("fg-bright"), 2.5)
        else:
            draw.text(self, snap, "collecting… (one point a minute)", pad_l + 12, y(55), 11,
                      th.color("fg", 0.6), center=False)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Battery", default_width=540, default_height=1100)
        self.day = 0          # 0 = last 24 h, -1 = yesterday (calendar day), ...
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        self.prev = Gtk.Button(icon_name="go-previous-symbolic", css_classes=["flat"], tooltip_text="Earlier day")
        self.prev.connect("clicked", lambda *_: self.shift(-1))
        self.next = Gtk.Button(icon_name="go-next-symbolic", css_classes=["flat"], tooltip_text="Later day")
        self.next.connect("clicked", lambda *_: self.shift(1))
        hb.pack_start(self.prev)
        hb.pack_start(self.next)
        self.title = label("", "bt-title", 0.5, False)
        hb.set_title_widget(self.title)
        tv.add_top_bar(hb)

        box = vbox(14, "bt-page")
        head = hbox(16)
        self.big = label("", "bt-big", 0.0, False)
        sub = vbox(2, valign=Gtk.Align.CENTER, hexpand=True)
        self.status = label("", "bt-status")
        self.status2 = label("", "bt-sub")
        sub.append(self.status)
        sub.append(self.status2)
        head.append(self.big)
        head.append(sub)
        # battery saver: slower phone, notifications still come in (~/saver/nrsaver)
        self.saver = Gtk.ToggleButton(label="SAVER", css_classes=["bt-saver"], valign=Gtk.Align.CENTER)
        self.saver_sync = False
        self.saver.connect("toggled", self.saver_toggled)
        head.append(self.saver)
        box.append(head)

        card = vbox(8, "bt-card")
        self.chart = Chart()
        card.append(self.chart)
        legend = hbox(14)
        for name, css in (("SCREEN ON", "on"), ("ASLEEP", "sleep"), ("CHARGING", "chg")):
            legend.append(label("■ " + name, "bt-legend " + css, ellipsize=False))
        card.append(legend)
        box.append(card)

        self.verdict = label("", "bt-verdict", ellipsize=False)
        self.verdict.set_wrap(True)
        box.append(self.verdict)

        self.tiles = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True)
        box.append(self.tiles)
        box.append(label("APPS · SCREEN TIME · BATTERY", "bt-head"))
        self.apps = vbox(0, "bt-card bt-list")
        box.append(self.apps)
        box.append(label("HEAT · BUSIEST PROCESSES", "bt-head"))
        self.heat = vbox(0, "bt-card bt-list")
        box.append(self.heat)
        box.append(label("logged by nrbatteryd · % = mAh of 4500", "bt-foot", 0.5))

        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(box)
        tv.set_content(sc)
        self.set_content(tv)
        notareal_theme.on_change(self.on_theme)
        self.on_theme()
        self.connect("notify::is-active", lambda *_: self.is_active() and self.refresh())
        GLib.timeout_add_seconds(60, lambda: (self.refresh(), True)[1])

    def saver_toggled(self, btn):
        if self.saver_sync:
            return
        btn.set_sensitive(False)
        btn.set_label("…")
        on = btn.get_active()

        def run():
            subprocess.run([SAVER, "on" if on else "off"], capture_output=True, timeout=60)
            GLib.idle_add(lambda: (btn.set_sensitive(True), self.sync_saver(), False)[-1])
        threading.Thread(target=run, daemon=True).start()

    def sync_saver(self):
        self.saver_sync = True
        on = os.path.exists(SAVER_FLAG)
        self.saver.set_active(on)
        self.saver.set_label("SAVER ON" if on else "SAVER")
        self.saver_sync = False

    def shift(self, d):
        self.day = min(0, self.day + d)
        self.refresh()

    def window(self):
        now = time.time()
        if self.day == 0:
            return now - 86400, now, "LAST 24 H"
        d = datetime.date.today() + datetime.timedelta(days=self.day)
        t0 = time.mktime(d.timetuple())
        return t0, t0 + 86400, d.strftime("%a %d %b").upper()

    def refresh(self):
        t0, t1, name = self.window()
        self.title.set_text(name)
        self.next.set_sensitive(self.day < 0)
        if self.saver.get_sensitive():
            self.sync_saver()
        recs = load(t0, t1)
        self.chart.set(recs, t0, t1)
        s, apps, procs, pickups, hot = summarize(recs)

        cap, st = rd("capacity"), (rd("status") or "").upper()
        ma = int(rd("current_now") or 0) / 1000
        self.big.set_text(f"{cap}%")
        self.status.set_text(st)
        if ma > 0:
            self.status2.set_text(f"+{ma:.0f} mA")
        else:
            self.status2.set_text(f"-{-ma:.0f} mA")

        def rate(k):
            return pct(s[k]["mah"]) / (s[k]["t"] / 3600) if s[k]["t"] > 120 else None

        used = sum(s[k]["mah"] for k in ("on", "off", "sleep"))
        share_on = s["on"]["mah"] / used if used > 0 else 0
        if used <= 0:
            self.verdict.set_text("Not enough data yet - the logger started recently.")
        else:
            who = "YOU" if share_on >= 0.6 else "THE PHONE" if share_on <= 0.4 else "BOTH"
            self.verdict.set_text(f"{who}: {share_on * 100:.0f}% of the {pct(used):.0f}% used went to screen-on time, "
                                  f"{(1 - share_on) * 100:.0f}% to the phone on its own (screen off).")

        for c in list(self.tiles):
            self.tiles.remove(c)
        tiles = [("SCREEN ON", s["on"], rate("on"), f"{pickups} pickups"),
                 ("SCREEN OFF · AWAKE", s["off"], rate("off"), "should be ~0"),
                 ("ASLEEP", s["sleep"], rate("sleep"), "suspended"),
                 ("CHARGING", s["chg"], None, "")]
        for i, (title, b, r, note) in enumerate(tiles):
            t = vbox(2, "bt-card bt-tile")
            t.append(label(title, "bt-tile-title"))
            t.append(label(dur(b["t"]), "bt-tile-value"))
            line = f"{pct(b['mah']):.0f}% · {r:.1f} %/h" if r is not None else ("—" if title != "CHARGING" else "")
            t.append(label(line, "bt-tile-sub"))
            if note:
                t.append(label(note, "bt-tile-note"))
            self.tiles.attach(t, i % 2, i // 2, 1, 1)

        for c in list(self.apps):
            self.apps.remove(c)
        merged = {}   # one row per app name (Chromium shows up under several window ids)
        for a, (secs, mah) in apps.items():
            if a in HIDE_APPS and secs <= 600:
                continue
            name = app_name(a) if a != "overview" else "Home / overview"
            e = merged.setdefault(name, [0.0, 0.0])
            e[0] += secs
            e[1] += mah
        rows = sorted(merged.items(), key=lambda kv: -kv[1][1])[:12]
        if not rows:
            self.apps.append(label("nothing yet", "bt-row-sub"))
        top = max((v[1] for _, v in rows), default=1) or 1
        for a, (secs, mah) in rows:
            row = vbox(4, "bt-row")
            line = hbox(8)
            line.append(label(a, "bt-row-name", hexpand=True))
            line.append(label(f"{dur(secs)} · {pct(mah):.1f}%", "bt-row-sub", 1.0, False))
            row.append(line)
            bar = Gtk.LevelBar(min_value=0, max_value=1, value=max(0, mah) / top)
            bar.add_css_class("bt-bar")
            row.append(bar)
            self.apps.append(row)

        for c in list(self.heat):
            self.heat.remove(c)
        if hot[0]:
            self.heat.append(self.kv("hottest skin", f"{hot[0]:.0f} °C at {time.strftime('%H:%M', time.localtime(hot[1]))}"))
        for name, cs in sorted(procs.items(), key=lambda kv: -kv[1])[:6]:
            self.heat.append(self.kv(name, f"{cs / 60:.0f} CPU-min"))
        return False

    def kv(self, k, v):
        line = hbox(8, "bt-row")
        line.append(label(k, "bt-row-name", hexpand=True))
        line.append(label(v, "bt-row-sub", 1.0, False))
        return line

    def on_theme(self):
        (self.add_css_class if notareal_theme.value("corners", "1") == "0" else self.remove_css_class)("square")
        self.refresh()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
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
