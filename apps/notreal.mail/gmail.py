"""Gmail over IMAP/SMTP for vaporMail: config, keyring, cache, listing, trash, snooze, send.

Everything here blocks; the app and the daemon call it from worker threads.
Snooze is our own (Gmail's native snooze isn't reachable over IMAP): the message gets the
SNOOZE_LABEL and loses \\Inbox; snoozed.json remembers when it comes back.
"""
import contextlib
import email
import email.policy
import email.utils
import fcntl
import html
import imaplib
import json
import mimetypes
import os
import re
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage

import gi
gi.require_version("Secret", "1")
from gi.repository import Secret  # noqa: E402

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
SNOOZE_LABEL = "vaporMail/Snoozed"

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".config", "vapormail")
DATA_DIR = os.path.join(HOME, ".local", "share", "vapormail")
CACHE_DIR = os.path.join(HOME, ".cache", "vapormail")
MSG_DIR = os.path.join(CACHE_DIR, "msg")
CONFIG = os.path.join(CONFIG_DIR, "config.json")
SNOOZED = os.path.join(DATA_DIR, "snoozed.json")
STATE = os.path.join(DATA_DIR, "state.json")      # daemon: last notified INBOX uid
INBOX_CACHE = os.path.join(CACHE_DIR, "inbox.json")
SENT_CACHE = os.path.join(CACHE_DIR, "sent.json")
LOCK = os.path.join(DATA_DIR, ".lock")

DEFAULT_SIGNATURE = ""
LIST_LIMIT = 60
PREFETCH_MAX = 400_000  # bodies above this size are fetched only when opened

HEADER_FIELDS = "FROM TO CC REPLY-TO SUBJECT DATE MESSAGE-ID REFERENCES IN-REPLY-TO"

SCHEMA = Secret.Schema.new(
    "dev.notareal.VaporMail", Secret.SchemaFlags.DONT_MATCH_NAME,
    {"service": Secret.SchemaAttributeType.STRING, "account": Secret.SchemaAttributeType.STRING})


class MailError(Exception):
    pass


# --- files ---------------------------------------------------------------

def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


