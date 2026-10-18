"""GPS for notarealMaps through GeoClue (modem GPS is enabled in /etc/geoclue/geoclue.conf).

Location(on_fix) calls on_fix(dict(lat, lng, acc, speed, heading)) on the GTK main loop.
Simulator(route_pts, on_fix, speed) replays a route for testing / demo.
"""
import time

import gi
gi.require_version("Geoclue", "2.0")
from gi.repository import GLib, Geoclue  # noqa: E402

import gmaps  # noqa: E402

DESKTOP_ID = "dev.notareal.Maps"


class Location:
    def __init__(self, on_fix, on_error=None):
        self.on_fix, self.on_error = on_fix, on_error
        self.simple = None
        self.last = None
        Geoclue.Simple.new_with_thresholds(DESKTOP_ID, Geoclue.AccuracyLevel.EXACT, 0, 0, None, self._ready)

    def _ready(self, _src, res):
        try:
            self.simple = Geoclue.Simple.new_with_thresholds_finish(res)
        except GLib.Error as e:
            if self.on_error:
                self.on_error(e.message)
            return
        self.simple.connect("notify::location", lambda *_: self._emit())
        self._emit()

    def _emit(self):
        loc = self.simple.get_location() if self.simple else None
        if not loc:
            return
        fix = {"lat": loc.props.latitude, "lng": loc.props.longitude, "acc": loc.props.accuracy,
               "speed": loc.props.speed if loc.props.speed >= 0 else None,
               "heading": loc.props.heading if loc.props.heading >= 0 else None, "t": time.time()}
        # GeoClue's heading is unreliable when slow: derive it from movement instead
        if self.last and gmaps.dist((self.last["lat"], self.last["lng"]), (fix["lat"], fix["lng"])) > 4:
            fix["heading"] = gmaps.bearing((self.last["lat"], self.last["lng"]), (fix["lat"], fix["lng"]))
        elif fix["heading"] is None and self.last:
            fix["heading"] = self.last.get("heading")
        self.last = fix
        self.on_fix(fix)

    def stop(self):
        self.simple = None


class Simulator:
    """Drive along points at `speed` m/s, one fix per second (with a little GPS jitter)."""

    def __init__(self, pts, on_fix, speed=14.0):
        self.pts, self.on_fix, self.speed = pts, on_fix, speed
        self.i, self.carry = 0, 0.0
        self.pos = pts[0]
        self.src = GLib.timeout_add(1000, self._tick)
        self.last = None

    def _tick(self):
        if not self.src:
            return False
        left = self.speed
        while left > 0 and self.i < len(self.pts) - 1:
            a, b = self.pos, self.pts[self.i + 1]
            d = gmaps.dist(a, b)
            if d <= left:
                self.pos, left = b, left - d
                self.i += 1
            else:
                f = left / d
                self.pos = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
                left = 0
        hd = gmaps.bearing(self.pts[max(0, self.i)], self.pts[min(len(self.pts) - 1, self.i + 1)])
        self.last = {"lat": self.pos[0], "lng": self.pos[1], "acc": 6.0, "speed": self.speed,
                     "heading": hd, "t": time.time()}
        self.on_fix(self.last)
        if self.i >= len(self.pts) - 1:
            self.src = None
            return False
        return True

    def stop(self):
        if self.src:
            GLib.source_remove(self.src)
        self.src = None
