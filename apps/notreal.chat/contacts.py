"""Google contacts (GOA -> Evolution address book) with phone numbers, cached.

Same source as New Chat: [[name, [[+e164, shown], ...]], ...]. Also number -> name.
"""
import json
import os
import re

import gi
gi.require_version("EBook", "1.2")
gi.require_version("EBookContacts", "1.2")
gi.require_version("EDataServer", "1.2")
from gi.repository import EBook, EBookContacts, EDataServer, GLib  # noqa: E402

CACHE = os.path.expanduser("~/.cache/notarealchat/contacts.json")
DEFAULT_CC = "+46"


def e164(attr):
    """A vCard TEL -> +<digits>. Evolution stores the parsed form in X-EVOLUTION-E164."""
    for p in attr.get_params():
        if p.get_name().upper() == "X-EVOLUTION-E164":
            vals = p.get_values()
            if len(vals) >= 2 and vals[1].startswith("+"):
                return vals[1] + vals[0]
    return normalize(attr.get_value() or "")


def normalize(raw):
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits and not digits.startswith("+"):
        digits = DEFAULT_CC + digits.lstrip("0")
    return digits


def cached():
    try:
        with open(CACHE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def fetch():
    reg = EDataServer.SourceRegistry.new_sync(None)
    query = EBookContacts.BookQuery.field_exists(EBookContacts.ContactField.TEL).to_string()
    people = {}
    for src in reg.list_enabled(EDataServer.SOURCE_EXTENSION_ADDRESS_BOOK):
        try:
            client = EBook.BookClient.connect_sync(src, 5, None)
            ok, contacts = client.get_contacts_sync(query, None)
        except GLib.Error:
            continue
        for c in contacts or []:
            name = (c.get_property("full-name") or "").strip()
            org = (c.get_property("org") or "").strip()
            no_person = not ((c.get_property("given-name") or "").strip() or (c.get_property("family-name") or "").strip())
            if org and no_person and not any(ch.isalpha() for ch in name):
                name = org        # 10-17: business-only contacts were named after their number
            if not name:
                continue
            nums = people.setdefault(name, [])
            for a in c.get_attributes():
                if a.get_name().upper() == "TEL":
                    n = e164(a)
                    kinds = [v for p in a.get_params() if p.get_name().upper() == "TYPE"
                             for v in p.get_values()]
                    if len(n) > 6 and n not in [x[0] for x in nums] and "FAX" not in kinds:
                        nums.append([n, (a.get_value() or n).strip()])
    out = sorted(([k, v] for k, v in people.items() if v), key=lambda p: p[0].lower())
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE + ".tmp", "w") as f:
        json.dump(out, f)
    os.replace(CACHE + ".tmp", CACHE)
    return out


class Book:
    def __init__(self):
        self.people = cached()
        self._index()

    def _index(self):
        self.by_tail = {}
        for name, nums in self.people:
            for n, _shown in nums:
                self.by_tail[re.sub(r"\D", "", n)[-8:]] = name

    def set(self, people):
        self.people = people
        self._index()

    def name_for(self, number):
        d = re.sub(r"\D", "", number or "")
        return self.by_tail.get(d[-8:]) if len(d) >= 8 else None

    def search(self, text, limit=40):
        t = text.strip().lower()
        if not t:
            return []
        hits = [p for p in self.people
                if p[0].lower().startswith(t) or any(w.startswith(t) for w in p[0].lower().split())]
        hits += [p for p in self.people if t in p[0].lower() and p not in hits]
        digits = re.sub(r"\D", "", t)
        if len(digits) >= 3:
            hits += [p for p in self.people if p not in hits
                     and any(digits in re.sub(r"\D", "", n) for n, _ in p[1])]
        return hits[:limit]
