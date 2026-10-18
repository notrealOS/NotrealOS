"""notreal.player demo: a tiny fake Subsonic server with made-up albums and generated cover art.

No real library, no real covers: every cover is drawn here from the album's colours.
"""
import io
import json
import math
import os
import random
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

ALBUMS = [  # title, artist, year, colours (top, bottom), shape
    ("Night Drive", "Neon Harbor", 2024, ((255, 92, 160), (40, 20, 90)), "sun"),
    ("Paper Moons", "Lumen Choir", 2023, ((250, 240, 215), (120, 140, 180)), "rings"),
    ("Low Tide", "Saltwater Kids", 2022, ((60, 170, 190), (10, 40, 70)), "waves"),
    ("Orbit", "Kosmo", 2025, ((240, 110, 30), (30, 30, 35)), "planet"),
    ("Analog Heart", "Tape Club", 2021, ((230, 200, 90), (150, 60, 40)), "stripes"),
    ("Glass Garden", "Mira Holm", 2024, ((150, 230, 180), (20, 80, 70)), "rings"),
    ("Static", "The Receivers", 2020, ((200, 200, 205), (60, 60, 70)), "stripes"),
    ("Afterglow", "Neon Harbor", 2022, ((255, 170, 90), (120, 30, 90)), "sun"),
    ("Deep Field", "Kosmo", 2023, ((70, 90, 200), (5, 5, 25)), "planet"),
    ("Slow Rivers", "Lumen Choir", 2021, ((180, 210, 240), (40, 70, 110)), "waves"),
    ("Pocket Symphony", "Mira Holm", 2025, ((245, 225, 235), (180, 80, 120)), "rings"),
    ("Loops", "Tape Club", 2024, ((120, 220, 120), (20, 60, 30)), "stripes"),
]
TRACKS = ["Intro", "Signals", "Golden Hour", "Halfway", "Coastline", "Satellites", "Undertow", "Home"]


def cover_png(i, size=300):
    title, artist, year, (top, bot), shape = ALBUMS[i]
    rnd = random.Random(i)
    buf = bytearray(size * size * 3)
    for y in range(size):
        t = y / size
        base = [int(top[c] + (bot[c] - top[c]) * t) for c in range(3)]
        for x in range(size):
            r, g, b = base
            u, v = x / size - 0.5, y / size - 0.5
            d = math.hypot(u, v)
            if shape == "sun" and d < 0.22 and v < 0.12:
                r, g, b = 255, 230, 120
            elif shape == "sun" and v > 0.12 and int(y / 9) % 2:
                r, g, b = int(r * 0.6), int(g * 0.6), int(b * 0.8)
            elif shape == "rings" and int(d * 40) % 3 == 0 and d < 0.42:
                r, g, b = [int(c * 0.75) for c in (r, g, b)]
            elif shape == "waves" and y > size * (0.55 + 0.04 * math.sin(x / 13 + rnd.random())):
                r, g, b = [int(c * 0.7) for c in (r, g, b)]
            elif shape == "planet":
                if d < 0.2:
                    k = 1.2 - d * 2.5
                    r, g, b = int(min(255, top[0] * k)), int(min(255, top[1] * k)), int(min(255, top[2] * k))
                elif abs(v - u * 0.35) < 0.012 and 0.15 < d < 0.36:
                    r, g, b = 240, 240, 240
                elif (x * 7 + y * 13) % 211 == 0:
                    r, g, b = 255, 255, 255
            elif shape == "stripes" and int((x + y) / 22) % 2:
                r, g, b = [min(255, int(c * 1.15)) for c in (r, g, b)]
            o = (y * size + x) * 3
            buf[o:o + 3] = bytes((r, g, b))
    pb = GdkPixbuf.Pixbuf.new_from_data(bytes(buf), GdkPixbuf.Colorspace.RGB, False, 8, size, size, size * 3)
    ok, data = pb.save_to_bufferv("png", [], [])
    return data


COVERS = {}


def album_json(i, songs=False):
    title, artist, year, _c, _s = ALBUMS[i]
    a = {"id": "al%d" % i, "name": title, "artist": artist, "artistId": "ar-" + artist, "year": year,
         "coverArt": "al%d" % i, "songCount": len(TRACKS), "duration": len(TRACKS) * 214, "created": "2026-10-%02d" % (i + 1)}
    if songs:
        a["song"] = [{"id": "s%d_%d" % (i, n), "title": t, "album": title, "albumId": a["id"], "artist": artist,
                      "track": n + 1, "duration": 180 + 13 * n, "coverArt": a["id"], "suffix": "flac",
                      "bitRate": 1411, "contentType": "audio/flac", "year": year} for n, t in enumerate(TRACKS)]
    return a


def silence():
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100); w.writeframes(b"\0" * 44100 * 4 * 2)
    return b.getvalue()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        ep = u.path.rsplit("/", 1)[-1].removesuffix(".view")
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if ep == "getCoverArt":
            i = int(q.get("id", "al0").lstrip("al") or 0)
            if i not in COVERS:
                COVERS[i] = cover_png(i)
            return self.send(COVERS[i], "image/png")
        if ep == "stream":
            return self.send(silence(), "audio/wav")
        r = {}
        if ep == "getAlbumList2":
            r = {"albumList2": {"album": [album_json(i) for i in range(len(ALBUMS))] if q.get("offset", "0") == "0" else []}}
        elif ep == "getAlbum":
            r = {"album": album_json(int(q["id"][2:]), songs=True)}
        elif ep == "getArtists":
            names = sorted({a[1] for a in ALBUMS})
            r = {"artists": {"index": [{"name": "#", "artist": [{"id": "ar-" + n, "name": n, "albumCount": 3} for n in names]}]}}
        elif ep == "getPlaylists":
            r = {"playlists": {"playlist": [{"id": "pl1", "name": "Morning", "songCount": 24, "coverArt": "al1"},
                                            {"id": "pl2", "name": "Workout", "songCount": 40, "coverArt": "al3"}]}}
        elif ep == "getStarred2":
            r = {"starred2": {"song": album_json(0, True)["song"][:3], "album": [album_json(3)]}}
        elif ep in ("search3",):
            r = {"searchResult3": {"song": album_json(0, True)["song"], "album": [album_json(i) for i in range(4)]}}
        body = {"subsonic-response": dict({"status": "ok", "version": "1.16.1"}, **r)}
        self.send(json.dumps(body).encode())


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
for i in range(len(ALBUMS)):          # draw the covers before the app asks
    COVERS[i] = cover_png(i)
os.makedirs(os.path.expanduser("~/.config/spin"), exist_ok=True)
with open(os.path.expanduser("~/.config/spin/config.json"), "w") as f:
    json.dump({"server": "http://127.0.0.1:%d" % srv.server_address[1], "user": "demo", "token": "demo",
               "salt": "demo", "anim_server": "http://127.0.0.1:9", "output": "pulse"}, f)

