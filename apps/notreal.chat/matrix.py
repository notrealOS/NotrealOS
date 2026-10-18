"""Matrix side of notarealChat: /sync loop into the Store, sending, media, bridge commands.

Plain client-server API over HTTPS (the rooms aren't encrypted). Everything here runs
in worker threads; the UI gets told through the on_change callback.
"""
import hashlib
import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
import uuid

import requests

from store import preview


def pc(key, default=""):
    """Address of our PC server (see docs/pc-link.md); set in ~/.config/notreal/pc.json."""
    try:
        with open(os.path.expanduser("~/.config/notreal/pc.json")) as f:
            return json.load(f).get(key) or default
    except (OSError, ValueError):
        return default

HS = pc("matrix", "https://matrix.example.ts.net")
CONF = os.path.expanduser("~/.config/notarealchat")
SESSION = os.path.join(CONF, "session.json")
MEDIA = os.path.expanduser("~/.cache/notarealchat/media")
BOT_NET = {"whatsappbot": "wa", "facebookbot": "fb", "metabot": "fb", "messengerbot": "fb",
           "instagrambot": "ig", "gvoicebot": "gv"}
GHOST = re.compile(r"^@(whatsapp|facebook|meta|messenger|instagram|gvoice)[_a-z]*[-_]")
BOT = re.compile(r"^@(whatsappbot|facebookbot|metabot|messengerbot|instagrambot|gvoicebot):")
NET_SUFFIX = re.compile(r"\s*\((WA|FB|IG|GV|SMS)\)\s*$")
ARCHIVE_TAG = "u.archived"
FILTER = {
    "presence": {"not_types": ["*"]},
    "account_data": {"not_types": ["*"]},
    "room": {
        "timeline": {"limit": 30, "lazy_load_members": True},
        "state": {"lazy_load_members": True},
        "ephemeral": {"types": ["m.receipt"]},
        "account_data": {"types": ["m.tag"]},
    },
}


def q(s):
    return urllib.parse.quote(s, safe="")


