#!/usr/bin/env python3
"""Sound: ring mode + two volume sliders (10-13, mode 10-19).

MODE = feedbackd's profile: RING = full, VIBRATE = quiet (buzz, no sound), SILENT = silent
(LED only). Same setting as the bell tile in the Phosh shade; alarms still ring (alarmd's own paplay).

MEDIA = the default sink's volume (same as Phosh's slider and the volume keys).
NOTIFICATIONS = ~/.config/nrvolume.json "notify", applied by nrvolumed.py.
Left-handed: the mute / test buttons sit left of the sliders.
"""

import json
import os
import subprocess
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

import pulsectl  # noqa: E402
import notareal_theme  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP_ID = "dev.notareal.Sound"
CONF = os.path.expanduser("~/.config/nrvolume.json")
PING = "/usr/share/sounds/phosh/stereo/message-new-instant.oga"
MODES = (("full", "RING", "audio-volume-high-symbolic"),
         ("quiet", "VIBRATE", "phone-symbolic"),
         ("silent", "SILENT", "notifications-disabled-symbolic"))


def read_notify():
    try:
        with open(CONF) as f:
            return float(json.load(f).get("notify", 0.6))
    except (OSError, ValueError, TypeError):
        return 0.6


def write_notify(v):
    tmp = CONF + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"notify": round(v, 3)}, f)
    os.replace(tmp, CONF)


def ping():
    # tagged like feedbackd's sounds, so it plays at the notification volume
    subprocess.Popen(["paplay", "--property=media.role=event", PING],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Sound", default_width=400, default_height=420)
        self.pulse = pulsectl.Pulse("nrvolume")
        self.busy = False          # True while we set a slider from code
        self.ping_id = 0

        hb = Adw.HeaderBar()
        hb.add_css_class("vapor-bar")
        t = Gtk.Label(label="NOTREAL.SOUND")
        t.add_css_class("app-title")
        hb.set_title_widget(t)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=18,
                      margin_bottom=18, margin_start=16, margin_end=16)
        self.mode_row(box)
        self.media_btn, self.media = self.row(box, "MEDIA", "music, videos, voice", "audio-volume-high-symbolic")
        self.note_btn, self.note = self.row(box, "NOTIFICATIONS", "messages, mail, ringtone",
                                            "preferences-system-notifications-symbolic")
        self.media_btn.connect("clicked", self.toggle_mute)
        self.note_btn.connect("clicked", lambda *a: ping())
        self.note.set_value(read_notify() * 100)   # before connecting: no ping on open
        self.media.connect("value-changed", self.media_moved)
        self.note.connect("value-changed", self.note_moved)

        tv = Adw.ToolbarView()
        tv.add_top_bar(hb)
        tv.set_content(box)
        self.set_content(tv)

        self.sync()
        GLib.timeout_add(500, self.sync)

    def row(self, box, title, sub, icon):
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=14)
        lb = Gtk.Label(label=title, xalign=0)
        lb.add_css_class("sheet-title")
        head.append(lb)
        s = Gtk.Label(label=sub, xalign=0)
        s.add_css_class("dim-label")
        head.append(s)
        box.append(head)
        line = Gtk.Box(spacing=8)
        btn = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER)
        btn.add_css_class("flat")
        btn.add_css_class("vol-btn")
        sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        sc.set_hexpand(True)
        sc.set_draw_value(True)
        sc.set_value_pos(Gtk.PositionType.RIGHT)
        sc.set_format_value_func(lambda s, v: "%d%%" % v)
        sc.add_css_class("vol-scale")
        line.append(btn)
        line.append(sc)
        box.append(line)
        return btn, sc

    # --- mode = feedbackd profile ------------------------------------------------------
    def mode_row(self, box):
        lb = Gtk.Label(label="MODE", xalign=0)
        lb.add_css_class("sheet-title")
        box.append(lb)
        line = Gtk.Box(homogeneous=True, margin_top=4)
        line.add_css_class("linked")
        self.fb = Gio.Settings.new("org.sigxcpu.feedbackd")
        self.mode_btns = {}
        group = None
        for prof, label, icon in MODES:
            b = Gtk.ToggleButton(group=group)
            group = group or b
            c = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6)
            c.append(Gtk.Image(icon_name=icon))
            c.append(Gtk.Label(label=label))
            b.set_child(c)
            b.connect("toggled", self.mode_picked, prof)
            self.mode_btns[prof] = b
            line.append(b)
        box.append(line)
        self.mode_synced()
        self.fb.connect("changed::profile", lambda *a: self.mode_synced())

    def mode_synced(self):
        b = self.mode_btns.get(self.fb.get_string("profile"))
        if b and not b.get_active():
            self.busy = True
            b.set_active(True)
            self.busy = False

    def mode_picked(self, b, prof):
        if self.busy or not b.get_active() or self.fb.get_string("profile") == prof:
            return
        self.fb.set_string("profile", prof)
        if prof == "quiet":
            # a buzz so you feel the switch
            subprocess.Popen(["fbcli", "-E", "message-new-instant"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # --- media = default sink -----------------------------------------------------------
    def sink(self):
        return self.pulse.get_sink_by_name(self.pulse.server_info().default_sink_name)

    def sync(self):
        try:
            s = self.sink()
            # during a notification nrvolumed raises the sink; show the real media volume
            st = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "nrvolumed.state")
            v = s.volume.value_flat
            if os.path.exists(st):
                with open(st) as f:
                    r = json.load(f)
                if r.get("sink") == s.name:
                    v = r["m"]
            if abs(self.media.get_value() - v * 100) >= 1 and not self.media.has_focus():
                self.busy = True
                self.media.set_value(round(v * 100))
                self.busy = False
            self.media_btn.set_icon_name("audio-volume-muted-symbolic" if s.mute else "audio-volume-high-symbolic")
        except (pulsectl.PulseError, OSError, ValueError, KeyError):
            pass
        return True

    def media_moved(self, sc):
        if self.busy:
            return
        try:
            s = self.sink()
            self.pulse.volume_set_all_chans(s, sc.get_value() / 100)
            if s.mute:
                self.pulse.mute(s, False)
        except pulsectl.PulseError:
            pass

    def toggle_mute(self, *_):
        try:
            s = self.sink()
            self.pulse.mute(s, not s.mute)
        except pulsectl.PulseError:
            pass
        self.sync()

    # --- notifications = nrvolumed's config ----------------------------------------------
    def note_moved(self, sc):
        write_notify(sc.get_value() / 100)
        if self.ping_id:
            GLib.source_remove(self.ping_id)
        self.ping_id = GLib.timeout_add(400, self.ping_now)

    def ping_now(self):
        self.ping_id = 0
        ping()
        return False


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
