"""Settings, SQLite library cache, offline pins, auto cache, downloads, covers."""
import json
import random
import os
import queue
import sqlite3
import subprocess
import threading
import time
import requests
import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GLib, Gio, GdkPixbuf  # noqa: E402

from api import Subsonic, Offline, ApiError

CONF_DIR = os.path.expanduser("~/.config/spin")
DATA_DIR = os.path.expanduser("~/.local/share/spin")
AUDIO_DIR = os.path.join(DATA_DIR, "audio")
COVER_DIR = os.path.join(DATA_DIR, "covers")
ANIM_DIR = os.path.join(DATA_DIR, "anim")
for d in (CONF_DIR, AUDIO_DIR, COVER_DIR, ANIM_DIR):
    os.makedirs(d, exist_ok=True)


def pc(key, default=""):
    """Address of our PC server (see docs/pc-link.md); set in ~/.config/notreal/pc.json."""
    try:
        with open(os.path.expanduser("~/.config/notreal/pc.json")) as f:
            return json.load(f).get(key) or default
    except (OSError, ValueError):
        return default


DEFAULTS = {
    "server": "http://%s:4533" % pc("host", "localhost"), "user": "", "token": "", "salt": "",
    "anim_server": "http://%s:4535" % pc("host", "localhost"),
    "output": "pulse", "direct_device": "",
    "replaygain": "album", "rg_preamp": 0.0,
    "eq_enabled": False, "eq_preset": "Flat", "eq_bands": [0.0] * 10,
    "quality_wifi": "raw", "quality_mobile": "opus192",
    "auto_cache": True, "cache_limit_gb": 4, "cache_on_mobile": False, "prefetch": 3,
    "animated_covers": True, "grid_anim": False, "anim_on_mobile": True, "rpm": 8.0, "np_style": "vinyl",
    "crossfade": True, "crossfade_s": 6.0,
    "auto_queue": True, "volume": 1.0, "shuffle": False, "repeat": "off", "album_sort": "newest",
}

QUALITY = {
    "raw": ("Original (lossless)", None, None),
    "opus320": ("Opus 320 kbps", "opus", 320),
    "opus192": ("Opus 192 kbps", "opus", 192),
    "opus128": ("Opus 128 kbps", "opus", 128),
    "mp3_320": ("MP3 320 kbps", "mp3", 320),
}


class Settings(dict):
    path = os.path.join(CONF_DIR, "config.json")

    def __init__(self):
        super().__init__(DEFAULTS)
        try:
            with open(self.path) as f:
                self.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self, f, indent=1)
        os.replace(tmp, self.path)

    def set(self, key, value):
        self[key] = value
        self.save()


_metered = [0.0, False]


def metered():
    """True on mobile data. NM's own Metered/PrimaryConnection are unset on this phone (mobile routes
    live in their own table), so look at the devices: no Wi-Fi/ethernet up but a modem up = metered."""
    now = time.time()
    if now - _metered[0] > 15:
        try:
            out = subprocess.run(["nmcli", "-t", "-f", "TYPE,STATE", "dev"], capture_output=True, text=True,
                                 timeout=3).stdout
            up = {line.split(":")[0] for line in out.splitlines() if ":connected" in line}
            _metered[1] = ("gsm" in up or "cdma" in up) and not ({"wifi", "ethernet"} & up)
        except (OSError, subprocess.TimeoutExpired):
            _metered[1] = Gio.NetworkMonitor.get_default().get_network_metered()
        _metered[0] = now
    return _metered[1]


# Navidrome ignores the size for animated WebP artwork and sends the whole original (6-18 MB);
# Gdk.Texture then spent 200-300 ms per cover ON THE UI THREAD (10-17: the app felt stuck
# while the grid filled). Animated playback uses spin-anim's mp4s, so cache a small still.
COVER_MAX_BYTES = 400_000
_shrink_lock = threading.Lock()   # the sandboxed (glycin) decoder fails with parallel loads


def shrink_cover(path, size):
    """Replace an oversized cached cover by a size x size JPEG of its first frame (thread-safe)."""
    try:
        if os.path.getsize(path) <= COVER_MAX_BYTES:
            return
        with _shrink_lock:
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
                pb.savev(path + ".small", "jpeg", ["quality"], ["90"])
            except GLib.Error:      # glycin's sandbox drops some big animated WebPs: ImageMagick
                subprocess.run(["magick", path + "[0]", "-resize", f"{size}x{size}", "-quality", "90",
                                "jpg:" + path + ".small"], capture_output=True, timeout=30, check=True)
            os.replace(path + ".small", path)
    except (GLib.Error, OSError, subprocess.SubprocessError) as e:
        print("cover shrink failed:", path, e)


