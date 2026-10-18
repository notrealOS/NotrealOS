#!/usr/bin/env python3
"""notarealContacts: Google Contacts (via Evolution) with a fast list and a big-step "new contact" flow.

List: A-Z sections, search, a letter scrubber on the LEFT edge and the NEW button bottom-left
(left-handed user). Opens from the cache instantly, then refreshes from Evolution.
Contact: CALL / SMS / WHATSAPP / MAIL (calls -> GNOME Calls via tel:, messages -> notarealChat,
mail -> vaporMail). New contact: full-screen steps NAME -> PHONE -> (EMAIL), FINISH at any step
after the name. Saved to the Google book; Evolution syncs it to Google.
"""
import os
import subprocess
import sys
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.expanduser("~/theme"))
import notareal_theme  # noqa: E402
import book  # noqa: E402

APP_ID = "dev.notareal.Contacts"
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ#"
_textures = {}


def bg(work, done=None, fail=None):
    def run():
        try:
            r = work()
        except Exception as e:     # noqa: BLE001 - shown to the user
            print("nrcontacts:", repr(e), flush=True)
            if fail:
                GLib.idle_add(fail, e)
            return
        if done:
            GLib.idle_add(done, r)
    threading.Thread(target=run, daemon=True).start()


def letter_of(name):
    ch = (name[:1] or "#").upper()
    # fold accents (Á -> A) for the index
    import unicodedata
    ch = unicodedata.normalize("NFD", ch)[0]
    return ch if ch in LETTERS[:-1] else "#"


def label(text="", css=None, xalign=0.0, ellipsize=True):
    lb = Gtk.Label(label=text, xalign=xalign)
    if ellipsize:
        lb.set_ellipsize(Pango.EllipsizeMode.END)
    if css:
        for c in css.split():
            lb.add_css_class(c)
    return lb


def initials(name):
    parts = [w for w in name.split() if w[:1].isalnum()]
    return ("".join(w[0] for w in parts[:2]) or name[:1] or "?").upper()


def texture(path):
    if path not in _textures:
        try:
            _textures[path] = Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            _textures[path] = None
    return _textures[path]


