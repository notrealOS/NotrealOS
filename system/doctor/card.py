#!/usr/bin/env python3
"""notreal.check: the doctor's card (10-24). Tap the face on the overview widget -> this.

Shows the last nrdoctor result in plain language (what needs you, what it fixed by itself,
what's worth knowing - each with why it matters and what to do), today's battery, the week,
and three buttons on the left: CHECK AGAIN, ASK THE PC (ask.py: Qwen on the PC reads a health
snapshot; its suggested fixes come from nrdoctor.ACTIONS and run only on a tap), SAVE REPORT
(a snapshot for Claude in ~/doctor/reports/).
"""
import json
import os
import subprocess
import sys
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.expanduser("~/theme"))
import notareal_theme  # noqa: E402
import trends  # noqa: E402

APP_ID = "dev.notareal.Check"
RUN = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
LAST = os.path.join(RUN, "nrdoctor-last.json")
ASKED = os.path.join(RUN, "nrdoctor-ask.json")
DOCTOR = os.path.join(HERE, "nrdoctor.py")
ASK = os.path.join(HERE, "ask.py")
REPORTS = os.path.join(HERE, "reports")


def label(text="", css=None, xalign=0.0, wrap=True):
    lb = Gtk.Label(label=text, xalign=xalign)
    if wrap:
        lb.set_wrap(True)
        lb.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    for c in (css or "").split():
        lb.add_css_class(c)
    return lb


def vbox(spacing=0, css=None, **kw):
    b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kw)
    for c in (css or "").split():
        b.add_css_class(c)
    return b


