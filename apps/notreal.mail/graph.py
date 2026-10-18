"""Microsoft 365 / Outlook.com mail over Microsoft Graph for vaporMail.

Sign-in comes from GNOME Online Accounts (provider "ms_graph"): GOA keeps the OAuth tokens,
we just ask it for a fresh access token per request. Same method names as gmail.Gmail so the
app and the daemon can treat every account alike.
Message ids are Graph *immutable* ids, so they survive moves (trash, snooze, undo).
Snooze = our own folder SNOOZE_FOLDER under the mailbox root.
"""
import email.utils
import threading
import time

import requests

import gi
gi.require_version("Goa", "1.0")
from gi.repository import Goa  # noqa: E402

import gmail  # noqa: E402
from gmail import MailError  # noqa: E402

API = "https://graph.microsoft.com/v1.0/me"
SNOOZE_FOLDER = "vaporMail Snoozed"
SIZE_PROP = "Integer 0x0E08"          # PR_MESSAGE_SIZE
SELECT = ("id,subject,from,toRecipients,ccRecipients,replyTo,receivedDateTime,isRead,"
          "bodyPreview,internetMessageId,conversationId")
# names the app uses (Gmail-ish) -> Graph well-known folder names
FOLDERS = {"INBOX": "inbox", "sent": "sentitems", "spam": "junkemail", "trash": "deleteditems"}

_client = None
_client_lock = threading.Lock()


def goa_client():
    global _client
    with _client_lock:
        if _client is None:
            _client = Goa.Client.new_sync(None)
        return _client


def goa_accounts():
    """[(identity, display name)] of the ms_graph accounts with mail switched on."""
    out = []
    for o in goa_client().get_accounts():
        a = o.get_account()
        if a.props.provider_type == "ms_graph" and not a.props.mail_disabled and o.get_oauth2_based():
            out.append((a.props.presentation_identity, a.props.presentation_identity))
    return out


def _addr(r):
    e = (r or {}).get("emailAddress") or {}
    addr = e.get("address") or ""
    return e.get("name") or addr.split("@")[0], addr


def _addr_list(rs):
    return ", ".join(email.utils.formataddr(_addr(r)) for r in rs or [])


