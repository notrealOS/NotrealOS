"""SMS through ModemManager (system D-Bus), like Chatty did: received texts are stored
in our Store and then deleted from the modem. Runs on the GLib main loop.
"""
import datetime
import os
import re
import sqlite3
import time
import zlib

from gi.repository import Gio, GLib

MM = "org.freedesktop.ModemManager1"
MESSAGING = MM + ".Modem.Messaging"
SMS_IFACE = MM + ".Sms"
ST_RECEIVING, ST_RECEIVED = 2, 3
PDU_DELIVER = 1
CHATTY_DB = os.path.expanduser("~/.purple/chatty/db/chatty-history.db")


def chat_id(number):
    return "sms:" + number


def parse_ts(s):
    if not s:
        return int(time.time() * 1000)
    s = re.sub(r"([+-]\d\d)$", r"\1:00", s)
    try:
        return int(datetime.datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return int(time.time() * 1000)


class Sms:
    def __init__(self, store, on_change, name_for):
        self.store = store
        self.on_change = on_change    # (chat ids, [new messages])
        self.name_for = name_for      # number -> contact name or None
        self.messaging = None
        self.waiting = {}             # sms path -> proxy still RECEIVING

    def start(self):
        Gio.DBusObjectManagerClient.new_for_bus(
            Gio.BusType.SYSTEM, Gio.DBusObjectManagerClientFlags.NONE, MM, "/org/freedesktop/ModemManager1",
            None, None, None, self._manager_ready)

    def _manager_ready(self, _src, res):
        try:
            self.manager = Gio.DBusObjectManagerClient.new_for_bus_finish(res)
        except GLib.Error as e:
            print("sms: no ModemManager:", e.message, flush=True)
            return
        self.manager.connect("object-added", lambda *_: self._find_modem())
        self.manager.connect("object-removed", lambda *_: self._find_modem())
        self._find_modem()

    def _find_modem(self):
        for obj in self.manager.get_objects():
            iface = obj.get_interface(MESSAGING)
            if iface:
                if self.messaging and self.messaging.get_object_path() == iface.get_object_path():
                    return
                self.messaging = iface
                iface.connect("g-signal", self._signal)
                print("sms: modem", iface.get_object_path(), flush=True)
                iface.call("List", None, Gio.DBusCallFlags.NONE, -1, None, self._listed)
                return
        self.messaging = None

    def _listed(self, proxy, res):
        try:
            paths = proxy.call_finish(res).unpack()[0]
        except GLib.Error as e:
            print("sms: list failed:", e.message, flush=True)
            return
        for p in paths:
            self._load(p)

    def _signal(self, proxy, sender, signal, params):
        if signal == "Added":
            path, received = params.unpack()
            if received:
                self._load(path)

    def _load(self, path):
        Gio.DBusProxy.new_for_bus(Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None, MM, path, SMS_IFACE,
                                  None, self._sms_ready, path)

    def _sms_ready(self, _src, res, path):
        try:
            sms = Gio.DBusProxy.new_for_bus_finish(res)
        except GLib.Error:
            return
        self._check(sms, path)

    def _prop(self, sms, name):
        v = sms.get_cached_property(name)
        return v.unpack() if v is not None else None

    def _check(self, sms, path):
        state, pdu = self._prop(sms, "State"), self._prop(sms, "PduType")
        if state == ST_RECEIVING:  # multipart still arriving
            if path not in self.waiting:
                self.waiting[path] = sms
                sms.connect("g-properties-changed", lambda p, *_: self._check(p, path))
            return
        self.waiting.pop(path, None)
        if state == ST_RECEIVED and pdu == PDU_DELIVER:
            number = self._prop(sms, "Number") or "?"
            text = self._prop(sms, "Text") or ""
            if not text:
                data = self._prop(sms, "Data")
                text = "[binary SMS, %d bytes]" % len(data or b"")
            ts = parse_ts(self._prop(sms, "Timestamp"))
            cid = chat_id(number)
            mid = "sms-%d-%08x" % (ts, zlib.crc32((number + text).encode()))
            self._ensure_chat(number)
            self.store.add_message(cid, mid, number, ts, "text", text)
            ch = self.store.chat(cid)
            self.store.upsert_chat(cid, unread=ch["unread"] + 1, archived=0)
            self.store.refresh_last(cid)
            self.on_change({cid}, [{"chat": cid, "id": mid, "sender": number, "ts": ts, "kind": "text",
                                    "body": text, "mine": False}])
        if state in (ST_RECEIVED, 5) or pdu == 3:  # stored here / sent / status report: off the modem
            self._delete(path)

    def _delete(self, path):
        if self.messaging:
            self.messaging.call("Delete", GLib.Variant("(o)", (path,)), Gio.DBusCallFlags.NONE, -1, None,
                                lambda p, r: self._ignore(p, r))

    @staticmethod
    def _ignore(proxy, res):
        try:
            proxy.call_finish(res)
        except GLib.Error as e:
            print("sms:", e.message, flush=True)

    def _ensure_chat(self, number):
        cid = chat_id(number)
        if not self.store.chat(cid):
            self.store.upsert_chat(cid, net="sms", name=self.name_for(number) or number, dm=1)
        return cid

    def open_chat(self, number, name=None):
        cid = self._ensure_chat(number)
        if name:
            self.store.upsert_chat(cid, name=name)
        return cid

    # --- sending ----------------------------------------------------------------------
    def send(self, cid, text):
        number = cid[4:]
        mid = "sms-out-%d" % int(time.time() * 1000)
        self.store.add_message(cid, mid, "me", int(time.time() * 1000), "text", text, mine=True,
                               status="sending")
        self.store.refresh_last(cid)
        self.on_change({cid}, [])
        if not self.messaging:
            return self._sent(cid, mid, None, "No modem")
        props = GLib.Variant("(a{sv})", ({"number": GLib.Variant("s", number),
                                          "text": GLib.Variant("s", text)},))
        self.messaging.call("Create", props, Gio.DBusCallFlags.NONE, -1, None, self._created, (cid, mid))

    def _created(self, proxy, res, ctx):
        cid, mid = ctx
        try:
            path = proxy.call_finish(res).unpack()[0]
        except GLib.Error as e:
            return self._sent(cid, mid, None, e.message)
        Gio.DBusConnection.call(proxy.get_connection(), MM, path, SMS_IFACE, "Send", None, None,
                                Gio.DBusCallFlags.NONE, 120000, None,
                                lambda c, r: self._send_done(c, r, cid, mid, path))

    def _send_done(self, conn, res, cid, mid, path):
        try:
            conn.call_finish(res)
            self._sent(cid, mid, path, None)
        except GLib.Error as e:
            self._sent(cid, mid, path, e.message)

    def _sent(self, cid, mid, path, error):
        if error:
            print("sms: send failed:", error, flush=True)
        self.store.update_message(cid, mid, status="failed" if error else "sent")
        if path:
            self._delete(path)
        self.on_change({cid}, [])


def import_chatty(store, name_for):
    """One-time copy of Chatty's SMS threads into our store."""
    if store.get("chatty_imported") or not os.path.exists(CHATTY_DB):
        return
    try:
        db = sqlite3.connect("file:%s?mode=ro" % CHATTY_DB, uri=True)
        rows = db.execute(
            "SELECT t.name, m.uid, m.body, m.direction, m.time, m.status FROM messages m "
            "JOIN threads t ON t.id=m.thread_id JOIN accounts a ON a.id=t.account_id "
            "JOIN users u ON u.id=a.user_id WHERE u.username LIKE 'invalid-%' AND t.name LIKE '+%' "
            "ORDER BY m.time").fetchall()
    except sqlite3.Error as e:
        print("chatty import:", e, flush=True)
        return
    for number, uid, body, direction, ts, status in rows:
        if not body:
            continue
        cid = chat_id(number)
        if not store.chat(cid):
            store.upsert_chat(cid, net="sms", name=name_for(number) or number, dm=1)
        mine = direction == -1
        store.add_message(cid, "chatty-%s-%d" % (uid, ts), "me" if mine else number, ts * 1000, "text", body,
                          mine=mine, status="sent")
        store.refresh_last(cid)
    store.put("chatty_imported", True)
    print("chatty import: %d messages" % len(rows), flush=True)
