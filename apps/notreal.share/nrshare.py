#!/usr/bin/env python3
"""notarealShare: send files to a chat (notarealChat) or attach them to a new mail (vaporMail).

Opened with files: from the file manager (right-click > Open With > Share, or Scripts > Share),
or by our own apps (`nrshare FILE...`). Left-handed: everything is left-aligned, MAIL first.
Chats = notarealChat's Matrix chats (WhatsApp / Messenger / Instagram), newest first; SMS chats
can't carry files, so they are left out.
"""
import os
import sqlite3
import subprocess
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.expanduser("~/theme"))
import notareal_theme  # noqa: E402

APP_ID = "dev.notareal.Share"
CHAT_DB = os.path.expanduser("~/.local/share/notarealchat/chat.db")
NETS = {"wa": "WA", "fb": "FB", "ig": "IG"}


def chats():
    try:
        db = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT id, net, name, last_ts FROM chats WHERE hidden=0 AND archived=0 "
                          "ORDER BY last_ts DESC LIMIT 60").fetchall()
        db.close()
    except sqlite3.Error as e:
        print("nrshare: chats:", e, flush=True)
        return []
    return [dict(r) for r in rows if r["net"] in NETS and r["name"]]   # no SMS, no bridge bot rooms


def label(text, css=None, xalign=0.0):
    lb = Gtk.Label(label=text, xalign=xalign, ellipsize=3)
    if css:
        lb.add_css_class(css)
    return lb


class Window(Adw.ApplicationWindow):
    def __init__(self, app, paths):
        super().__init__(application=app, title="Share")
        self.set_default_size(360, 720)
        self.paths = paths
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        close = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat"])
        close.connect("clicked", lambda *_: self.close())
        hb.pack_end(close)
        n = len(paths)
        hb.set_title_widget(label(f"SHARE {n} FILE{'S' if n != 1 else ''}", "app-title", 0.5))
        tv.add_top_bar(hb)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=14,
                      margin_end=14, margin_top=10, margin_bottom=10)
        # what's being shared: thumbnails for pictures, names for the rest
        strip = Gtk.Box(spacing=8)
        for p in paths[:4]:
            strip.append(self._preview(p))
        if n > 4:
            strip.append(label(f"+{n - 4}", "share-more"))
        sw0 = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, child=strip)
        sw0.set_size_request(-1, 88)
        box.append(sw0)

        mail = Gtk.Button(css_classes=["share-target", "mail"])
        mrow = Gtk.Box(spacing=12)
        mrow.append(Gtk.Image(icon_name="mail-send-symbolic", pixel_size=22))
        mrow.append(label("NEW MAIL WITH ATTACHMENT", "share-name"))
        mail.set_child(mrow)
        mail.connect("clicked", lambda *_: self.to_mail())
        box.append(mail)

        self.search = Gtk.SearchEntry(placeholder_text="Search chats")
        self.search.connect("search-changed", lambda *_: self.listbox.invalidate_filter())
        box.append(self.search)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["share-list"])
        self.listbox.set_filter_func(self._filter)
        for c in chats():
            self.listbox.append(self._chat_row(c))
        sw = Gtk.ScrolledWindow(vexpand=True, child=self.listbox,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        box.append(sw)
        tv.set_content(box)
        self.set_content(tv)

    def _preview(self, path):
        mime = Gio.content_type_guess(path, None)[0] or ""
        if mime.startswith("image/"):
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 160, 160, True)
                pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, css_classes=["share-thumb"])
                pic.set_paintable(Gdk.Texture.new_for_pixbuf(pb))
                pic.set_size_request(80, 80)
                return pic
            except GLib.Error:
                pass
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["share-file"])
        b.set_size_request(80, 80)
        icon = Gio.content_type_get_icon(mime) if mime else None
        b.append(Gtk.Image(gicon=icon, pixel_size=32) if icon else Gtk.Image(icon_name="text-x-generic"))
        b.append(label(os.path.basename(path), "share-fname", 0.5))
        return b

    def _chat_row(self, c):
        row = Gtk.ListBoxRow()
        row.chat = c
        btn = Gtk.Button(css_classes=["share-target"])
        h = Gtk.Box(spacing=12)
        initials = "".join(w[0] for w in c["name"].split()[:2]).upper() or "?"
        h.append(label(initials, "share-avatar", 0.5))
        h.append(label(c["name"], "share-name"))
        net = NETS.get((c["net"] or "").lower(), (c["net"] or "")[:3].upper())
        if net:
            h.append(label(net, "share-net"))
        btn.set_child(h)
        btn.connect("clicked", lambda *_: self.to_chat(c))
        row.set_child(btn)
        return row

    def _filter(self, row):
        q = self.search.get_text().strip().lower()
        return not q or q in row.chat["name"].lower()

    def _launch(self, argv):
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        GLib.timeout_add(400, lambda: (self.close(), False)[1])

    def to_chat(self, c):
        self._launch(["python3", os.path.expanduser("~/chat/chat.py"), "--send", c["id"], *self.paths])

    def to_mail(self):
        self._launch(["python3", os.path.expanduser("~/vapormail/vapormail.py"), "--attach", *self.paths])


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_OPEN | Gio.ApplicationFlags.NON_UNIQUE)

    def do_startup(self):
        Adw.Application.do_startup(self)
        notareal_theme.apply(os.path.join(HERE, "style.css"))

    def do_open(self, files, n, hint):
        paths = [f.get_path() for f in files if f.get_path() and os.path.isfile(f.get_path())]
        if paths:
            Window(self, paths).present()

    def do_activate(self):
        # no files: nothing to share
        print("usage: nrshare FILE...", flush=True)


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
