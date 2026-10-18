"""iotas_notes - read/write notes in the Iotas notes app's database.

Used by voiced (voice notes), PhantomShell (`note`) and the overview widget, so all notes
live in Iotas (~/.local/share/iotas/notes.db). Notes we create get the category
CATEGORY, and clear() only removes those, never notes written in Iotas itself.
Rows look like the ones Iotas makes for a local (not synced) note. A running Iotas
window only shows new notes after it is reopened.
"""

import contextlib
import os
import sqlite3
import time

DB = os.path.expanduser("~/.local/share/iotas/notes.db")
CATEGORY = "notarealShell"
EXCERPT_LENGTH = 200


def available():
    return os.path.exists(DB)


@contextlib.contextmanager
def _connect():
    # sqlite3's own "with conn" only commits, it never closes (leaked fds in long-running daemons, 10-12)
    c = sqlite3.connect(DB, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def add(text, category=CATEGORY):
    text = text.strip()
    first = text.splitlines()[0] if text else ""
    title = first if len(first) <= 60 else first[:57].rstrip() + "…"
    rest = text[len(first):].strip()
    excerpt = "   ".join(l.strip() for l in rest.splitlines() if l.strip())[:EXCERPT_LENGTH]
    with _connect() as c:
        c.execute(
            "INSERT INTO note (title, content, excerpt, last_modified, favourite, category,"
            " remote_id, dirty, locally_deleted, etag, read_only)"
            " VALUES (?, ?, ?, ?, 0, ?, -1, 1, 0, '', 0)",
            (title, text + "\n", excerpt, int(time.time()), category))


def recent(n=None, category=None):
    """Newest first: [{"id", "time", "title", "text", "category"}]."""
    q = "SELECT id, title, content, last_modified, category FROM note WHERE locally_deleted = 0"
    args = []
    if category is not None:
        q += " AND category = ?"
        args.append(category)
    q += " ORDER BY last_modified DESC"
    if n:
        q += " LIMIT %d" % int(n)
    try:
        with _connect() as c:
            rows = c.execute(q, args).fetchall()
    except sqlite3.Error:
        return []
    return [{"id": r["id"], "time": r["last_modified"], "title": r["title"],
             "text": r["content"].strip(), "category": r["category"]} for r in rows]


def delete(note_id):
    with _connect() as c:
        c.execute("DELETE FROM note WHERE id = ?", (note_id,))


def clear(category=CATEGORY):
    """Delete our notes (only CATEGORY); returns how many."""
    with _connect() as c:
        return c.execute("DELETE FROM note WHERE category = ?", (category,)).rowcount