def avatar(person, size):
    """Photo if there is one, else initials on a tinted circle."""
    ov = Gtk.Overlay(css_classes=["avatar"], halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
    ov.set_size_request(size, size)
    ini = label(initials(person["name"]), "avatar-ini", 0.5, False)
    ini.set_size_request(size, size)
    ov.set_child(ini)
    if person.get("photo"):
        tex = texture(person["photo"])
        if tex:
            pic = Gtk.Picture(paintable=tex, content_fit=Gtk.ContentFit.COVER, css_classes=["avatar-pic"])
            pic.set_size_request(size, size)
            ov.add_overlay(pic)
    ov.set_overflow(Gtk.Overflow.HIDDEN)
    return ov


def launch(argv):
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


class Person(GObject.Object):
    def __init__(self, d):
        super().__init__()
        self.d = d
        self.key = d["name"].lower()
        self.letter = letter_of(d["name"])


# --- list --------------------------------------------------------------------------------------

class ListPage(Adw.NavigationPage):
    def __init__(self, win):
        super().__init__(title="Contacts", tag="list")
        self.win = win
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        hb.add_css_class("vapor-bar")
        hb.set_title_widget(label("NOTREAL.CONTACTS", "app-title", 0.5, False))
        tv.add_top_bar(hb)

        self.store = Gio.ListStore(item_type=Person)
        self.filter = Gtk.CustomFilter.new(self._match)
        filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        sorter = Gtk.CustomSorter.new(lambda a, b, _d: (a.letter > b.letter) - (a.letter < b.letter), None)
        self.model = Gtk.SortListModel(model=filtered, section_sorter=sorter)
        sel = Gtk.NoSelection(model=self.model)

        fac = Gtk.SignalListItemFactory()
        fac.connect("setup", self._setup)
        fac.connect("bind", self._bind)
        hfac = Gtk.SignalListItemFactory()
        hfac.connect("setup", lambda _f, h: h.set_child(label("", "section", 0.0, False)))
        hfac.connect("bind", lambda _f, h: h.get_child().set_label(h.get_item().letter))
        self.view = Gtk.ListView(model=sel, factory=fac, header_factory=hfac, single_click_activate=True,
                                 css_classes=["people"])
        self.view.connect("activate", self._activate)
        sw = Gtk.ScrolledWindow(child=self.view, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.sw = sw

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.search = Gtk.SearchEntry(placeholder_text="Search name, number or email",
                                      margin_start=14, margin_end=14, margin_top=6, margin_bottom=6)
        self.search.add_css_class("find")
        self.search.connect("search-changed", lambda *_: self.filter.changed(Gtk.FilterChange.DIFFERENT))
        body.append(self.search)
        self.status = label("", "status", 0.5)
        self.status.set_visible(False)
        body.append(self.status)

        row = Gtk.Box()
        row.append(self._scrubber())      # left edge: thumb reach (left-handed)
        row.append(sw)
        body.append(row)
        sw.set_hexpand(True)

        ov = Gtk.Overlay(child=body)
        new = Gtk.Button(css_classes=["fab"], halign=Gtk.Align.START, valign=Gtk.Align.END,
                         margin_start=40, margin_bottom=22)
        nb = Gtk.Box(spacing=8)
        nb.append(Gtk.Image(icon_name="list-add-symbolic"))
        nb.append(label("NEW", None, 0.5, False))
        new.set_child(nb)
        new.connect("clicked", lambda *_: win.new_contact())
        ov.add_overlay(new)
        tv.set_content(ov)
        self.set_child(tv)

    def _scrubber(self):
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["scrubber"], vexpand=True)
        for ch in LETTERS:
            col.append(label(ch, "scrub-letter", 0.5, False))
        self.bubble = label("", "scrub-bubble", 0.5, False)
        drag = Gtk.GestureDrag()

        def at(y):
            h = col.get_height() or 1
            i = max(0, min(len(LETTERS) - 1, int(y / h * len(LETTERS))))
            self.jump(LETTERS[i])
        state = {}
        drag.connect("drag-begin", lambda g, x, y: (state.update(y0=y), at(y)))
        drag.connect("drag-update", lambda g, dx, dy: at(state["y0"] + dy))
        col.add_controller(drag)
        col.set_size_request(26, -1)
        return col

    def jump(self, ch):
        for i in range(self.model.get_n_items()):
            if self.model.get_item(i).letter >= ch:
                self.view.scroll_to(i, Gtk.ListScrollFlags.NONE, None)
                return

    def _match(self, p):
        q = self.search.get_text().strip().lower()
        if not q:
            return True
        d = p.d
        if q in p.key or q in d.get("org", "").lower():
            return True
        digits = "".join(c for c in q if c.isdigit())
        if len(digits) >= 3 and any(digits in "".join(c for c in t["e164"] + t["value"] if c.isdigit())
                                    for t in d["tels"]):
            return True
        return any(q in e.lower() for e in d["emails"])

    def _setup(self, _f, item):
        h = Gtk.Box(spacing=12, css_classes=["person"])
        item.set_child(h)

    def _bind(self, _f, item):
        h, d = item.get_child(), item.get_item().d
        while h.get_first_child():
            h.remove(h.get_first_child())
        h.append(avatar(d, 40))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        col.append(label(d["name"], "p-name"))
        sub = d["tels"][0]["value"] if d["tels"] else (d["emails"][0] if d["emails"] else d.get("org", ""))
        if sub:
            col.append(label(sub, "p-sub"))
        h.append(col)

    def _activate(self, _view, pos):
        self.win.open_person(self.model.get_item(pos).d)

    def fill(self, people):
        items = [Person(d) for d in people]
        self.store.splice(0, self.store.get_n_items(), items)


# --- one contact ---------------------------------------------------------------------------------

class PersonPage(Adw.NavigationPage):
    def __init__(self, win, d):
        super().__init__(title=d["name"])
        self.win, self.d = win, d
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        edit = Gtk.Button(label="EDIT", css_classes=["chip"])
        edit.connect("clicked", lambda *_: win.edit_contact(d))
        hb.pack_start(edit)
        delete = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat"])
        delete.connect("clicked", lambda *_: self.confirm_delete())
        hb.pack_end(delete)
        tv.add_top_bar(hb)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=18, margin_end=18,
                      margin_top=10, margin_bottom=20)
        box.append(avatar(d, 120))
        box.append(label(d["name"], "big-name", 0.5))
        if d.get("org"):
            box.append(label(d["org"], "p-sub", 0.5))
        first = d["tels"][0]["e164"] if d["tels"] else None
        acts = Gtk.Box(spacing=10, halign=Gtk.Align.START)          # left-handed
        if first:
            acts.append(self._action("call-start-symbolic", "CALL", lambda: self.call(first)))
            acts.append(self._action("mail-unread-symbolic", "SMS", lambda: self.sms(first)))
            acts.append(self._action("chat-message-new-symbolic", "WHATSAPP", lambda: self.whatsapp(first)))
        if d["emails"]:
            acts.append(self._action("mail-send-symbolic", "MAIL", lambda: self.mail(d["emails"][0])))
        box.append(acts)

        for t in d["tels"]:
            r = Gtk.Box(spacing=8, css_classes=["detail-row"])
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            col.append(label(t["value"], "d-value"))
            col.append(label(t["kind"].upper() or "PHONE", "d-kind"))
            for icon, cb in (("call-start-symbolic", self.call), ("mail-unread-symbolic", self.sms)):
                b = Gtk.Button(icon_name=icon, css_classes=["flat", "circular"], valign=Gtk.Align.CENTER)
                b.connect("clicked", lambda *_, n=t["e164"], cb=cb: cb(n))
                r.append(b)
            r.append(col)
            box.append(r)
        for e in d["emails"]:
            r = Gtk.Box(spacing=8, css_classes=["detail-row"])
            b = Gtk.Button(icon_name="mail-send-symbolic", css_classes=["flat", "circular"], valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_, e=e: self.mail(e))
            r.append(b)
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            col.append(label(e, "d-value"))
            col.append(label("EMAIL", "d-kind"))
            r.append(col)
            box.append(r)
        sw = Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER)
        tv.set_content(sw)
        self.set_child(tv)

    def _action(self, icon, text, cb):
        b = Gtk.Button(css_classes=["action"])
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        v.append(Gtk.Image(icon_name=icon, pixel_size=22))
        v.append(label(text, "action-label", 0.5, False))
        b.set_child(v)
        b.connect("clicked", lambda *_: cb())
        return b

    def call(self, number):
        launch(["gio", "open", "tel:" + number])

    def sms(self, number):
        launch(["python3", os.path.expanduser("~/chat/chat.py"), "--sms", number, self.d["name"]])

    def whatsapp(self, number):
        launch(["python3", os.path.expanduser("~/chat/chat.py"), "--whatsapp", number, self.d["name"]])

    def mail(self, address):
        launch(["gio", "open", "mailto:" + address])

    def confirm_delete(self):
        dlg = Adw.AlertDialog(heading=f"Delete {self.d['name']}?",
                              body="Removed from Google Contacts too.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", lambda _d, r: r == "delete" and self.win.delete_contact(self.d))
        dlg.present(self.win)


