#!/usr/bin/env python3
"""notreal.system: the phone's own stuff in one app (10-24) - CHECK, BATTERY, SOUND, GLYPH.

CHECK, BATTERY and SOUND are the existing apps' screens (~/doctor/card.py, ~/battery/nrbattery.py,
~/nrvolume/nrvolume.py): each Window is built off-screen and its content moved into a tab here,
so the apps stay the one place their code lives. GLYPH is new: AMBIENT / TORCH / FLASH over
glyphd's D-Bus API. Left-handed: the tab row sits bottom-left.

nrsystem [--page check|battery|sound|glyph]   (a second launch switches the open window's tab)
"""
import importlib.util
import os
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
sys.path.insert(0, os.path.join(HOME, "theme"))
import notareal_theme  # noqa: E402

APP_ID = "dev.notareal.System"
PAGES = (  # tab, key, module file (None = built here)
    ("CHECK", "check", "doctor/card.py"),
    ("BATTERY", "battery", "battery/nrbattery.py"),
    ("SOUND", "sound", "nrvolume/nrvolume.py"),
    ("GLYPH", "glyph", None),
)
GLYPH = ("org.notarealinux.Glyph", "/org/notarealinux/Glyph", "org.notarealinux.Glyph")


def load(rel):
    path = os.path.join(HOME, rel)
    sys.path.insert(0, os.path.dirname(path))
    spec = importlib.util.spec_from_file_location("nrsys_" + os.path.basename(path)[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def label(text, css, xalign=0.0):
    lb = Gtk.Label(label=text, xalign=xalign, wrap=True)
    for c in css.split():
        lb.add_css_class(c)
    return lb


class GlyphPage(Gtk.Box):
    """AMBIENT (slow random fades) and TORCH (every light on) toggles + a FLASH test."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        hb.set_title_widget(label("GLYPH", "ck-title", 0.5))
        tv = Adw.ToolbarView()
        tv.add_top_bar(hb)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, css_classes=["ck-page"])
        page.append(label("The lights on the back.", "ck-sub"))
        self.toggles = {}
        for prop, method, title, why in (
                ("Ambient", "ToggleAmbient", "AMBIENT", "Slow random fades, like a lava lamp."),
                ("Torch", "ToggleTorch", "TORCH", "Every light on, full: a soft lamp.")):
            card = Gtk.Box(spacing=14, css_classes=["ck-card"])
            # left-handed: the switch on the left
            sw = Gtk.Switch(valign=Gtk.Align.CENTER)
            sw.connect("state-set", self._flip, method)
            card.append(sw)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            text.append(label(title, "ck-name"))
            text.append(label(why, "ck-why"))
            card.append(text)
            page.append(card)
            self.toggles[prop] = sw
        flash = Gtk.Button(label="FLASH", css_classes=["ck-btn"], halign=Gtk.Align.START)
        flash.connect("clicked", lambda *_: self._call("Flash"))
        page.append(flash)
        self.note = label("", "ck-foot")
        page.append(self.note)
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=page)
        tv.set_content(sc)
        self.append(tv)
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self._syncing = False

    def _call(self, method, then=None):
        def done(bus, res):
            try:
                bus.call_finish(res)
            except GLib.Error:
                self.note.set_label("glyphd isn't running")
            if then:
                then()
        self.bus.call(*GLYPH, method, None, None, Gio.DBusCallFlags.NONE, 2000, None, done)

    def _flip(self, sw, state, method):
        if self._syncing:
            return False
        sw.set_sensitive(False)
        self._call(method, lambda: (sw.set_sensitive(True), self.refresh()))
        return True                 # the real state comes from glyphd in refresh()

    def refresh(self):
        for prop, sw in self.toggles.items():
            def got(bus, res, sw=sw):
                try:
                    on = bus.call_finish(res).unpack()[0]
                except GLib.Error:
                    self.note.set_label("glyphd isn't running")
                    return
                self.note.set_label("")
                self._syncing = True
                sw.set_state(on)
                sw.set_active(on)
                self._syncing = False
            self.bus.call(GLYPH[0], GLYPH[1], "org.freedesktop.DBus.Properties", "Get",
                          GLib.Variant("(ss)", (GLYPH[2], prop)), GLib.VariantType("(v)"),
                          Gio.DBusCallFlags.NONE, 2000, None, got)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="System", default_width=540, default_height=1100)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.donors, self.tabs = {}, {}
        for title, key, rel in PAGES:
            if rel:
                try:
                    donor = load(rel).Window(None)        # never shown: we take its content
                except Exception as e:  # noqa: BLE001 - one broken page must not take the hub down
                    print(f"nrsystem: {key} failed: {e!r}", flush=True)
                    self.stack.add_named(label(f"{title} couldn't load:\n{e}", "ck-sub", 0.5), key)
                    continue
                content = donor.get_content()
                donor.set_content(None)
                self.donors[key] = donor
                self.stack.add_named(content, key)
            else:
                self.glyph = GlyphPage()
                self.stack.add_named(self.glyph, key)

        tabs = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=8, margin_bottom=14,
                       halign=Gtk.Align.START)
        group = None
        for title, key, _rel in PAGES:
            b = Gtk.ToggleButton(label=title, css_classes=["sys-tab"], group=group)
            group = group or b
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self.show(k))
            tabs.append(b)
            self.tabs[key] = b
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.stack)
        body.append(Gtk.ScrolledWindow(child=tabs, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                       hscrollbar_policy=Gtk.PolicyType.EXTERNAL, propagate_natural_height=True))
        self.set_content(body)
        notareal_theme.on_change(self.on_theme)
        self.on_theme()
        self.connect("notify::is-active", lambda *_: self.is_active() and self.refresh())

    def on_theme(self):
        (self.add_css_class if notareal_theme.value("corners", "1") == "0" else self.remove_css_class)("square")

    def show(self, key):
        if key not in self.tabs:
            key = "check"
        if not self.tabs[key].get_active():
            self.tabs[key].set_active(True)          # comes back here through "toggled"
            return
        self.stack.set_visible_child_name(key)
        self.refresh()

    def refresh(self):
        key = self.stack.get_visible_child_name()
        donor = self.donors.get(key)
        if key == "glyph":
            self.glyph.refresh()
        elif key == "check" and donor:
            donor.render()
        elif key == "battery" and donor:
            donor.refresh()
            donor.sync_saver()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        for css in ("doctor/style.css", "battery/style.css", "nrvolume/style.css"):
            notareal_theme.apply(os.path.join(HOME, css))
        notareal_theme.apply(os.path.join(HERE, "style.css"))

    def do_command_line(self, cmd):
        args = cmd.get_arguments()[1:]
        page = args[args.index("--page") + 1] if "--page" in args[:-1] else None
        if not self.win:
            self.win = Window(self)
            page = page or "check"
        if page:
            self.win.show(page)
        self.win.present()
        return 0


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
