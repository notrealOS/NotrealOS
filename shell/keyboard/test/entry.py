#!/usr/bin/env python3
"""Test window for the nested compositor: a text view and a single-line entry."""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio

def act(app):
    w = Gtk.ApplicationWindow(application=app, title="swipe test")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    e = Gtk.Entry(placeholder_text="single line")
    term = Gtk.Entry(placeholder_text="terminal (shortcut bar)", input_purpose=Gtk.InputPurpose.TERMINAL)
    tv = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD, vexpand=True)
    box.append(term); box.append(e); box.append(tv)
    w.set_child(box); w.present()

app = Gtk.Application(application_id="dev.notareal.SwipeTest", flags=Gio.ApplicationFlags.NON_UNIQUE)
app.connect("activate", act)
app.run([])