def idle(fn, *a):
    GLib.idle_add(lambda: (fn(*a), False)[1])


class _Rows(list):
    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


class LockedDB:
    """sqlite3 connection shared by the UI and worker threads, one statement at a time."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()

    def execute(self, sql, args=()):
        with self.lock:
            return _Rows(self.conn.execute(sql, args).fetchall())

    def executemany(self, sql, rows):
        with self.lock:
            self.conn.executemany(sql, rows)

    def executescript(self, sql):
        with self.lock:
            self.conn.executescript(sql)

    def commit(self):
        with self.lock:
            self.conn.commit()


class Store:
    def __init__(self, settings):
        self.s = settings
        self.api = None
        self.online = True
        self.lock = threading.RLock()
        self.db = LockedDB(os.path.join(DATA_DIR, "spin.db"))
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS tracks(id TEXT PRIMARY KEY, data TEXT, file TEXT, size INT, last_used REAL);
        CREATE TABLE IF NOT EXISTS pins(kind TEXT, id TEXT, name TEXT, cover TEXT, added REAL, PRIMARY KEY(kind, id));
        CREATE TABLE IF NOT EXISTS pin_tracks(kind TEXT, id TEXT, track_id TEXT);
        CREATE INDEX IF NOT EXISTS pin_tracks_t ON pin_tracks(track_id);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, data TEXT, updated REAL);
        CREATE TABLE IF NOT EXISTS scrobbles(id TEXT, time_ms INT);
        CREATE TABLE IF NOT EXISTS anim(album_id TEXT PRIMARY KEY, state TEXT, checked REAL);
        """)
        self.listeners = {}          # event -> [callbacks]
        self.progress = {}           # track_id -> fraction while downloading
        self.dl_queue = queue.PriorityQueue()
        self.dl_seq = 0
        self.dl_pending = set()
        self.anim_busy = set()
        self.anim_waiters = {}
        self.star_override = {}
        self.rating_override = {}
        self.cover_busy = {}
        self.cover_sem = threading.Semaphore(4)
        for _ in range(2):
            threading.Thread(target=self._dl_worker, daemon=True).start()
        self.connect_api()

    # --- events ----------------------------------------------------------
    def on(self, event, cb):
        self.listeners.setdefault(event, []).append(cb)

    def emit(self, event, *a):
        for cb in self.listeners.get(event, []):
            idle(cb, *a)

    # --- api ---------------------------------------------------------------
    def connect_api(self):
        s = self.s
        if s["user"] and s["token"]:
            self.api = Subsonic(s["server"], s["user"], s["token"], s["salt"])

    def login(self, server, user, password):
        api = Subsonic.login(server, user, password)
        self.s.update(server=server, user=user, token=api.token, salt=api.salt)
        self.s.save()
        self.api = api

    def logout(self):
        self.s.update(user="", token="", salt="")
        self.s.save()
        self.api = None

    def _set_online(self, on):
        if on != self.online:
            self.online = on
            self.emit("online", on)
            if on:
                threading.Thread(target=self.flush_scrobbles, daemon=True).start()

    def cached(self, key, fn):
        """Run an API call; cache the result, fall back to the cache when offline."""
        try:
            data = fn()
        except Offline:
            self._set_online(False)
            row = self.db.execute("SELECT data FROM meta WHERE key=?", (key,)).fetchone()
            if row is None:
                raise
            return json.loads(row[0])
        self._set_online(True)
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?,?)", (key, json.dumps(data), time.time()))
            self.db.commit()
        return data

    def album(self, album_id):
        return self.cached(f"album:{album_id}", lambda: self.api.album(album_id))

    def playlist(self, pl_id):
        return self.cached(f"playlist:{pl_id}", lambda: self.api.playlist(pl_id))

    def playlists(self):
        return self.cached("playlists", self.api.playlists)

    def artists(self):
        return self.cached("artists", self.api.artists)

    def artist(self, artist_id):
        return self.cached(f"artist:{artist_id}", lambda: self.api.artist(artist_id))

    # --- whole-library pools (shuffle all, smart playlists) -------------------
    POOL_BATCH = 500   # queue size per batch; the player refills from the pool when it runs low

    def all_songs(self):
        """Every song in the library (search3 with an empty query, 500 per page); 10 min cache."""
        if not self.api:
            raise RuntimeError("offline")
        cache = getattr(self, "_all_songs", None)
        if cache and time.time() - cache[0] < 600:
            return cache[1]
        out, off = [], 0
        while True:
            page = self.api.call("search3", timeout=60, query="", songCount=500, songOffset=off,
                                 artistCount=0, albumCount=0).get("searchResult3", {}).get("song", [])
            out += page
            off += 500
            if len(page) < 500:
                break
        self._all_songs = (time.time(), out)
        return out

    def song_pool(self, kind):
        """kind "all" | "unheard" -> (first batch, refill callable, pool size). Runs in a thread."""
        songs = self.all_songs()
        if kind == "unheard":
            songs = [t for t in songs if not t.get("playCount")]
        total = len(songs)
        pool = list(songs)
        random.shuffle(pool)

        def take():
            nonlocal pool
            if not pool and kind == "all":       # went through everything: start a new round
                pool = list(songs)
                random.shuffle(pool)
            batch, pool = pool[:self.POOL_BATCH], pool[self.POOL_BATCH:]
            return batch
        return take(), take, total

    UNHEARD_FILE = os.path.join(DATA_DIR, "unheard.json")

    def unheard_count(self):
        """Counting means listing the whole library (~8 s, competes with the UI): the result is
        kept in unheard.json and shown at once next time (unheard_cached)."""
        n = sum(1 for t in self.all_songs() if not t.get("playCount"))
        try:
            with open(self.UNHEARD_FILE + ".tmp", "w") as f:
                json.dump({"n": n, "t": time.time()}, f)
            os.replace(self.UNHEARD_FILE + ".tmp", self.UNHEARD_FILE)
        except OSError:
            pass
        return n

    def unheard_cached(self):
        """(count, age in s) from the last count, or (None, inf)."""
        try:
            with open(self.UNHEARD_FILE) as f:
                d = json.load(f)
            return d["n"], time.time() - d["t"]
        except (OSError, ValueError, KeyError):
            return None, float("inf")

    def album_list(self, kind, offset):
        if kind == "offline":
            return self.offline_albums() if offset == 0 else []
        try:
            return self.cached(f"albums:{kind}:{offset}", lambda: self.api.album_list(kind, 60, offset))
        except Offline:
            return self.offline_albums() if offset == 0 else []

    def starred(self):
        return self.cached("starred", self.api.starred)

    def set_star(self, kind, item, on):
        """Star/unstar on the server; update the dict in place so the UI reflects it."""
        self.star_override[item["id"]] = on
        if on:
            item["starred"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        else:
            item.pop("starred", None)
        def run():
            try:
                self.api.star(kind, item["id"], on)
            except ApiError as e:
                print("star failed", e)
        threading.Thread(target=run, daemon=True).start()
        if kind == "song":
            self._fav_offline(item, on)
        self.emit("starred", kind, item["id"], on)

    # Favourite songs stay offline (10-12): they are pin_tracks of a hidden pin ("fav",
    # "songs"), so evict() never removes them; they download like pins (prio 5, Wi-Fi only
    # unless cache_on_mobile). No row in pins, so they don't show as a pinned album.
    FAV_PIN = ("fav", "songs")

    def _fav_offline(self, track, on):
        kind, pid = self.FAV_PIN
        with self.lock:
            self.db.execute("DELETE FROM pin_tracks WHERE kind=? AND id=? AND track_id=?", (kind, pid, track["id"]))
            if on:
                self.db.execute("INSERT INTO pin_tracks VALUES(?,?,?)", (kind, pid, track["id"]))
            self.db.commit()
        if on:
            self.enqueue(track, 5)
        else:
            threading.Thread(target=self.evict, daemon=True).start()  # back to the normal cache

    def sync_favs(self):
        """Make the favourite pin match the server's starred songs (also starred elsewhere)."""
        songs = self.starred().get("song", [])
        with self.lock:
            self._set_pin_tracks(*self.FAV_PIN, songs)
        self.evict()

    def rating(self, item):
        return self.rating_override.get(item["id"], int(item.get("userRating") or 0))

    def set_rating(self, item, n):
        """1-5 stars on the server (0 clears); update the dict in place like set_star."""
        self.rating_override[item["id"]] = n
        item["userRating"] = n
        def run():
            try:
                self.api.set_rating(item["id"], n)
            except ApiError as e:
                print("rating failed", e)
        threading.Thread(target=run, daemon=True).start()
        self.emit("rated", item["id"], n)

    def is_starred(self, item):
        return self.star_override.get(item["id"], bool(item.get("starred")))

    def offline_albums(self):
        """Albums we have at least one local track for (pinned or cached)."""
        rows = self.db.execute("SELECT data FROM tracks").fetchall()
        seen, out = set(), []
        for (data,) in rows:
            t = json.loads(data)
            aid = t.get("albumId")
            if aid and aid not in seen:
                seen.add(aid)
                out.append({"id": aid, "name": t.get("album", "?"), "artist": t.get("artist", ""),
                            "coverArt": t.get("coverArt"), "year": t.get("year")})
        return sorted(out, key=lambda a: a["name"].lower())

    # --- local files -------------------------------------------------------
    def local_path(self, track_id, touch=True):
        row = self.db.execute("SELECT file FROM tracks WHERE id=?", (track_id,)).fetchone()
        if row and os.path.exists(row[0]):
            if touch:
                with self.lock:
                    self.db.execute("UPDATE tracks SET last_used=? WHERE id=?", (time.time(), track_id))
                    self.db.commit()
            return row[0]
        return None

    def is_local(self, track_id):
        return self.local_path(track_id, touch=False) is not None

    def is_pinned_track(self, track_id):
        return self.db.execute("SELECT 1 FROM pin_tracks WHERE track_id=?", (track_id,)).fetchone() is not None

    def cache_usage(self):
        auto = self.db.execute("SELECT COALESCE(SUM(size),0), COUNT(*) FROM tracks WHERE id NOT IN "
                               "(SELECT track_id FROM pin_tracks)").fetchone()
        pinned = self.db.execute("SELECT COALESCE(SUM(size),0), COUNT(*) FROM tracks WHERE id IN "
                                 "(SELECT track_id FROM pin_tracks)").fetchone()
        return auto, pinned

    def clear_auto_cache(self):
        with self.lock:
            rows = self.db.execute("SELECT id, file FROM tracks WHERE id NOT IN "
                                   "(SELECT track_id FROM pin_tracks)").fetchall()
            for tid, f in rows:
                try:
                    os.remove(f)
                except OSError:
                    pass
                self.db.execute("DELETE FROM tracks WHERE id=?", (tid,))
            self.db.commit()
        self.emit("cache-changed")

    def evict(self):
        limit = self.s["cache_limit_gb"] * 1024 ** 3
        with self.lock:
            rows = self.db.execute("SELECT id, file, size FROM tracks WHERE id NOT IN "
                                   "(SELECT track_id FROM pin_tracks) ORDER BY last_used DESC").fetchall()
            total = 0
            for tid, f, size in rows:
                total += size or 0
                if total > limit:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                    self.db.execute("DELETE FROM tracks WHERE id=?", (tid,))
            self.db.commit()

    # --- downloads -----------------------------------------------------------
    def enqueue(self, track, prio):
        """prio 0 = playing now, 1 = prefetch, 5 = pinned."""
        tid = track["id"]
        with self.lock:
            if tid in self.dl_pending or self.is_local(tid):
                return
            self.dl_pending.add(tid)
            self.dl_seq += 1
            self.dl_queue.put((prio, self.dl_seq, track))

    def _may_download(self, prio):
        if not self.api:
            return False
        if metered() and not self.s["cache_on_mobile"]:
            return prio == 0 and self.s["cache_on_mobile"]
        return True

    def _dl_worker(self):
        while True:
            prio, seq, track = self.dl_queue.get()
            tid = track["id"]
            if self.is_local(tid):
                self.dl_pending.discard(tid)
                continue
            if not self._may_download(prio):
                time.sleep(20)
                self.dl_queue.put((prio, seq, track))
                continue
            try:
                self._download(track)
                self._set_online(True)
            except Exception as e:  # noqa: BLE001 - retry later on any failure
                print("download failed", tid, e)
                self.progress.pop(tid, None)
                time.sleep(10)
                self.dl_queue.put((prio, seq, track))
                continue
            self.dl_pending.discard(tid)
            if not self.is_pinned_track(tid):
                self.evict()
            self.emit("downloaded", tid)

    def _download(self, track):
        tid = track["id"]
        url = self.api.stream_url(tid, "raw")
        suffix = track.get("suffix") or "audio"
        path = os.path.join(AUDIO_DIR, f"{tid}.{suffix}")
        part = path + ".part"
        total = track.get("size") or 0
        got = 0
        with requests.get(url, stream=True, timeout=20) as r:
            r.raise_for_status()
            if r.headers.get("content-type", "").startswith(("application/json", "text/xml")):
                raise ApiError("server refused stream")
            with open(part, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        self.progress[tid] = got / total
        os.replace(part, path)
        self.progress.pop(tid, None)
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO tracks VALUES(?,?,?,?,?)",
                            (tid, json.dumps(track), path, got, time.time()))
            self.db.commit()

    def prefetch(self, tracks):
        if not self.s["auto_cache"]:
            return
        for t in tracks[: self.s["prefetch"]]:
            self.enqueue(t, 1)
            if t.get("albumId"):
                self.want_anim(t["albumId"])

    def played(self, track):
        if self.s["auto_cache"]:
            self.enqueue(track, 0)
        if self.s["animated_covers"] and track.get("albumId"):
            self.want_anim(track["albumId"])

    # --- pins (offline albums / playlists) ---------------------------------
    def pins(self):
        return self.db.execute("SELECT kind, id, name, cover FROM pins ORDER BY added DESC").fetchall()

    def is_pinned(self, kind, pid):
        return self.db.execute("SELECT 1 FROM pins WHERE kind=? AND id=?", (kind, pid)).fetchone() is not None

    def pin(self, kind, item, tracks):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO pins VALUES(?,?,?,?,?)",
                            (kind, item["id"], item.get("name", "?"), item.get("coverArt"), time.time()))
            self._set_pin_tracks(kind, item["id"], tracks)
        if kind == "album" and self.s["animated_covers"]:
            self.want_anim(item["id"])
        self.emit("pins-changed")

    def _set_pin_tracks(self, kind, pid, tracks):
        self.db.execute("DELETE FROM pin_tracks WHERE kind=? AND id=?", (kind, pid))
        self.db.executemany("INSERT INTO pin_tracks VALUES(?,?,?)", [(kind, pid, t["id"]) for t in tracks])
        self.db.commit()
        for t in tracks:
            self.enqueue(t, 5)

    def unpin(self, kind, pid):
        with self.lock:
            self.db.execute("DELETE FROM pins WHERE kind=? AND id=?", (kind, pid))
            self.db.execute("DELETE FROM pin_tracks WHERE kind=? AND id=?", (kind, pid))
            self.db.commit()
        self.evict()
        self.emit("pins-changed")

    def pin_progress(self, kind, pid):
        total = self.db.execute("SELECT COUNT(*) FROM pin_tracks WHERE kind=? AND id=?", (kind, pid)).fetchone()[0]
        done = self.db.execute("SELECT COUNT(*) FROM pin_tracks p JOIN tracks t ON t.id=p.track_id "
                               "WHERE p.kind=? AND p.id=?", (kind, pid)).fetchone()[0]
        return done, total

    def sync_pins(self):
        """Refresh pinned playlists/albums (playlists change on the server)."""
        for kind, pid, _name, _cover in self.pins():
            try:
                if kind == "playlist":
                    pl = self.playlist(pid)
                    tracks = pl.get("entry", [])
                else:
                    tracks = self.album(pid).get("song", [])
            except ApiError:
                continue
            with self.lock:
                self._set_pin_tracks(kind, pid, tracks)
        try:
            self.sync_favs()
        except (ApiError, Offline):
            pass
        self.emit("pins-changed")

    # --- scrobbling ------------------------------------------------------------
    def scrobble(self, track_id, submission):
        def run():
            t = int(time.time() * 1000)
            try:
                self.api.scrobble(track_id, submission, t if submission else None)
            except ApiError:
                if submission:
                    with self.lock:
                        self.db.execute("INSERT INTO scrobbles VALUES(?,?)", (track_id, t))
                        self.db.commit()
        if self.api:
            threading.Thread(target=run, daemon=True).start()

    def flush_scrobbles(self):
        rows = self.db.execute("SELECT rowid, id, time_ms FROM scrobbles").fetchall()
        for rowid, tid, t in rows:
            try:
                self.api.scrobble(tid, True, t)
            except ApiError:
                return
            with self.lock:
                self.db.execute("DELETE FROM scrobbles WHERE rowid=?", (rowid,))
                self.db.commit()

    # --- covers ----------------------------------------------------------------
    def cover(self, cover_id, size, cb):
        """Call cb(path) on the main loop once the cover is on disk (None if unavailable)."""
        if not cover_id:
            cb(None)
            return
        path = os.path.join(COVER_DIR, f"{cover_id.replace('/', '_')}_{size}")
        key = (cover_id, size)
        if os.path.exists(path):
            if os.path.getsize(path) <= COVER_MAX_BYTES:
                cb(path)
                return
            if key in self.cover_busy:          # an oversized one from before: shrink it first
                self.cover_busy[key].append(cb)
                return
            self.cover_busy[key] = [cb]

            def shrink():
                shrink_cover(path, size)
                idle(lambda: [c(path) for c in self.cover_busy.pop(key, [])])
            threading.Thread(target=shrink, daemon=True).start()
            return
        if key in self.cover_busy:
            self.cover_busy[key].append(cb)
            return
        self.cover_busy[key] = [cb]

        def run():
            ok = None
            with self.cover_sem:
                try:
                    r = self.api.http.get(self.api.cover_url(cover_id, size), timeout=15)
                    if r.ok and r.headers.get("content-type", "").startswith("image"):
                        with open(path + ".part", "wb") as f:
                            f.write(r.content)
                        os.replace(path + ".part", path)
                        shrink_cover(path, size)
                        ok = path
                except (requests.RequestException, OSError, AttributeError):
                    pass
            def done():
                for c in self.cover_busy.pop(key, []):
                    c(ok)
            idle(done)
        threading.Thread(target=run, daemon=True).start()

    # --- animated covers (converted to small mp4 by spin-anim on the PC) -----
    def anim_path(self, album_id, thumb=False):
        p = os.path.join(ANIM_DIR, f"{album_id}{'.thumb' if thumb else ''}.mp4")
        return p if os.path.exists(p) else None

    def anim_known_none(self, album_id):
        row = self.db.execute("SELECT state, checked FROM anim WHERE album_id=?", (album_id,)).fetchone()
        return bool(row and row[0] == "none" and time.time() - row[1] < 3 * 86400)

    def anim_lookup(self, album_id, cb, thumb=False):
        """cb(mp4 path or None) on the main loop once we know whether an animated cover exists.
        thumb=True: the small 240 px / 15 fps version for grid tiles."""
        if not album_id or not self.s["animated_covers"]:
            return cb(None)
        path = self.anim_path(album_id, thumb)
        if path:
            return cb(path)
        if self.anim_known_none(album_id) or (metered() and not self.s["anim_on_mobile"]):
            return cb(None)
        self.anim_waiters.setdefault((album_id, thumb), []).append(cb)
        self.want_anim(album_id, thumb)

    def want_anim(self, album_id, thumb=False):
        key = (album_id, thumb)
        if not self.s["animated_covers"] or self.anim_path(album_id, thumb) or key in self.anim_busy:
            return
        if self.anim_known_none(album_id) or (metered() and not self.s["anim_on_mobile"]):
            return
        self.anim_busy.add(key)
        threading.Thread(target=self._fetch_anim, args=(album_id, thumb), daemon=True).start()

    def _fetch_anim(self, album_id, thumb=False):
        url = f"{self.s['anim_server'].rstrip('/')}/{'thumb' if thumb else 'anim'}/{album_id}.mp4"
        state = None
        for _ in range(30):
            try:
                r = requests.get(url, timeout=20)
            except requests.RequestException:
                break
            if r.status_code == 200:
                path = os.path.join(ANIM_DIR, f"{album_id}{'.thumb' if thumb else ''}.mp4")
                with open(path + ".part", "wb") as f:
                    f.write(r.content)
                os.replace(path + ".part", path)
                state = "ok"
            elif r.status_code == 404:
                state = "none"
            elif r.status_code == 202:      # the PC is converting it
                time.sleep(2)
                continue
            break
        self.anim_busy.discard((album_id, thumb))
        if state:
            with self.lock:
                self.db.execute("INSERT OR REPLACE INTO anim VALUES(?,?,?)", (album_id, state, time.time()))
                self.db.commit()
        path = self.anim_path(album_id, thumb)

        def done():
            for cb in self.anim_waiters.pop((album_id, thumb), []):
                cb(path)
        idle(done)
        if state == "ok" and not thumb:
            self.emit("anim", album_id)
