"""Turn-by-turn engine: snap GPS fixes to the route, track progress, decide what to say.

Plain Python (no GTK) so it can be driven by a simulator in tests.
A Google step's navigationInstruction is the manoeuvre at the START of that step, so while
we are on step k the next thing to do is step k+1's instruction, at the end of step k.
"""
import math
import time

from gmaps import bearing, dist

OFF_ROUTE_M = 40
OFF_ROUTE_FIXES = 3
REROUTE_GAP_S = 12
ARRIVE_M = 25


def _project(p, a, b):
    """Closest point to p on segment a-b, in a local flat frame. Returns (t, metres away)."""
    k = math.cos(math.radians(p[0]))
    ax, ay = a[1] * k, a[0]
    bx, by = b[1] * k, b[0]
    px, py = p[1] * k, p[0]
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    q = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    return t, dist(p, q), q


def spoken_distance(m):
    if m >= 950:
        km = round(m / 1000, 1)
        return ("%g kilometers" % km).replace(".0 ", " ") if km != 1 else "1 kilometer"
    if m >= 300:
        return "%d meters" % (round(m / 100) * 100)
    return "%d meters" % max(10, round(m / 10) * 10)


def short_distance(m):
    if m >= 1000:
        return "%.1f km" % (m / 1000)
    if m >= 300:
        return "%d m" % (round(m / 50) * 50)
    return "%d m" % max(0, round(m / 10) * 10)


class Navigator:
    def __init__(self, route):
        self.set_route(route)

    def set_route(self, route):
        self.route = route
        self.pts = route["pts"]
        self.cum = [0.0]
        for a, b in zip(self.pts, self.pts[1:]):
            self.cum.append(self.cum[-1] + dist(a, b))
        total = self.cum[-1] or 1.0
        steps = route["steps"]
        meters = sum(s["m"] for s in steps) or total
        scale = total / meters
        self.step_end, acc = [], 0.0
        for s in steps:
            acc += s["m"] * scale
            self.step_end.append(acc)
        if self.step_end:
            self.step_end[-1] = total
        self.traffic_k = (route["s"] / route["static_s"]) if route.get("static_s") else 1.0
        self.idx = 0
        self.progress = 0.0
        self.off = 0
        self.said = set()
        self.started = time.time()
        self.last_reroute = time.time()
        self.arrived = False
        self.snapped = self.pts[0] if self.pts else None

    @property
    def total(self):
        return self.cum[-1]

    def update(self, lat, lng, speed=None, accuracy=20.0):
        """Feed one fix. Returns a state dict with optional 'say' and 'event' ('reroute'/'arrived')."""
        p = (lat, lng)
        lo = max(0, self.idx - 3)
        hi = min(len(self.pts) - 1, self.idx + 120)
        best = None
        for i in range(lo, hi):
            t, d, q = _project(p, self.pts[i], self.pts[i + 1])
            along = self.cum[i] + (self.cum[i + 1] - self.cum[i]) * t
            # prefer forward progress when two parts of the route are equally close
            score = d + (0 if along >= self.progress - 30 else 60)
            if best is None or score < best[0]:
                best = (score, d, i, along, q)
        st = {"say": None, "event": None}
        if best is None or self.arrived:
            return st
        _s, lateral, i, along, q = best
        limit = max(OFF_ROUTE_M, (accuracy or 0) * 1.5)
        if lateral > limit:
            self.off += 1
        else:
            self.off = 0
            self.idx = i
            self.progress = max(self.progress, along) if along > self.progress - 50 else along
            self.snapped = q
        st["lateral"] = lateral
        if self.off >= OFF_ROUTE_FIXES and time.time() - self.last_reroute > REROUTE_GAP_S:
            self.last_reroute = time.time()
            self.off = 0
            st["event"] = "reroute"

        remaining = max(0.0, self.total - self.progress)
        k = next((n for n, e in enumerate(self.step_end) if e > self.progress), len(self.step_end) - 1)
        steps = self.route["steps"]
        to_turn = max(0.0, self.step_end[k] - self.progress) if self.step_end else remaining
        nxt = steps[k + 1] if k + 1 < len(steps) else None
        st.update(step=k, next=nxt, to_turn=to_turn, remaining=remaining,
                  then=steps[k + 2] if k + 2 < len(steps) else None,
                  eta_s=self._remaining_s(k, to_turn),
                  heading=bearing(self.pts[i], self.pts[i + 1]), snapped=self.snapped)

        if remaining < ARRIVE_M and not self.arrived:
            self.arrived = True
            st["event"] = "arrived"
            st["say"] = "You have arrived at your destination"
            return st

        # what to say about the next manoeuvre
        v = speed if speed and speed > 0 else (12.0 if self.route["mode"] == "DRIVE" else 1.4)
        if self.route["mode"] in ("WALK", "BICYCLE"):
            marks = [("far", 160 if self.route["mode"] == "WALK" else 400), ("now", 20 if v < 3 else 35)]
        else:
            marks = [("far", 1600 if v > 22 else 750), ("near", 300 if v > 16 else 150),
                     ("now", max(35, v * 3.5))]
        text = (nxt["text"] if nxt else "") or ("Arrive at your destination" if not nxt else "")
        key_base = "end" if not nxt else k + 1
        for name, at in marks:
            key = (key_base, name)
            if key in self.said:
                continue
            if to_turn <= at:
                # mark every bigger threshold as done too (no stale "in 700 m" right after a turn)
                for n2, at2 in marks:
                    if at2 >= at:
                        self.said.add((key_base, n2))
                if name == "now" or to_turn < 60:
                    st["say"] = text
                else:
                    st["say"] = "In %s, %s" % (spoken_distance(to_turn), text[:1].lower() + text[1:])
                break
        return st

    def _remaining_s(self, k, to_turn):
        steps = self.route["steps"]
        if not steps:
            return 0
        cur = steps[k]
        part = cur["s"] * (to_turn / cur["m"]) if cur["m"] else 0
        rest = sum(s["s"] for s in steps[k + 1:])
        return (part + rest) * self.traffic_k


def maneuver_icon(m):
    """Routes API maneuver -> arrow glyph for the banner."""
    return {
        "TURN_LEFT": "⬅", "TURN_RIGHT": "➡", "TURN_SLIGHT_LEFT": "⬉", "TURN_SLIGHT_RIGHT": "⬈",
        "TURN_SHARP_LEFT": "↙", "TURN_SHARP_RIGHT": "↘", "UTURN_LEFT": "↶", "UTURN_RIGHT": "↷",
        "STRAIGHT": "⬆", "RAMP_LEFT": "⬉", "RAMP_RIGHT": "⬈", "MERGE": "⇡", "FORK_LEFT": "⬉",
        "FORK_RIGHT": "⬈", "FERRY": "⛴", "FERRY_TRAIN": "⛴", "ROUNDABOUT_LEFT": "↺",
        "ROUNDABOUT_RIGHT": "↻", "DEPART": "⬆", "NAME_CHANGE": "⬆",
    }.get(m or "", "⬆")
