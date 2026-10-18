"""Address suggestions for vaporMail: Google contacts (GNOME Online Accounts -> Evolution
Data Server address books) plus people from the inbox, cached in contacts.json."""
import os

import gi
gi.require_version("EBook", "1.2")
gi.require_version("EBookContacts", "1.2")
gi.require_version("EDataServer", "1.2")
from gi.repository import EBook, EBookContacts, EDataServer, GLib  # noqa: E402

import gmail  # noqa: E402

CACHE = os.path.join(gmail.CACHE_DIR, "contacts.json")


def load_cached():
    return [tuple(c) for c in gmail._read_json(CACHE, [])]


def fetch():
    """Blocking (~5 s): every address book's contacts that have an email -> [(name, addr)]."""
    reg = EDataServer.SourceRegistry.new_sync(None)
    query = EBookContacts.BookQuery.field_exists(EBookContacts.ContactField.EMAIL).to_string()
    out = {}
    for src in reg.list_enabled(EDataServer.SOURCE_EXTENSION_ADDRESS_BOOK):
        try:
            client = EBook.BookClient.connect_sync(src, 5, None)
            ok, contacts = client.get_contacts_sync(query, None)
        except GLib.Error as e:
            print("contacts:", src.get_display_name(), e.message, flush=True)
            continue
        for c in contacts or []:
            name = c.get_property("full-name") or ""
            for a in c.get_attributes():
                if a.get_name().upper() == "EMAIL":
                    addr = (a.get_value() or "").strip()
                    if "@" in addr:
                        out.setdefault(addr.lower(), (name, addr))
    result = sorted(out.values(), key=lambda c: (c[0] or c[1]).lower())
    gmail._write_json(CACHE, result)
    return result


class Book:
    """Matching for the To/Cc fields; people you get mail from rank first."""

    def __init__(self):
        self.contacts = load_cached()
        self.seen = {}  # addr.lower() -> how often in the inbox

    def set_contacts(self, contacts):
        self.contacts = contacts

    def note_inbox(self, msgs):
        self.seen = {}
        for m in msgs:
            a = (m.get("from_addr") or "").lower()
            if a:
                self.seen[a] = self.seen.get(a, 0) + 1
        known = {c[1].lower() for c in self.contacts}
        self.extra = [(m["from_name"], m["from_addr"]) for m in msgs
                      if m.get("from_addr") and m["from_addr"].lower() not in known]

    def match(self, text, limit=6, exclude=()):
        t = text.strip().lower()
        if len(t) < 1:
            return []
        exclude = {e.lower() for e in exclude}
        hits, have = [], set()
        for name, addr in list(self.contacts) + list(getattr(self, "extra", [])):
            a = addr.lower()
            if a in have or a in exclude:
                continue
            n = (name or "").lower()
            if a.startswith(t) or n.startswith(t) or any(w.startswith(t) for w in n.split()):
                rank = 0
            elif t in a or t in n:
                rank = 1
            else:
                continue
            have.add(a)
            hits.append((rank, -self.seen.get(a, 0), n or a, name, addr))
        hits.sort()
        return [(h[3], h[4]) for h in hits[:limit]]