@contextlib.contextmanager
def locked():
    """The app and the daemon both edit snoozed.json/state.json."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load_config():
    cfg = _read_json(CONFIG, {})
    cfg.setdefault("signature", DEFAULT_SIGNATURE)
    cfg.setdefault("name", DEFAULT_SIGNATURE.split("\n")[0])
    return cfg


def save_config(cfg):
    _write_json(CONFIG, cfg)


def get_password(address):
    return Secret.password_lookup_sync(SCHEMA, {"service": "vapormail", "account": address}, None)


def store_password(address, password):
    Secret.password_store_sync(SCHEMA, {"service": "vapormail", "account": address},
                               Secret.COLLECTION_DEFAULT, "vaporMail (%s)" % address, password, None)


def load_snoozed():
    return _read_json(SNOOZED, [])


def save_snoozed(items):
    _write_json(SNOOZED, items)


def load_state():
    return _read_json(STATE, {})


def save_state(state):
    _write_json(STATE, state)


def mark_listed(uidvalidity, max_uid):
    """The app saw the inbox up to max_uid: the daemon must not notify those again."""
    with locked():
        st = load_state()
        if st.get("uidvalidity") != uidvalidity or st.get("last_uid", 0) < max_uid:
            st.update(uidvalidity=uidvalidity, last_uid=max_uid)
            save_state(st)


def _cache_name(msgid):
    msgid = str(msgid)
    if re.fullmatch(r"[\w-]{1,80}", msgid):
        return msgid
    import hashlib
    return "h" + hashlib.sha1(msgid.encode()).hexdigest()


def mark_listed_other(key, newest):
    """Same as mark_listed for a non-Gmail account: newest received time already shown."""
    with locked():
        st = load_state()
        other = st.setdefault("other", {})
        if other.get(key, 0) < newest:
            other[key] = newest
            save_state(st)


def cached_raw(msgid):
    try:
        with open(os.path.join(MSG_DIR, "%s.eml" % _cache_name(msgid)), "rb") as f:
            return f.read()
    except OSError:
        return None


def _cache_raw(msgid, raw):
    os.makedirs(MSG_DIR, exist_ok=True)
    name = _cache_name(msgid)
    tmp = os.path.join(MSG_DIR, "%s.tmp" % name)
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, os.path.join(MSG_DIR, "%s.eml" % name))


def prune_cache(keep):
    """Drop cached bodies that are neither listed nor snoozed."""
    keep = {_cache_name(k) for k in keep}
    try:
        names = os.listdir(MSG_DIR)
    except OSError:
        return
    for n in names:
        if n.endswith(".eml") and n[:-4] not in keep:
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(MSG_DIR, n))


# --- parsing -------------------------------------------------------------

def _quote(folder):
    return '"%s"' % folder.replace("\\", "\\\\").replace('"', '\\"')


def _meta_int(meta, key):
    m = re.search(rb"\b" + key + rb" (\d+)", meta)
    return int(m.group(1)) if m else None


def _fetch_items(data):
    """imaplib FETCH data -> [(meta bytes, literal bytes)]; meta includes text after the literal."""
    out = []
    i = 0
    while i < len(data):
        part = data[i]
        if isinstance(part, tuple):
            meta, lit = part[0], part[1]
            if i + 1 < len(data) and isinstance(data[i + 1], bytes):
                meta += data[i + 1]
                i += 1
            out.append((meta, lit))
        i += 1
    return out


def _addr(value):
    name, addr = email.utils.parseaddr(str(value or ""))
    return name or addr.split("@")[0], addr


def _header_msg(meta, header_bytes, folder):
    h = email.message_from_bytes(header_bytes, policy=email.policy.default)
    name, addr = _addr(h.get("from"))
    flags = re.search(rb"FLAGS \((.*?)\)", meta)
    flags = flags.group(1).decode() if flags else ""
    tup = imaplib.Internaldate2tuple(meta)
    return {
        "acct": "gmail",
        "msgid": str(_meta_int(meta, b"X-GM-MSGID")),
        "thrid": str(_meta_int(meta, b"X-GM-THRID")),
        "uid": _meta_int(meta, b"UID"),
        "folder": folder,
        "size": _meta_int(meta, b"RFC822.SIZE") or 0,
        "seen": "\\Seen" in flags,
        "date": time.mktime(tup) if tup else time.time(),
        "from_name": name,
        "from_addr": addr,
        "to": str(h.get("to") or ""),
        "cc": str(h.get("cc") or ""),
        "reply_to": str(h.get("reply-to") or ""),
        "subject": str(h.get("subject") or "(no subject)").strip() or "(no subject)",
        "message_id": str(h.get("message-id") or "").strip(),
        "references": str(h.get("references") or "").strip(),
        "snippet": "",
    }


def _part_text(part):
    try:
        return part.get_content()
    except (LookupError, KeyError, ValueError, AssertionError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", "replace")


def html_to_text(s):
    s = re.sub(r"(?is)<(style|script|head)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s)


def parse(raw):
    """raw RFC822 -> dict(plain, html, attachments=[(name, ctype, bytes)])."""
    m = email.message_from_bytes(raw, policy=email.policy.default)
    plain = m.get_body(("plain",))
    htm = m.get_body(("html",))
    out = {"plain": _part_text(plain) if plain else None,
           "html": _part_text(htm) if htm else None,
           "attachments": []}
    if out["html"]:
        # inline images (cid:) -> data: URIs so WebKit can show them without a network
        import base64
        cids = {}
        for part in m.walk():
            cid = part.get("content-id")
            if cid and part.get_content_maintype() == "image":
                data = part.get_payload(decode=True) or b""
                cids[cid.strip("<> ")] = "data:%s;base64,%s" % (
                    part.get_content_type(), base64.b64encode(data).decode())
        if cids:
            out["html"] = re.sub(r"cid:([^\"'\s>)]+)",
                                 lambda mm: cids.get(mm.group(1), mm.group(0)), out["html"])
    for part in m.iter_attachments():
        name = part.get_filename()
        if not name:
            if part.get("content-id"):
                continue  # inline image, already embedded
            name = "attachment" + (mimetypes.guess_extension(part.get_content_type()) or "")
        out["attachments"].append((name, part.get_content_type(), part.get_payload(decode=True) or b""))
    return out


def snippet(raw, n=160):
    try:
        p = parse(raw)
    except Exception:  # noqa: BLE001 - a broken mail must not break the list
        return ""
    text = p["plain"] or html_to_text(p["html"] or "")
    if re.match(r"\s*<(!doctype|html|head|body|div|table|p\b)", text, re.I):
        text = html_to_text(text)  # some senders put HTML in the text/plain part
    return clean_snippet(text, n)


def clean_snippet(text, n=160):
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))
    # newsletters pad their preheader with &zwnj;/&nbsp; and invisible characters
    text = html.unescape(html.unescape(text))
    text = re.sub("[\u034f\u00ad\u200b-\u200f\u2060\ufeff]", "", text)
    return re.sub(r"\s+", " ", text).strip()[:n]


# --- Gmail ---------------------------------------------------------------

class Gmail:
    kind = "gmail"
    key = "gmail"

    def __init__(self, address, password, name=None):
        self.address, self.password = address, password
        self.name = name
        self._c = None
        self._special = {}
        self.lock = threading.RLock()

    @classmethod
    def from_config(cls):
        cfg = load_config()
        addr = cfg.get("address")
        if not addr:
            return None
        pw = get_password(addr)
        if not pw:
            return None
        return cls(addr, pw, cfg.get("name"))

    # connection ---------------------------------------------------------
    def _conn(self):
        if self._c is not None:
            try:
                self._c.noop()
                return self._c
            except (imaplib.IMAP4.error, OSError):
                self.close()
        try:
            c = imaplib.IMAP4_SSL(IMAP_HOST, timeout=30)
            c.login(self.address, self.password)
        except imaplib.IMAP4.error as e:
            raise MailError("Login failed: %s" % e) from e
        except OSError as e:
            raise MailError("Offline: %s" % e) from e
        self._c = c
        return c

    def close(self):
        c, self._c = self._c, None
        if c is not None:
            with contextlib.suppress(Exception):
                c.logout()

    def check_login(self):
        with self.lock:
            self._conn()

    def _select(self, c, folder, readonly=False):
        typ, data = c.select(_quote(folder), readonly=readonly)
        if typ != "OK":
            raise MailError("Can't open %s" % folder)
        typ, data = c.response("UIDVALIDITY")
        return int(data[0]) if data and data[0] else 0

    def special(self, flag):
        """The folder with a SPECIAL-USE flag (\\Trash, \\All); names are localised."""
        if flag in self._special:
            return self._special[flag]
        c = self._conn()
        typ, data = c.list()
        for line in data or []:
            if not isinstance(line, bytes):
                continue
            m = re.match(rb'\((.*?)\) "(.*?)" (.*)', line)
            if not m:
                continue
            flags, _, name = m.groups()
            name = name.strip().decode()
            if name.startswith('"'):
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            for f in flags.decode().split():
                self._special.setdefault(f, name)
        if flag not in self._special:
            raise MailError("No %s folder" % flag)
        return self._special[flag]

    def _run(self, fn):
        """fn(conn) with one reconnect if the connection died under us."""
        with self.lock:
            try:
                return fn(self._conn())
            except (imaplib.IMAP4.abort, OSError):
                self.close()
                return fn(self._conn())

    # reading ------------------------------------------------------------
    def list_folder(self, folder="INBOX", limit=LIST_LIMIT, since_uid=0):
        """Newest `limit` messages, newest first. Returns (uidvalidity, [msg])."""
        folder = {"sent": "\\Sent", "spam": "\\Junk", "trash": "\\Trash"}.get(folder, folder)
        if folder.startswith("\\"):
            folder = self.special(folder)

        def go(c):
            validity = self._select(c, folder, readonly=True)
            crit = "UID %d:*" % (since_uid + 1) if since_uid else "ALL"
            typ, data = c.uid("SEARCH", None, crit)
            uids = [int(u) for u in (data[0] or b"").split() if int(u) > since_uid][-limit:]
            if not uids:
                return validity, []
            typ, data = c.uid("FETCH", ",".join(map(str, uids)),
                              "(UID X-GM-MSGID X-GM-THRID FLAGS INTERNALDATE RFC822.SIZE "
                              "BODY.PEEK[HEADER.FIELDS (%s)])" % HEADER_FIELDS)
            msgs = [_header_msg(meta, lit, folder) for meta, lit in _fetch_items(data)]
            msgs.sort(key=lambda m: m["uid"], reverse=True)
            return validity, msgs
        return self._run(go)

    def list_sent(self, limit=LIST_LIMIT):
        return self._run(lambda c: self.list_folder("sent", limit))

    def prefetch(self, msgs):
        """Cache bodies (<= PREFETCH_MAX) and fill in snippets. Returns the msgs touched."""
        need = [m for m in msgs if cached_raw(m["msgid"]) is None and m["size"] <= PREFETCH_MAX]
        by_folder = {}
        for m in need:
            by_folder.setdefault(m["folder"], []).append(m)
        for folder, items in by_folder.items():
            for i in range(0, len(items), 10):
                batch = {m["uid"]: m for m in items[i:i + 10]}

                def go(c, batch=batch, folder=folder):
                    self._select(c, folder, readonly=True)
                    typ, data = c.uid("FETCH", ",".join(map(str, batch)), "(UID BODY.PEEK[])")
                    return _fetch_items(data)
                for meta, lit in self._run(go):
                    m = batch.get(_meta_int(meta, b"UID"))
                    if m:
                        _cache_raw(m["msgid"], lit)
        for m in msgs:
            if not m.get("snippet"):
                raw = cached_raw(m["msgid"])
                if raw:
                    m["snippet"] = snippet(raw)
        return msgs

    def _uid_in(self, c, folder, msgid, readonly=False):
        self._select(c, folder, readonly=readonly)
        typ, data = c.uid("SEARCH", None, "X-GM-MSGID", str(msgid))
        uids = (data[0] or b"").split()
        return uids[0].decode() if uids else None

    def get_raw(self, msg):
        raw = cached_raw(msg["msgid"])
        if raw:
            return raw

        def go(c):
            uid = self._uid_in(c, self.special("\\All"), msg["msgid"], readonly=True)
            if not uid and msg.get("folder") and msg["folder"] != self.special("\\All"):
                uid = self._uid_in(c, msg["folder"], msg["msgid"], readonly=True)  # Spam / Trash
            if not uid:
                raise MailError("That message is gone")
            typ, data = c.uid("FETCH", uid, "(BODY.PEEK[])")
            items = _fetch_items(data)
            if not items:
                raise MailError("That message is gone")
            return items[0][1]
        raw = self._run(go)
        _cache_raw(msg["msgid"], raw)
        return raw

    def mark_seen(self, msgid, seen=True):
        def go(c):
            uid = self._uid_in(c, self.special("\\All"), msgid)
            if uid:
                c.uid("STORE", uid, "+FLAGS" if seen else "-FLAGS", "(\\Seen)")
        self._run(go)

    # changing -----------------------------------------------------------
    def trash(self, msgid):
        def go(c):
            uid = self._uid_in(c, self.special("\\All"), msgid)
            if not uid:
                return
            typ, data = c.uid("MOVE", uid, _quote(self.special("\\Trash")))
            if typ != "OK":
                raise MailError("Trash failed: %s" % data)
        self._run(go)

    def to_inbox(self, msgid, folder):
        """Not spam / restore from Trash: those folders aren't part of All Mail."""
        def go(c):
            uid = self._uid_in(c, folder, msgid)
            if not uid:
                raise MailError("That message is gone")
            typ, data = c.uid("MOVE", uid, "INBOX")
            if typ != "OK":
                raise MailError("Move failed: %s" % data)
        self._run(go)

    def _uids_in_all(self, c, msgids):
        self._select(c, self.special("\\All"))
        uids = []
        for mid in msgids:
            typ, data = c.uid("SEARCH", None, "X-GM-MSGID", str(mid))
            uids += [u.decode() for u in (data[0] or b"").split()[:1]]
        return ",".join(uids)

    def trash_many(self, msgids):
        def go(c):
            uids = self._uids_in_all(c, msgids)
            if uids:
                typ, data = c.uid("MOVE", uids, _quote(self.special("\\Trash")))
                if typ != "OK":
                    raise MailError("Trash failed: %s" % data)
        self._run(go)

    def snooze_many(self, msgids):
        def go(c):
            with contextlib.suppress(imaplib.IMAP4.error):
                c.create(_quote(SNOOZE_LABEL))
            uids = self._uids_in_all(c, msgids)
            if uids:
                c.uid("STORE", uids, "+X-GM-LABELS", "(%s)" % _quote(SNOOZE_LABEL))
                c.uid("STORE", uids, "-X-GM-LABELS", "(\\Inbox)")
        self._run(go)

    def snooze(self, msgid):
        """Out of the inbox, into SNOOZE_LABEL (snoozed.json is the caller's job)."""
        def go(c):
            with contextlib.suppress(imaplib.IMAP4.error):
                c.create(_quote(SNOOZE_LABEL))  # "already exists" is fine
            uid = self._uid_in(c, self.special("\\All"), msgid)
            if not uid:
                raise MailError("That message is gone")
            c.uid("STORE", uid, "+X-GM-LABELS", "(%s)" % _quote(SNOOZE_LABEL))
            c.uid("STORE", uid, "-X-GM-LABELS", "(\\Inbox)")
        self._run(go)

    def unsnooze(self, msgid):
        """Back to the top of the inbox, unread, label removed."""
        def go(c):
            uid = self._uid_in(c, self.special("\\All"), msgid)
            if not uid:
                return False
            c.uid("STORE", uid, "+X-GM-LABELS", "(\\Inbox)")
            c.uid("STORE", uid, "-X-GM-LABELS", "(%s)" % _quote(SNOOZE_LABEL))
            c.uid("STORE", uid, "-FLAGS", "(\\Seen)")
            return True
        return self._run(go)

    # sending ------------------------------------------------------------
    def send(self, to, cc, subject, body, in_reply_to=None, references=None, attachments=()):
        msg = build_message(self.name, self.address, to, cc, subject, body,
                            in_reply_to, references, attachments)
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, 465, context=ssl.create_default_context(),
                                  timeout=60) as s:
                s.login(self.address, self.password)
                s.send_message(msg)  # Gmail files it under Sent by itself
        except smtplib.SMTPException as e:
            raise MailError("Send failed: %s" % e) from e
        except OSError as e:
            raise MailError("Offline: %s" % e) from e


def build_message(name, address, to, cc, subject, body, in_reply_to=None, references=None,
                  attachments=()):
    msg = EmailMessage()
    msg["From"] = email.utils.formataddr((name or "", address))
    msg["To"] = to
    if cc.strip():
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=address.split("@")[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = ("%s %s" % (references or "", in_reply_to)).strip()
    msg.set_content(body)
    for path in attachments:
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        main, sub = ctype.split("/", 1)
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=main, subtype=sub,
                               filename=os.path.basename(path))
    return msg


# --- snooze bookkeeping (shared by app and daemon) ----------------------

def add_snoozed(msg, due):
    with locked():
        items = [s for s in load_snoozed() if s["msgid"] != msg["msgid"]]
        entry = {k: msg.get(k) for k in ("acct", "msgid", "thrid", "from_name", "from_addr", "to", "cc",
                                          "reply_to", "subject", "message_id", "references",
                                          "snippet", "date", "size")}
        entry["due"] = due
        items.append(entry)
        save_snoozed(items)


def remove_snoozed(msgid):
    with locked():
        items = load_snoozed()
        keep = [s for s in items if s["msgid"] != msgid]
        if len(keep) != len(items):
            save_snoozed(keep)
        return len(keep) != len(items)
