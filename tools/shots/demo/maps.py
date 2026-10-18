"""notreal.maps demo: a fake Google backend (OpenStreetMap tiles), a simulated GPS fix and a
scripted scene. MAPS_SCENE = place | routes | nav. Voice output is muted."""
import os

os.environ["NOTAREAL_MAPS_SIM"] = "1"
os.environ["NOTAREAL_MAPS_KEY"] = "demo"

import gmaps  # noqa: E402

gmaps.CONFIG = os.path.expanduser("~/.config/notrealmaps-demo.json")
A, B, C = (59.3300, 18.0600), (59.3408, 18.0600), (59.3408, 18.0705)


def line(a, b, n):
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]


PTS = line(A, B, 30) + line(B, C, 15)[1:]
PLACE = {"id": "p1", "name": "Kaffebar Lumen", "address": "Exempelgatan 1, Stockholm", "lat": C[0], "lng": C[1],
         "rating": 4.6, "ratings": 1203, "type": "Coffee shop", "open": True, "next_close": None, "next_open": None,
         "utc_off": 120, "hours": ["Monday: 7–18"] * 7, "phone": "", "web": "https://example.com",
         "summary": "Bright corner café with house-roasted beans.", "photos": [],
         "reviews": [{"who": "Anna", "stars": 5, "when": "a week ago", "text": "Best cardamom bun in town."}]}


class Fake(gmaps.Google):
    def tile_session(self, traffic=False):
        return "s"

    def tile_url(self, s):
        return "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

    def autocomplete(self, text, near=None):
        return [{"id": "p1", "name": PLACE["name"], "sub": PLACE["address"], "meters": 850}]

    def search(self, q, near=None):
        return [dict(PLACE)]

    def details(self, pid):
        return dict(PLACE)

    def routes(self, origin, dest, mode="DRIVE", heading=None, alternatives=True):
        d1, d2 = gmaps.dist(A, B), gmaps.dist(B, C)
        r = {"pts": PTS, "mode": mode, "m": d1 + d2, "s": 240, "static_s": 180, "via": "Sveavägen", "labels": [],
             "warnings": [], "traffic": [(0, 10, "NORMAL"), (10, 25, "SLOW"), (25, 45, "TRAFFIC_JAM")],
             "steps": [{"m": d1, "s": 120, "maneuver": "DEPART", "text": "Head north on Sveavägen", "mode": mode,
                        "transit": None, "pts": []},
                       {"m": d2, "s": 60, "maneuver": "TURN_RIGHT", "text": "Turn right onto Odengatan", "mode": mode,
                        "transit": None, "pts": []}]}
        alt = dict(r, pts=line(A, (A[0], C[1]), 15) + line((A[0], C[1]), C, 30)[1:], s=300, static_s=290,
                   traffic=[], via="Birger Jarlsgatan")
        return [r, alt]


gmaps.Google = Fake

import voice  # noqa: E402

voice.Voice.say = lambda self, t: None
voice.Voice.prepare = lambda self, t: None

from gi.repository import GLib, Gtk  # noqa: E402

SCENE = os.environ.get("MAPS_SCENE", "place")


def step(n=[0]):
    ws = [w for w in Gtk.Window.list_toplevels() if w.get_visible() and hasattr(w, "_on_fix")]
    if not ws:
        return True
    w = ws[0]
    n[0] += 1
    t = n[0]
    if t == 2:
        w._on_fix({"lat": A[0], "lng": A[1], "acc": 8, "speed": 0, "heading": None, "t": 0})
    if t == 6:
        w._pick({"id": "p1", "name": PLACE["name"]})
    if t == 14 and SCENE in ("routes", "nav"):
        w.directions(w.place)
    if t == 22 and SCENE == "nav":
        w.start_nav(0)
    return t < 40


GLib.timeout_add(250, step)
