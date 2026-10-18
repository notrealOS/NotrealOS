"""Google Contacts through Evolution Data Server (GOA Google account -> CardDAV address book).

Evolution keeps the CardDAV book in sync with Google both ways, so adding/editing/deleting here
reaches contacts.google.com (and the other phone) on the next sync.
The Microsoft 365 books ("Link to Windows" copies) are left out on purpose: thousands of duplicates.
"""
import json
import os
import re
import urllib.parse

import gi
gi.require_version("EBook", "1.2")
gi.require_version("EBookContacts", "1.2")
gi.require_version("EDataServer", "1.2")
from gi.repository import EBook, EBookContacts, EDataServer, GLib  # noqa: E402

F = EBookContacts.ContactField
CACHE_DIR = os.path.expanduser("~/.cache/nrcontacts")
CACHE = os.path.join(CACHE_DIR, "contacts.json")
DEFAULT_CC = "+46"


def normalize(raw):
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits and not digits.startswith("+"):
        digits = DEFAULT_CC + digits.lstrip("0")
    return digits


def _param(attr, name):
    for p in attr.get_params():
        if p.get_name().upper() == name:
            return p.get_values()
    return []


def _e164(attr):
    vals = _param(attr, "X-EVOLUTION-E164")
    if len(vals) >= 2 and vals[1].startswith("+"):
        return vals[1] + vals[0]
    return normalize(attr.get_value())


def _kind(attr):
    t = [v.upper() for v in _param(attr, "TYPE")]
    for k, label in (("CELL", "mobile"), ("HOME", "home"), ("WORK", "work"), ("MAIN", "main")):
        if k in t:
            return label
    return ""


def to_dict(c):
    tels, emails, photo = [], [], None
    for a in c.get_attributes():
        n = a.get_name().upper()
        if n == "TEL" and a.get_value():
            tels.append({"value": a.get_value(), "e164": _e164(a), "kind": _kind(a)})
        elif n == "EMAIL" and a.get_value():
            emails.append(a.get_value())
        elif n == "PHOTO" and photo is None:
            v = a.get_value() or ""
            if v.startswith("file://"):
                photo = urllib.parse.unquote(v[7:])
    # c.get() hands back raw pointers in PyGObject: read the GObject properties instead
    name = (c.get_property("full-name") or "").strip()
    given, family = (c.get_property("given-name") or "").strip(), (c.get_property("family-name") or "").strip()
    org = (c.get_property("org") or "").strip()
    if not name or (not (given or family) and org and not any(ch.isalpha() for ch in name)):
        # 10-17: business-only contacts came from Google with the NUMBER as their name: use the
        # business name when there is no first/last name
        name = " ".join(x for x in (given, family) if x) or org or (tels[0]["value"] if tels else "") or \
            (emails[0] if emails else "")
    return {"uid": c.get_property("id"), "name": name, "given": given, "family": family,
            "org": org, "tels": tels, "emails": emails, "photo": photo}


class Book:
    def __init__(self):
        self.client = None
        self.registry = None

    def connect(self):
        """Blocking (~1-2 s): run off the main thread."""
        self.registry = EDataServer.SourceRegistry.new_sync(None)
        best = None
        for src in self.registry.list_enabled(EDataServer.SOURCE_EXTENSION_ADDRESS_BOOK):
            ext = src.get_extension(EDataServer.SOURCE_EXTENSION_ADDRESS_BOOK)
            parent = self.registry.ref_source(src.get_parent()) if src.get_parent() else None
            if ext.get_backend_name() == "carddav" and parent and \
                    parent.has_extension(EDataServer.SOURCE_EXTENSION_GOA):
                best = src
                break
        if best is None:
            raise RuntimeError("no Google address book (GNOME Online Accounts > Google > Contacts)")
        # 1 s: 0 means "wait until online" and hung for good on 10-16
        self.client = EBook.BookClient.connect_sync(best, 1, None)
        return best.get_display_name()

    def load(self):
        ok, cs = self.client.get_contacts_sync("", None)
        people = [to_dict(c) for c in cs or []]
        people = [p for p in people if p["name"]]
        people.sort(key=lambda p: p["name"].lower())
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(CACHE + ".tmp", "w") as f:
            json.dump(people, f)
        os.replace(CACHE + ".tmp", CACHE)
        return people

    @staticmethod
    def cached():
        try:
            with open(CACHE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    # --- writing ---------------------------------------------------------------------------
    @staticmethod
    def _fill(c, given, family, phones, emails):
        given, family = given.strip(), family.strip()
        c.set_property("given-name", given or None)
        c.set_property("family-name", family or None)
        c.set_property("full-name", " ".join(x for x in (given, family) if x) or None)
        c.remove_attributes(None, "TEL")
        c.remove_attributes(None, "EMAIL")
        for num in phones:
            num = num.strip()
            if num:
                a = EBookContacts.VCardAttribute.new(None, "TEL")
                p = EBookContacts.VCardAttributeParam.new("TYPE")
                p.add_value("CELL")
                a.add_param(p)
                c.append_attribute_with_value(a, num)
        for mail in emails:
            mail = mail.strip()
            if mail:
                a = EBookContacts.VCardAttribute.new(None, "EMAIL")
                p = EBookContacts.VCardAttributeParam.new("TYPE")
                p.add_value("INTERNET")
                a.add_param(p)
                c.append_attribute_with_value(a, mail)

    def add(self, given, family, phones=(), emails=()):
        c = EBookContacts.Contact.new()
        self._fill(c, given, family, phones, emails)
        ok, uid = self.client.add_contact_sync(c, EBookContacts.BookOperationFlags.NONE, None)
        return uid

    def update(self, uid, given, family, phones=(), emails=()):
        ok, c = self.client.get_contact_sync(uid, None)
        self._fill(c, given, family, phones, emails)
        self.client.modify_contact_sync(c, EBookContacts.BookOperationFlags.NONE, None)

    def delete(self, uid):
        self.client.remove_contact_by_uid_sync(uid, EBookContacts.BookOperationFlags.NONE, None)