def bg(work, done):
    def run():
        try:
            r = work()
        except Exception as e:  # noqa: BLE001
            r = e
        GLib.idle_add(lambda: (done(r), False)[1])
    threading.Thread(target=run, daemon=True).start()


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def ago(t):
    m = int((time.time() - t) // 60)
    return "just now" if m < 1 else f"{m} min ago" if m < 60 else time.strftime("%H:%M", time.localtime(t))


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="System check", default_width=540, default_height=1100)
        self.toasts = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=True)
        hb.set_title_widget(label("SYSTEM CHECK", "ck-title", 0.5, False))
        tv.add_top_bar(hb)
        page = vbox(14, "ck-page")
        self.hero = label("", "ck-hero")
        self.sub = label("", "ck-sub")
        page.append(self.hero)
        page.append(self.sub)
        # left-handed: the buttons start on the left
        btns = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3, column_spacing=8,
                           row_spacing=8, homogeneous=False)
        self.check_btn = self.button("CHECK AGAIN", self.check_again, "primary")
        self.ask_btn = self.button("ASK THE PC", self.ask_pc)
        self.report_btn = self.button("SAVE REPORT", self.save_report)
        for b in (self.check_btn, self.ask_btn, self.report_btn):
            btns.append(b)
        page.append(btns)
        self.ask_box = vbox(8)
        page.append(self.ask_box)
        self.body = vbox(14)
        page.append(self.body)
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(page)
        tv.set_content(sc)
        self.toasts.set_child(tv)
        self.set_content(self.toasts)
        notareal_theme.on_change(self.on_theme)
        self.on_theme()
        self.show_ask(read_json(ASKED), only_recent=True)

    def button(self, text, cb, css=""):
        b = Gtk.Button(label=text, css_classes=["ck-btn"] + ([css] if css else []))
        b.connect("clicked", lambda *_: cb())
        return b

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def on_theme(self):
        (self.add_css_class if notareal_theme.value("corners", "1") == "0" else self.remove_css_class)("square")
        self.render()

    # --- the result ---------------------------------------------------------------------
    def render(self):
        res = read_json(LAST)
        for c in list(self.body):
            self.body.remove(c)
        if not res:
            self.hero.set_text("NO CHECK YET")
            self.sub.set_text("Tap CHECK AGAIN.")
            return
        bad = res.get("failed", []) + res.get("manual", [])
        self.hero.set_text(f"{len(bad)} NEED{'S' if len(bad) == 1 else ''} YOU" if bad else
                           "ALL GOOD" if not res.get("minor") else "ALL WORKING")
        for c in ("bad", "ok", "meh"):
            self.hero.remove_css_class(c)
        self.hero.add_css_class("bad" if bad else "meh" if res.get("minor") else "ok")
        mode = f" · {res['mode']} on" if res.get("mode") else ""
        self.sub.set_text(f"checked {ago(res.get('time', time.time()))} · {len(res.get('ok', []))} things fine{mode}")
        det = res.get("details", {})
        self.section("NEEDS YOU", bad, det, "bad")
        self.section("FIXED BY ITSELF", res.get("fixed", []), det, "fixed", todo=False)
        self.section("WORTH KNOWING", res.get("minor", []), det, "meh")
        self.battery_section()
        n = len([f for f in os.listdir(REPORTS)]) if os.path.isdir(REPORTS) else 0
        if n:
            self.body.append(label(f"{n} report{'s' if n > 1 else ''} saved for Claude in ~/doctor/reports", "ck-foot"))

    def section(self, title, names, det, css, todo=True):
        if not names:
            return
        self.body.append(label(title, "ck-head " + css, wrap=False))
        for n in names:
            card = vbox(4, "ck-card " + css)
            card.append(label(n, "ck-name"))
            d = det.get(n) or {}
            if d.get("why"):
                card.append(label(d["why"], "ck-why"))
            if todo and d.get("todo"):
                card.append(label("→ " + d["todo"], "ck-todo"))
            self.body.append(card)

    def battery_section(self):
        try:
            t = trends.summary(trends.today())
            week = trends.text_report(7)
        except Exception as e:  # noqa: BLE001
            self.body.append(label(f"battery log unreadable: {e}", "ck-why"))
            return
        self.body.append(label("BATTERY TODAY", "ck-head", wrap=False))
        h, r = t["hours"], t["rate"]
        lines = [f"Screen on {h['on']:.1f} h" + (f" · {r['on']:.0f} %/h" if r["on"] else ""),
                 f"Asleep {h['sleep']:.1f} h" + (f" · {r['sleep']:.1f} %/h" if r["sleep"] else ""),
                 f"Screen off but awake {h['off'] * 60:.0f} min" + (f" · {r['off']:.1f} %/h" if r["off"] else "")]
        if t["apps"]:
            lines.append("Most battery: " + ", ".join(f"{a} {p:.0f}%" for a, _, p in t["apps"][:3]))
        card = vbox(4, "ck-card")
        for line in lines:
            card.append(label(line, "ck-why"))
        self.body.append(card)
        self.body.append(label("THIS WEEK", "ck-head", wrap=False))
        card = vbox(4, "ck-card")
        card.append(label(week, "ck-why"))
        self.body.append(card)

    # --- buttons ------------------------------------------------------------------------
    def busy(self, btn, text):
        btn.set_sensitive(False)
        btn.set_label(text)

    def check_again(self):
        self.busy(self.check_btn, "CHECKING…")

        def done(r):
            self.check_btn.set_sensitive(True)
            self.check_btn.set_label("CHECK AGAIN")
            if isinstance(r, Exception):
                self.toast(f"check failed: {r}")
            self.render()
        bg(lambda: subprocess.run(["python3", DOCTOR, "--json"], capture_output=True, timeout=180), done)

    def ask_pc(self):
        self.busy(self.ask_btn, "ASKING…")
        for c in list(self.ask_box):
            self.ask_box.remove(c)
        self.ask_box.append(label("Asking the PC… the first question loads its model (~40 s).", "ck-sub"))

        def work():
            out = subprocess.run(["python3", ASK], capture_output=True, text=True, timeout=240).stdout
            return json.loads(out)

        def done(r):
            self.ask_btn.set_sensitive(True)
            self.ask_btn.set_label("ASK THE PC")
            if isinstance(r, Exception):
                r = {"error": f"Couldn't ask the PC: {r}"}
            self.show_ask(r)
        bg(work, done)

    def show_ask(self, r, only_recent=False):
        for c in list(self.ask_box):
            self.ask_box.remove(c)
        if not r or (only_recent and time.time() - r.get("time", 0) > 1800):
            return
        card = vbox(6, "ck-card ask")
        card.append(label("THE PC SAYS", "ck-head", wrap=False))
        if r.get("error"):
            card.append(label(r["error"], "ck-why"))
        else:
            card.append(label(r.get("summary", ""), "ck-name"))
            if r.get("cause"):
                card.append(label("Cause: " + r["cause"], "ck-why"))
            for s in r.get("steps", []):
                card.append(label("→ " + s, "ck-todo"))
            for aid in r.get("actions", []):
                b = Gtk.Button(label=r.get("labels", {}).get(aid, aid).upper(), css_classes=["ck-btn", "primary"],
                               halign=Gtk.Align.START)
                b.connect("clicked", lambda btn, a=aid: self.run_action(btn, a))
                card.append(b)
        self.ask_box.append(card)

    def run_action(self, btn, aid):
        btn.set_sensitive(False)

        def done(r):
            try:
                msg = json.loads(r.stdout)["msg"]
            except Exception:  # noqa: BLE001
                msg = "action failed"
            self.toast(msg)
            self.check_again()
        bg(lambda: subprocess.run(["python3", DOCTOR, "--do", aid], capture_output=True, text=True, timeout=120), done)

    def save_report(self):
        self.busy(self.report_btn, "SAVING…")

        def done(r):
            self.report_btn.set_sensitive(True)
            self.report_btn.set_label("SAVE REPORT")
            path = r.stdout.strip() if not isinstance(r, Exception) else ""
            self.toast(f"Saved for Claude: {os.path.basename(path)}" if path else "couldn't save the report")
            self.render()
        bg(lambda: subprocess.run(["python3", DOCTOR, "--report", "saved from the card"], capture_output=True,
                                  text=True, timeout=60), done)


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
        else:
            self.win.render()          # opened again from the face: show the newest result
        self.win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv[:1]))