# --- new / edit: full-screen steps --------------------------------------------------------------

class StepPage(Adw.NavigationPage):
    """One big question per screen. Buttons sit bottom-LEFT (left-handed)."""

    def __init__(self, title, question, fields, buttons):
        super().__init__(title=title)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        hb.set_title_widget(label(title, "sheet-title", 0.5, False))
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin_start=22, margin_end=22,
                      margin_top=24, margin_bottom=26)
        box.append(label(question, "question", 0.0, False))
        self.entries = []
        for placeholder, purpose, value in fields:
            e = Gtk.Entry(placeholder_text=placeholder, input_purpose=purpose, css_classes=["big-entry"])
            e.set_text(value or "")
            box.append(e)
            self.entries.append(e)
        box.append(Gtk.Box(vexpand=True))
        row = Gtk.Box(spacing=12, halign=Gtk.Align.START)
        self.buttons = []
        for text, css, cb in buttons:
            b = Gtk.Button(label=text, css_classes=["step-btn"] + css.split())
            b.connect("clicked", lambda *_, cb=cb: cb(self))
            row.append(b)
            self.buttons.append(b)
        box.append(row)
        # Enter in the last field = the first button
        if self.entries:
            for i, e in enumerate(self.entries):
                nxt = self.entries[i + 1] if i + 1 < len(self.entries) else None
                e.connect("activate", lambda _e, n=nxt: n.grab_focus() if n else self.buttons[0].emit("clicked"))
        tv.set_content(box)
        self.set_child(tv)
        self.connect("shown", lambda *_: self.entries and self.entries[0].grab_focus())

    def values(self):
        return [e.get_text().strip() for e in self.entries]