def _ts(iso):
    try:
        return time.mktime(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except (TypeError, ValueError):
        return time.time()


class Graph:
    kind = "ms"

    def __init__(self, identity, name=None):
        self.identity = identity
        self.address = identity
        self.key = "ms:" + identity
        self.name = name
        self.http = requests.Session()
        self.lock = threading.RLock()
        self._snooze_id = None

    # auth ---------------------------------------------------------------
    def _obj(self):
        for o in goa_client().get_accounts():
            a = o.get_account()
            if a.props.provider_type == "ms_graph" and a.props.presentation_identity == self.identity:
                return o
        raise MailError("%s is no longer in Online Accounts" % self.identity)

    def _token(self, refresh=False):
        o = self._obj()
        try:
            if refresh:
                o.get_account().call_ensure_credentials_sync(None)
            return o.get_oauth2_based().call_get_access_token_sync(None)[0]
        except Exception as e:  # noqa: BLE001 - GLib.Error from D-Bus
            raise MailError("%s: sign in again in Settings > Online Accounts (%s)"
                            % (self.identity, getattr(e, "message", e))) from e

    def _req(self, method, path, raw=False, **kw):
        headers = kw.pop("headers", {})
        headers["Prefer"] = 'IdType="ImmutableId"'
        for attempt in (0, 1):
            headers["Authorization"] = "Bearer " + self._token(refresh=attempt == 1)
            try:
                r = self.http.request(method, API + path, headers=headers, timeout=45, **kw)
            except requests.RequestException as e:
                raise MailError("Offline: %s" % e) from e
            if r.status_code != 401:
                break
        if r.status_code == 404:
            raise MailError("That message is gone")
        if r.status_code >= 400:
            try:
                msg = r.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                msg = r.text[:200]
            raise MailError("%s: %s" % (self.identity, msg))
        if raw:
            return r.content
        return r.json() if r.content else {}

    def check_login(self):
        self._req("GET", "/mailFolders/inbox", params={"$select": "id"})

    def close(self):
        pass

    # reading ------------------------------------------------------------
    def _msg(self, d, folder):
        name, addr = _addr(d.get("from"))
        size = 0
        for p in d.get("singleValueExtendedProperties") or []:
            if p.get("id", "").lower().replace("0x0", "0x") == SIZE_PROP.lower().replace("0x0", "0x"):
                size = int(p.get("value") or 0)
        return {
            "acct": self.key,
            "msgid": d["id"],
            "thrid": d.get("conversationId") or "",
            "uid": 0,
            "folder": folder,
            "size": size,
            "seen": bool(d.get("isRead")),
            "date": _ts(d.get("receivedDateTime")),
            "from_name": name,
            "from_addr": addr,
            "to": _addr_list(d.get("toRecipients")),
            "cc": _addr_list(d.get("ccRecipients")),
            "reply_to": _addr_list(d.get("replyTo")),
            "subject": (d.get("subject") or "").strip() or "(no subject)",
            "message_id": d.get("internetMessageId") or "",
            "references": "",
            "snippet": gmail.clean_snippet(d.get("bodyPreview") or ""),
        }

    def list_folder(self, folder="INBOX", limit=gmail.LIST_LIMIT, since_uid=0):
        """Newest `limit` messages, newest first. Returns (None, [msg]) like Gmail."""
        f = FOLDERS.get(folder, folder)
        d = self._req("GET", "/mailFolders/%s/messages" % f, params={
            "$top": str(limit), "$select": SELECT, "$orderby": "receivedDateTime desc",
            "$expand": "singleValueExtendedProperties($filter=id eq '%s')" % SIZE_PROP})
        return None, [self._msg(x, folder) for x in d.get("value", [])]

    def list_sent(self, limit=gmail.LIST_LIMIT):
        return self.list_folder("sent", limit)

    def get_raw(self, msg):
        raw = gmail.cached_raw(msg["msgid"])
        if raw:
            return raw
        raw = self._req("GET", "/messages/%s/$value" % msg["msgid"], raw=True)
        gmail._cache_raw(msg["msgid"], raw)
        return raw

    def prefetch(self, msgs):
        for m in msgs:
            if gmail.cached_raw(m["msgid"]) is None and m.get("size", 0) <= gmail.PREFETCH_MAX:
                try:
                    self.get_raw(m)
                except MailError:
                    pass
        return msgs

    def mark_seen(self, msgid, seen=True):
        self._req("PATCH", "/messages/%s" % msgid, json={"isRead": seen})

    # moving -------------------------------------------------------------
    def _move(self, msgid, dest):
        self._req("POST", "/messages/%s/move" % msgid, json={"destinationId": dest})

    def trash(self, msgid):
        self._move(msgid, "deleteditems")

    def trash_many(self, msgids):
        for mid in msgids:
            self.trash(mid)

    def to_inbox(self, msgid, folder=None):
        """Not spam / restore from Trash."""
        self._move(msgid, "inbox")

    def _snooze_folder(self):
        if self._snooze_id:
            return self._snooze_id
        d = self._req("GET", "/mailFolders", params={
            "$filter": "displayName eq '%s'" % SNOOZE_FOLDER, "$select": "id"})
        if d.get("value"):
            self._snooze_id = d["value"][0]["id"]
        else:
            self._snooze_id = self._req("POST", "/mailFolders",
                                        json={"displayName": SNOOZE_FOLDER, "isHidden": False})["id"]
        return self._snooze_id

    def snooze(self, msgid):
        self._move(msgid, self._snooze_folder())

    def snooze_many(self, msgids):
        for mid in msgids:
            self.snooze(mid)

    def unsnooze(self, msgid):
        try:
            self._move(msgid, "inbox")
        except MailError as e:
            if "gone" in str(e):
                return False
            raise
        self.mark_seen(msgid, False)
        return True

    # sending ------------------------------------------------------------
    def send(self, to, cc, subject, body, in_reply_to=None, references=None, attachments=()):
        import base64
        msg = gmail.build_message(self.name, self.address, to, cc, subject, body,
                                  in_reply_to, references, attachments)
        self._req("POST", "/sendMail", data=base64.b64encode(msg.as_bytes()),
                  headers={"Content-Type": "text/plain"})  # Graph files it under Sent Items
