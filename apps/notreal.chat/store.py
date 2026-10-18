"""notarealChat's local store: chats + messages for Matrix rooms and SMS threads.

One SQLite file, shared by the sync thread, the SMS watcher and the UI (one lock).
Chat ids: Matrix room ids ("!abc:server") or "sms:+4670...".
"""
import json
import os
import sqlite3
import threading

DATA = os.path.expanduser("~/.local/share/notarealchat")
DB = os.path.join(DATA, "chat.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
  id TEXT PRIMARY KEY, net TEXT, name TEXT, avatar TEXT,
  last_ts INTEGER DEFAULT 0, last_text TEXT DEFAULT '', last_mine INTEGER DEFAULT 0,
  unread INTEGER DEFAULT 0, archived INTEGER DEFAULT 0, muted INTEGER DEFAULT 0,
  hidden INTEGER DEFAULT 0, invite INTEGER DEFAULT 0, dm INTEGER DEFAULT 1,
  prev_batch TEXT, seen_ts INTEGER DEFAULT 0, extra TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS messages (
  chat TEXT, id TEXT, sender TEXT, ts INTEGER, kind TEXT, body TEXT,
  media TEXT, reply_to TEXT, mine INTEGER DEFAULT 0, status TEXT DEFAULT 'sent',
  reactions TEXT DEFAULT '{}', edited INTEGER DEFAULT 0,
  PRIMARY KEY (chat, id));
CREATE INDEX IF NOT EXISTS msg_ts ON messages (chat, ts);
CREATE TABLE IF NOT EXISTS members (
  chat TEXT, user TEXT, name TEXT, avatar TEXT, PRIMARY KEY (chat, user));
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path=DB):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.execute("PRAGMA journal_mode=WAL")

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.rowcount

    # key/value ---------------------------------------------------------------
    def get(self, k, default=None):
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def put(self, k, v):
        self.x("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))

    # chats -------------------------------------------------------------------
    def chat(self, cid):
        r = self.q("SELECT * FROM chats WHERE id=?", (cid,))
        return r[0] if r else None

    def chats(self):
        return self.q("SELECT * FROM chats WHERE hidden=0 ORDER BY last_ts DESC")

    def hidden_chats(self):
        return self.q("SELECT * FROM chats WHERE hidden=1 ORDER BY last_ts DESC")

    def upsert_chat(self, cid, **f):
        with self.lock:
            if not self.chat(cid):
                self.db.execute("INSERT INTO chats (id) VALUES (?)", (cid,))
            if f:
                self.db.execute("UPDATE chats SET %s WHERE id=?" % ",".join("%s=?" % k for k in f),
                                (*f.values(), cid))
            self.db.commit()

    def drop_chat(self, cid):
        with self.lock:
            for t, col in (("chats", "id"), ("messages", "chat"), ("members", "chat")):
                self.db.execute("DELETE FROM %s WHERE %s=?" % (t, col), (cid,))
            self.db.commit()

    # messages ----------------------------------------------------------------
    def add_message(self, chat, mid, sender, ts, kind, body, media=None, reply_to=None,
                    mine=False, status="sent"):
        self.x("INSERT OR IGNORE INTO messages (chat,id,sender,ts,kind,body,media,reply_to,mine,status)"
               " VALUES (?,?,?,?,?,?,?,?,?,?)",
               (chat, mid, sender, ts, kind, body, json.dumps(media) if media else None,
                reply_to, int(mine), status))

    def message(self, chat, mid):
        r = self.q("SELECT * FROM messages WHERE chat=? AND id=?", (chat, mid))
        return r[0] if r else None

    def messages(self, chat, limit=60, before=None):
        if before is None:
            rows = self.q("SELECT * FROM messages WHERE chat=? ORDER BY ts DESC LIMIT ?", (chat, limit))
        else:
            rows = self.q("SELECT * FROM messages WHERE chat=? AND ts<? ORDER BY ts DESC LIMIT ?",
                          (chat, before, limit))
        return rows[::-1]

    def update_message(self, chat, mid, **f):
        return self.x("UPDATE messages SET %s WHERE chat=? AND id=?" % ",".join("%s=?" % k for k in f),
                      (*f.values(), chat, mid))

    def clear_messages(self, chat):
        self.x("DELETE FROM messages WHERE chat=?", (chat,))

    def refresh_last(self, chat):
        """Recompute the chat's preview line from its newest message."""
        r = self.q("SELECT * FROM messages WHERE chat=? AND kind!='reaction' ORDER BY ts DESC LIMIT 1",
                   (chat,))
        if r:
            m = r[0]
            self.upsert_chat(chat, last_ts=m["ts"], last_text=preview(m), last_mine=m["mine"])

    # members -----------------------------------------------------------------
    def set_member(self, chat, user, name, avatar):
        self.x("INSERT OR REPLACE INTO members VALUES (?,?,?,?)", (chat, user, name, avatar))

    def member(self, chat, user):
        r = self.q("SELECT * FROM members WHERE chat=? AND user=?", (chat, user))
        return r[0] if r else None


def preview(m):
    """One line for the chat list."""
    k = m["kind"]
    if k in ("text", "notice", "emote"):
        return (m["body"] or "").replace("\n", " ")[:120]
    return {"image": "📷 photo", "gif": "GIF", "video": "🎬 video", "audio": "🎤 voice message",
            "file": "📎 " + (m["body"] or "file"), "sticker": "sticker", "deleted": "message deleted",
            "location": "📍 location"}.get(k, m["body"] or k)