class Wizard:
    """NAME -> NUMBER (-> EMAIL via + EMAIL). FINISH saves. Editing pre-fills every step.
    One name field (10-18): split on the first space into given / family for the vCard."""

    def __init__(self, win, d=None):
        self.win, self.d = win, d
        self.given = d["given"] if d else ""
        self.family = d["family"] if d else ""
        if d and not (self.given or self.family):
            self.given = d["name"]
        self.phones = [t["value"] for t in d["tels"]] if d else []
        self.emails = list(d["emails"]) if d else []
        title = "EDIT CONTACT" if d else "NEW CONTACT"
        self.title = title
        win.nav.push(StepPage(title, "Name?",
                              [("Name", Gtk.InputPurpose.NAME, " ".join(filter(None, (self.given, self.family))))],
                              [("NEXT", "suggested", self.name_done)]))

    def name_done(self, page):
        name = " ".join(page.values()[0].split())
        if not name:
            page.entries[0].add_css_class("error")
            return
        self.given, _, self.family = name.partition(" ")
        self.win.nav.push(StepPage(self.title, "Number?",
                                   [("+46 70 123 45 67", Gtk.InputPurpose.PHONE,
                                     self.phones[0] if self.phones else "")],
                                   [("FINISH", "suggested", self.phone_finish), ("＋ EMAIL", "", self.phone_next)]))

    def _phone(self, page):
        num = page.values()[0]
        self.phones = ([num] if num else []) + self.phones[1:]

    def phone_finish(self, page):
        self._phone(page)
        self.save()

    def phone_next(self, page):
        self._phone(page)
        self.win.nav.push(StepPage(self.title, "Email?",
                                   [("name@example.com", Gtk.InputPurpose.EMAIL,
                                     self.emails[0] if self.emails else "")],
                                   [("FINISH", "suggested", self.email_finish)]))

    def email_finish(self, page):
        mail = page.values()[0]
        self.emails = ([mail] if mail else []) + self.emails[1:]
        self.save()

    def save(self):
        self.win.save_contact(self.d["uid"] if self.d else None, self.given, self.family,
                              self.phones, self.emails)


# --- window ----------------------------------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Contacts", default_width=540, default_height=1100)
        self.book = book.Book()
        self.people = book.Book.cached()
        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)
        self.list_page = ListPage(self)
        self.nav.add(self.list_page)
        self.list_page.fill(self.people)
        if not self.people:
            self.list_page.status.set_label("loading Google contacts…")
            self.list_page.status.set_visible(True)
        bg(self.book.connect, lambda _n: self.refresh(), self._failed)

    def _failed(self, e):
        self.list_page.status.set_label(str(e))
        self.list_page.status.set_visible(True)

    def refresh(self, then=None):
        def done(people):
            self.people = people
            self.list_page.fill(people)
            self.list_page.status.set_visible(False)
            if then:
                then()
        bg(self.book.load, done, self._failed)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    def open_person(self, d):
        self.nav.push(PersonPage(self, d))

    def new_contact(self):
        Wizard(self)

    def edit_contact(self, d):
        Wizard(self, d)

    def save_contact(self, uid, given, family, phones, emails):
        if not self.book.client:
            self.toast("Google contacts not connected yet")
            return
        name = " ".join(x for x in (given, family) if x)

        def work():
            if uid:
                self.book.update(uid, given, family, phones, emails)
                return uid
            return self.book.add(given, family, phones, emails)

        def done(new_uid):
            def show():
                self.nav.pop_to_tag("list")
                d = next((p for p in self.people if p["uid"] == new_uid), None)
                if d:
                    self.open_person(d)
            self.toast(f"{name} saved")
            self.refresh(show)
        bg(work, done, lambda e: self.toast(f"not saved: {e}"))

    def delete_contact(self, d):
        def done(_r):
            self.toast(f"{d['name']} deleted")
            self.nav.pop_to_tag("list")
            self.refresh()
        bg(lambda: self.book.delete(d["uid"]), done, lambda e: self.toast(f"not deleted: {e}"))


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