class Matrix:
    def __init__(self, store, on_change, on_status=None):
        self.store = store
        self.on_change = on_change          # (chat ids, [new incoming messages]) from a worker thread
        self.on_status = on_status or (lambda s: None)
        with open(SESSION) as f:
            s = json.load(f)
        self.token, self.me = s["token"], s["user"]
        self.http = requests.Session()
        self.http.headers["Authorization"] = "Bearer " + self.token
        self.running = False
        self.bot_room_cache = {}
        # Your OWN bridge ghosts (e.g. @whatsapp_lid-...: what you send from the real WhatsApp/Messenger
        # app arrives from them): their messages are "mine" too. Learned from the members table.
        self.selves = set(json.loads(self.store.get("self_ghosts") or "[]"))
        self._detect_selves()

    def is_self(self, uid):
        return uid == self.me or uid in self.selves

    def _detect_selves(self):
        """A ghost that is in (nearly) every DM of its network is you. Re-flags old messages on change."""
        st = self.store
        rows = st.q("SELECT m.user AS user, c.id AS chat FROM members m JOIN chats c ON c.id = m.chat "
                    "WHERE c.dm = 1")
        per_net, per_user = {}, {}
        for r in rows:
            g = GHOST.match(r["user"])
            if not g or BOT.match(r["user"]):
                continue
            per_net.setdefault(g.group(1), set()).add(r["chat"])
            per_user.setdefault((g.group(1), r["user"]), set()).add(r["chat"])
        found = {u for (net, u), chats in per_user.items()
                 if len(chats) >= 3 and len(chats) >= 0.8 * len(per_net[net])}
        found |= self.selves
        if found == self.selves and self.store.get("self_ghosts") is not None:
            return
        self.selves = found
        st.put("self_ghosts", json.dumps(sorted(found)))
        if found:
            marks = ",".join("?" * len(found))
            st.x("UPDATE messages SET mine=1 WHERE sender IN (%s)" % marks, tuple(found))
            st.x("UPDATE chats SET last_mine = COALESCE((SELECT mine FROM messages WHERE messages.chat = chats.id "
                 "AND kind NOT IN ('reaction','deleted') ORDER BY ts DESC LIMIT 1), last_mine)")
        print("own bridge ghosts:", sorted(found), flush=True)

    # --- HTTP ------------------------------------------------------------------
    def api(self, method, path, body=None, params=None, timeout=30):
        r = self.http.request(method, HS + "/_matrix/client/v3" + path, json=body,
                              params=params, timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError("%s %s: %s" % (r.status_code, path.split("?")[0], r.text[:200]))
        return r.json()

    # --- sync -------------------------------------------------------------------
    def start(self):
        self.running = True
        threading.Thread(target=self._loop, daemon=True, name="matrix-sync").start()

    def _loop(self):
        backoff = 2
        while self.running:
            since = self.store.get("since")
            params = {"timeout": 30000 if since else 0, "filter": json.dumps(FILTER)}
            if since:
                params["since"] = since
            try:
                r = self.http.get(HS + "/_matrix/client/v3/sync", params=params, timeout=60)
                if r.status_code == 401:
                    self.on_status("logged out")
                    return
                r.raise_for_status()
                data = r.json()
            except Exception as e:  # noqa: BLE001
                print("sync error:", repr(e), flush=True)
                self.on_status("offline")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            backoff = 2
            self.on_status("online")
            try:
                changed, fresh = self._process(data, initial=not since)
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                changed, fresh = set(), []
                print("process error:", repr(e), flush=True)
            self.store.put("since", data["next_batch"])
            if changed:
                self._detect_selves()
            if changed or fresh:
                self.on_change(changed, fresh)

    def _process(self, data, initial):
        rooms = data.get("rooms", {})
        changed, fresh = set(), []
        for rid, room in rooms.get("join", {}).items():
            fresh += self._room(rid, room, initial)
            changed.add(rid)
        for rid, room in rooms.get("invite", {}).items():
            self._invite(rid, room)
            changed.add(rid)
        for rid in rooms.get("leave", {}):
            self.store.drop_chat(rid)
            changed.add(rid)
        return changed, fresh

    def _room(self, rid, room, initial):
        st = self.store
        existed = st.chat(rid) is not None
        st.upsert_chat(rid, invite=0)
        chat = st.chat(rid)
        extra = json.loads(chat["extra"] or "{}")
        tl = room.get("timeline", {})
        events = tl.get("events", [])
        if tl.get("limited") and existed:
            st.clear_messages(rid)  # gap: start over from this chunk, older via backfill
        if tl.get("prev_batch") and (tl.get("limited") or not existed or not chat["prev_batch"]):
            st.upsert_chat(rid, prev_batch=tl["prev_batch"])
        for ev in room.get("state", {}).get("events", []) + [e for e in events if "state_key" in e]:
            self._state(rid, ev, extra)
        fresh = []
        for ev in events:
            m = self.event(rid, ev)
            if m and not m["mine"] and m["kind"] not in ("reaction", "deleted") and not initial:
                fresh.append(m)
        summ = room.get("summary", {})
        if "m.heroes" in summ:
            extra["heroes"] = summ["m.heroes"]
        if "m.joined_member_count" in summ:
            extra["joined"] = summ["m.joined_member_count"]
        for ev in room.get("account_data", {}).get("events", []):
            if ev["type"] == "m.tag":
                st.upsert_chat(rid, archived=int(ARCHIVE_TAG in ev["content"].get("tags", {})))
        for ev in room.get("ephemeral", {}).get("events", []):
            if ev["type"] == "m.receipt":
                self._receipts(rid, ev["content"])
        un = room.get("unread_notifications", {})
        fields = {"extra": json.dumps(extra)}
        if "notification_count" in un:
            fields["unread"] = un["notification_count"]
        fields["net"] = extra.get("net", "mx")
        fields["dm"] = int(self.is_dm(rid, extra))
        fields["hidden"] = int("net" not in extra and extra.get("bot", False))
        fields["name"] = self._name(rid, extra)
        if extra.get("avatar"):
            fields["avatar"] = extra["avatar"]
        st.upsert_chat(rid, **fields)
        st.refresh_last(rid)
        if fresh and st.chat(rid)["archived"]:
            self.set_archived(rid, False)  # a new message brings the chat back
        return fresh

    def is_dm(self, rid, extra):
        """Bridges tag DMs "dm" but leave groups untagged, so untagged = count the other people."""
        if "net" not in extra:
            return extra.get("joined", 2) <= 2
        if extra.get("rtype"):
            return extra["rtype"] == "dm"
        others = [r["user"] for r in self.store.q("SELECT user FROM members WHERE chat=?", (rid,))
                  if not BOT.match(r["user"]) and not self.is_self(r["user"])]
        return len(others) <= 1 and extra.get("joined", 0) <= 4

    def _state(self, rid, ev, extra):
        t, c = ev["type"], ev.get("content", {})
        if t == "m.room.name":
            extra["name"] = c.get("name", "")
        elif t == "m.room.avatar":
            extra["avatar"] = c.get("url", "")
        elif t in ("m.bridge", "uk.half-shot.bridge"):
            bot = c.get("bridgebot", "")[1:].split(":")[0]
            extra["net"] = BOT_NET.get(bot, "mx")
            extra["rtype"] = c.get("com.beeper.room_type") or c.get("com.beeper.room_type.v2") or ""
        elif t == "m.room.member":
            uid = ev["state_key"]
            if c.get("membership") in ("join", "invite"):
                self.store.set_member(rid, uid, c.get("displayname") or uid[1:].split(":")[0],
                                      c.get("avatar_url"))
                if BOT.match(uid):
                    extra["bot"] = True

    def _name(self, rid, extra):
        name = extra.get("name")
        if not name:
            heroes = [h for h in extra.get("heroes", []) if not self.is_self(h)]
            names = []
            for h in heroes[:3]:
                mem = self.store.member(rid, h)
                names.append(mem["name"] if mem else h[1:].split(":")[0])
            name = ", ".join(names) or "empty chat"
        return NET_SUFFIX.sub("", name)

    def _receipts(self, rid, content):
        best = 0
        for eid, kinds in content.items():
            for uid, info in kinds.get("m.read", {}).items():
                if not self.is_self(uid) and not BOT.match(uid):
                    best = max(best, info.get("ts", 0))
        if best:
            cur = self.store.chat(rid)["seen_ts"] or 0
            if best > cur:
                self.store.upsert_chat(rid, seen_ts=best)

    def _invite(self, rid, room):
        evs = room.get("invite_state", {}).get("events", [])
        name, inviter = "", ""
        for ev in evs:
            if ev["type"] == "m.room.name":
                name = ev["content"].get("name", "")
            if ev["type"] == "m.room.member" and ev.get("state_key") == self.me:
                inviter = ev.get("sender", "")
        # no auto-join: the user turned auto-accept off (10-10); tap the row to join
        self.store.upsert_chat(rid, invite=1, name=NET_SUFFIX.sub("", name) or inviter,
                               last_ts=int(time.time() * 1000), last_text="invited you")

    # --- events -> messages ------------------------------------------------------
    def event(self, rid, ev):
        """Store one timeline event; returns the message dict if it became a message."""
        st = self.store
        t, c = ev.get("type"), ev.get("content", {})
        eid, sender, ts = ev.get("event_id"), ev.get("sender", ""), ev.get("origin_server_ts", 0)
        mine = self.is_self(sender)
        txn = ev.get("unsigned", {}).get("transaction_id")
        if txn:
            st.x("DELETE FROM messages WHERE chat=? AND id=?", (rid, "~" + txn))
        rel = c.get("m.relates_to", {})
        if t == "m.room.redaction":
            target = ev.get("redacts") or c.get("redacts")
            old = st.message(rid, target)
            if old and old["kind"] == "reaction":
                st.x("DELETE FROM messages WHERE chat=? AND id=?", (rid, target))
            elif old:
                st.update_message(rid, target, kind="deleted", body="", media=None)
            return None
        if t == "m.reaction":
            if rel.get("rel_type") == "m.annotation":
                st.add_message(rid, eid, sender, ts, "reaction", rel.get("key", ""),
                               reply_to=rel.get("event_id"), mine=mine)
            return None
        if t not in ("m.room.message", "m.sticker"):
            return None
        if "body" not in c and t == "m.room.message":
            return None  # redacted
        if rel.get("rel_type") == "m.replace":
            new = c.get("m.new_content", c)
            st.update_message(rid, rel.get("event_id"), body=new.get("body", ""), edited=1)
            return None
        kind, body, media = self.parse(t, c)
        reply = rel.get("m.in_reply_to", {}).get("event_id")
        if reply and kind == "text":
            body = strip_reply_fallback(body)
        st.add_message(rid, eid, sender, ts, kind, body, media, reply, mine)
        return {"chat": rid, "id": eid, "sender": sender, "ts": ts, "kind": kind, "body": body,
                "mine": mine, "media": media}

    @staticmethod
    def parse(t, c):
        info = c.get("info") or {}
        media = None
        if c.get("url") or c.get("file"):
            media = {"url": c.get("url") or c.get("file", {}).get("url"),
                     "mime": info.get("mimetype", ""), "w": info.get("w"), "h": info.get("h"),
                     "size": info.get("size"), "name": c.get("filename") or c.get("body", ""),
                     "thumb": info.get("thumbnail_url"), "dur": info.get("duration")}
        if t == "m.sticker":
            return "sticker", c.get("body", ""), media
        mt = c.get("msgtype", "m.text")
        body = c.get("body", "")
        if mt == "m.image":
            gif = media and (media["mime"] == "image/gif" or info.get("fi.mau.gif"))
            return ("gif" if gif else "image"), body, media
        if mt == "m.video":
            gif = info.get("fi.mau.gif") or info.get("fi.mau.loop")
            return ("gif" if gif else "video"), body, media
        if mt == "m.audio":
            return "audio", body, media
        if mt == "m.file":
            return "file", body, media
        if mt == "m.location":
            return "location", c.get("geo_uri", body), None
        return {"m.notice": "notice", "m.emote": "emote"}.get(mt, "text"), body, None

    def backfill(self, rid, limit=40):
        """Fetch older messages; returns how many arrived."""
        chat = self.store.chat(rid)
        if not chat or not chat["prev_batch"]:
            return 0
        f = json.dumps({"lazy_load_members": True})
        data = self.api("GET", "/rooms/%s/messages" % q(rid),
                        params={"from": chat["prev_batch"], "dir": "b", "limit": limit, "filter": f})
        extra = json.loads(chat["extra"] or "{}")
        for ev in data.get("state", []):
            self._state(rid, ev, extra)
        n = 0
        for ev in data.get("chunk", []):
            if self.event(rid, ev):
                n += 1
        self.store.upsert_chat(rid, prev_batch=data.get("end") or "")
        return n if data.get("end") else -1

    # --- actions -------------------------------------------------------------------
    def _send(self, rid, content, local):
        txn = uuid.uuid4().hex
        st = self.store
        st.add_message(rid, "~" + txn, self.me, int(time.time() * 1000), local["kind"], local["body"],
                       local.get("media"), local.get("reply_to"), True, "sending")
        st.refresh_last(rid)
        self.on_change({rid}, [])
        try:
            eid = self.api("PUT", "/rooms/%s/send/m.room.message/%s" % (q(rid), txn), content)["event_id"]
        except Exception:
            st.update_message(rid, "~" + txn, status="failed")
            self.on_change({rid}, [])
            raise
        if st.message(rid, eid):
            st.x("DELETE FROM messages WHERE chat=? AND id=?", (rid, "~" + txn))
        else:
            st.update_message(rid, "~" + txn, id=eid, status="sent")
        self.on_change({rid}, [])
        return eid

    def send_text(self, rid, text, reply_to=None):
        content = {"msgtype": "m.text", "body": text}
        if reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        return self._send(rid, content, {"kind": "text", "body": text, "reply_to": reply_to})

    def upload(self, path, mime=None, name=None):
        mime = mime or mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            r = self.http.post(HS + "/_matrix/media/v3/upload", data=f,
                               params={"filename": name or os.path.basename(path)},
                               headers={"Content-Type": mime}, timeout=300)
        r.raise_for_status()
        return r.json()["content_uri"]

    def send_media(self, rid, path, kind, mime, info=None, name=None, reply_to=None):
        """kind: image / video / gif / file / audio."""
        info = dict(info or {})
        info["mimetype"] = mime
        info["size"] = os.path.getsize(path)
        name = name or os.path.basename(path)
        url = self.upload(path, mime, name)
        msgtype = {"image": "m.image", "video": "m.video", "audio": "m.audio", "file": "m.file",
                   "gif": "m.video" if mime.startswith("video/") else "m.image"}[kind]
        if kind == "gif":  # mautrix bridges send these as looping GIFs
            info.update({"fi.mau.gif": True, "fi.mau.loop": True, "fi.mau.autoplay": True,
                         "fi.mau.hide_controls": True, "fi.mau.no_audio": True})
        content = {"msgtype": msgtype, "body": name, "url": url, "info": info, "filename": name}
        if reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        media = {"url": url, "mime": mime, "w": info.get("w"), "h": info.get("h"), "name": name,
                 "local": path}
        return self._send(rid, content, {"kind": kind, "body": name, "media": media, "reply_to": reply_to})

    def send_voice(self, rid, path, duration_ms, waveform, reply_to=None):
        """An Ogg/Opus voice note (MSC3245) -> WhatsApp/Messenger voice messages via the bridges."""
        size = os.path.getsize(path)
        url = self.upload(path, "audio/ogg", "Voice message.ogg")
        content = {"msgtype": "m.audio", "body": "Voice message", "url": url, "filename": "Voice message.ogg",
                   "info": {"mimetype": "audio/ogg", "size": size, "duration": duration_ms},
                   "org.matrix.msc1767.audio": {"duration": duration_ms, "waveform": waveform},
                   "org.matrix.msc3245.voice": {}}
        if reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        media = {"url": url, "mime": "audio/ogg", "dur": duration_ms, "name": "Voice message.ogg", "local": path,
                 "size": size}
        return self._send(rid, content, {"kind": "audio", "body": "Voice message", "media": media,
                                         "reply_to": reply_to})

    def react(self, rid, eid, key):
        self.api("PUT", "/rooms/%s/send/m.reaction/%s" % (q(rid), uuid.uuid4().hex),
                 {"m.relates_to": {"rel_type": "m.annotation", "event_id": eid, "key": key}})

    def redact(self, rid, eid):
        self.api("PUT", "/rooms/%s/redact/%s/%s" % (q(rid), q(eid), uuid.uuid4().hex), {})

    def mark_read(self, rid):
        last = self.store.q("SELECT id FROM messages WHERE chat=? AND id LIKE '$%' ORDER BY ts DESC LIMIT 1",
                            (rid,))
        self.store.upsert_chat(rid, unread=0)
        if last:
            eid = last[0]["id"]
            self.api("POST", "/rooms/%s/read_markers" % q(rid), {"m.fully_read": eid, "m.read": eid})

    def mark_unread(self, rid):
        self.store.upsert_chat(rid, unread=max(1, self.store.chat(rid)["unread"]))

    def set_archived(self, rid, on):
        self.store.upsert_chat(rid, archived=int(on))
        path = "/user/%s/rooms/%s/tags/%s" % (q(self.me), q(rid), ARCHIVE_TAG)
        if on:
            self.api("PUT", path, {"order": 0.5})
        else:
            self.api("DELETE", path)

    def join(self, rid):
        self.api("POST", "/join/" + q(rid), {})

    def leave(self, rid):
        self.api("POST", "/rooms/%s/leave" % q(rid), {})
        try:
            self.api("POST", "/rooms/%s/forget" % q(rid), {})
        except Exception:  # noqa: BLE001
            pass
        self.store.drop_chat(rid)

    def rename(self, rid, name):
        self.api("PUT", "/rooms/%s/state/m.room.name/" % q(rid), {"name": name})

    # --- media ----------------------------------------------------------------------
    def _media_path(self, mxc, tag):
        h = hashlib.sha1((mxc + tag).encode()).hexdigest()
        return os.path.join(MEDIA, h[:2], h)

    def fetch(self, mxc, w=None, h=None, crop=False):
        """mxc -> local file (cached). With w/h a server thumbnail, else the full file."""
        if not mxc or not mxc.startswith("mxc://"):
            return None
        tag = "%sx%s%s" % (w, h, "c" if crop else "") if w else "full"
        path = self._media_path(mxc, tag)
        if os.path.exists(path):
            return path
        server, mid = mxc[6:].split("/", 1)
        if w:
            url = "%s/_matrix/client/v1/media/thumbnail/%s/%s" % (HS, server, mid)
            params = {"width": w, "height": h or w, "method": "crop" if crop else "scale", "animated": "false"}
        else:
            url = "%s/_matrix/client/v1/media/download/%s/%s" % (HS, server, mid)
            params = None
        r = self.http.get(url, params=params, timeout=120)
        if r.status_code >= 400 and w:  # no thumbnail (e.g. video) -> the file itself
            return None
        r.raise_for_status()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".part", "wb") as f:
            f.write(r.content)
        os.replace(path + ".part", path)
        return path

    # --- bridges --------------------------------------------------------------------------
    def bot_room(self, bot="whatsappbot"):
        if bot in self.bot_room_cache:
            return self.bot_room_cache[bot]
        for c in self.store.hidden_chats():
            if self.store.member(c["id"], "@%s:%s" % (bot, self.me.split(":")[1])):
                self.bot_room_cache[bot] = c["id"]
                return c["id"]
        for r in self.api("GET", "/joined_rooms")["joined_rooms"]:
            mem = self.api("GET", "/rooms/%s/joined_members" % q(r))["joined"]
            if len(mem) == 2 and any(u.startswith("@%s:" % bot) for u in mem):
                self.bot_room_cache[bot] = r
                return r
        raise RuntimeError("No %s room" % bot)

    def ask(self, command, bot="whatsappbot", timeout=25):
        """Send a bridge command, return the bot's reply text."""
        room = q(self.bot_room(bot))
        sent = self.api("PUT", "/rooms/%s/send/m.room.message/%s" % (room, uuid.uuid4().hex),
                        {"msgtype": "m.text", "body": command})["event_id"]
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(0.8)
            chunk = self.api("GET", "/rooms/%s/messages?dir=b&limit=10" % room)["chunk"]
            for e in chunk:  # newest first; stop at our own command
                if e["event_id"] == sent:
                    break
                if e.get("type") == "m.room.message" and e["sender"].startswith("@%s:" % bot):
                    return e["content"].get("body", "")
        raise RuntimeError("The bridge didn't answer")

    def has_whatsapp(self, number):
        return self.ask("resolve-identifier " + number).startswith("Found")

    def start_whatsapp(self, number, name):
        """Create (or find) the WhatsApp chat with a number; returns the room id."""
        reply = self.ask("start-chat " + number)
        m = re.search(r"(![A-Za-z0-9_\-]+(:[A-Za-z0-9.\-]+)?)", reply)
        if not m:
            raise RuntimeError(reply.splitlines()[0] if reply else "No chat created")
        rid = m.group(1)
        if ":" not in rid:
            rid += ":" + self.me.split(":")[1]
        self.join(rid)  # fine when already joined
        try:
            cur = self.api("GET", "/rooms/%s/state/m.room.name/" % q(rid)).get("name", "")
        except RuntimeError:
            cur = ""
        if name and (not cur or re.match(r"^\+?[\d\s\-()]+( \(WA\))?$", cur)):
            self.rename(rid, "%s (WA)" % name)
        return rid


def strip_reply_fallback(body):
    """Drop the '> <@user> quoted text' lines clients put before a reply."""
    lines = (body or "").split("\n")
    if not lines or not lines[0].startswith("> "):
        return body
    i = 0
    while i < len(lines) and lines[i].startswith(">"):
        i += 1
    return "\n".join(lines[i:]).lstrip("\n")


__all__ = ["Matrix", "preview", "strip_reply_fallback"]
