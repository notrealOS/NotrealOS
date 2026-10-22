"""Google Maps Platform for notarealMaps: map tiles session, Places (New), Routes.

All calls block; the app runs them on worker threads. The key lives in the keyring.
APIs the key needs: Map Tiles API, Places API (New), Routes API.
"""
import json
import math
import os
import re
import threading
import time
import uuid

import requests

import gi
gi.require_version("Secret", "1")
from gi.repository import Secret  # noqa: E402

CONF_DIR = os.path.expanduser("~/.config/notarealmaps")
CACHE_DIR = os.path.expanduser("~/.cache/notarealmaps")
CONFIG = os.path.join(CONF_DIR, "config.json")
SCHEMA = Secret.Schema.new("dev.notareal.Maps", Secret.SchemaFlags.NONE,
                           {"service": Secret.SchemaAttributeType.STRING})

TILES = "https://tile.googleapis.com/v1"
PLACES = "https://places.googleapis.com/v1"
ROUTES = "https://routes.googleapis.com/directions/v2:computeRoutes"
LANG = "en"

# vapor night look for Google's own tiles (Maps JS style syntax, as the Map Tiles API takes it)
VAPOR_STYLE = [
    {"elementType": "geometry", "stylers": [{"color": "#1c2340"}]},
    {"elementType": "labels.text.fill", "stylers": [{"color": "#c5a0ff"}]},
    {"elementType": "labels.text.stroke", "stylers": [{"color": "#141a33"}]},
    {"featureType": "administrative", "elementType": "geometry.stroke", "stylers": [{"color": "#4b4f86"}]},
    {"featureType": "landscape.natural", "elementType": "geometry", "stylers": [{"color": "#1f2747"}]},
    {"featureType": "poi", "elementType": "geometry", "stylers": [{"color": "#252c52"}]},
    {"featureType": "poi", "elementType": "labels.text.fill", "stylers": [{"color": "#8fe0f2"}]},
    {"featureType": "poi.park", "elementType": "geometry", "stylers": [{"color": "#1d3a47"}]},
    {"featureType": "road", "elementType": "geometry", "stylers": [{"color": "#363d6e"}]},
    {"featureType": "road", "elementType": "geometry.stroke", "stylers": [{"color": "#1c2340"}]},
    {"featureType": "road", "elementType": "labels.text.fill", "stylers": [{"color": "#b9b4e6"}]},
    {"featureType": "road.highway", "elementType": "geometry", "stylers": [{"color": "#7a5aa8"}]},
    {"featureType": "road.highway", "elementType": "labels.text.fill", "stylers": [{"color": "#f7a1e8"}]},
    {"featureType": "transit", "elementType": "geometry", "stylers": [{"color": "#2d3360"}]},
    {"featureType": "transit.station", "elementType": "labels.text.fill", "stylers": [{"color": "#7ef0d2"}]},
    {"featureType": "water", "elementType": "geometry", "stylers": [{"color": "#0e1430"}]},
    {"featureType": "water", "elementType": "labels.text.fill", "stylers": [{"color": "#5d6aa8"}]},
]


def _mix(a, b, t):
    """Blend two #rrggbb colors (t = share of b)."""
    a, b = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _sea(c, fallback="#3d7fc4"):
    """The theme's blue, unless it is too grey to read as water (nasa): then a plain sea blue."""
    r, g, b = [int(c[i:i + 2], 16) for i in (1, 3, 5)]
    return c if b - min(r, g) >= 40 else fallback


def theme_style():
    """(theme name, Google tile styles): vapor keeps its hand-made night style; any other theme
    (micro: paper + ink) is built from its palette."""
    try:
        import notareal_theme as t
        name, pal = t.active(), t.palette()
    except Exception:
        return "vapor", VAPOR_STYLE
    if name == "vapor":
        return name, VAPOR_STYLE
    bg, deep, surf = pal["bg"], pal["bg-deep"], pal["surface"]
    fg, acc = pal["fg"], pal["accent"]
    ink_soft = _mix(fg, bg, 0.45)
    sea = _sea(pal["blue"])
    return name, [
        {"elementType": "geometry", "stylers": [{"color": bg}]},
        {"elementType": "labels.text.fill", "stylers": [{"color": _mix(fg, bg, 0.2)}]},
        {"elementType": "labels.text.stroke", "stylers": [{"color": bg}]},
        {"elementType": "labels.icon", "stylers": [{"saturation": -100}, {"lightness": 20}]},
        {"featureType": "administrative", "elementType": "geometry.stroke", "stylers": [{"color": ink_soft}]},
        {"featureType": "landscape.natural", "elementType": "geometry", "stylers": [{"color": _mix(bg, deep, 0.5)}]},
        {"featureType": "landscape.man_made", "elementType": "geometry", "stylers": [{"color": deep}]},
        {"featureType": "poi", "elementType": "geometry", "stylers": [{"color": deep}]},
        {"featureType": "poi", "elementType": "labels.text.fill", "stylers": [{"color": ink_soft}]},
        {"featureType": "poi.park", "elementType": "geometry", "stylers": [{"color": _mix(bg, pal.get("mint", pal["cyan"]), 0.22)}]},
        {"featureType": "road", "elementType": "geometry.fill", "stylers": [{"color": surf}]},
        {"featureType": "road", "elementType": "geometry.stroke", "stylers": [{"color": _mix(bg, fg, 0.28)}]},
        {"featureType": "road", "elementType": "labels.text.fill", "stylers": [{"color": _mix(fg, bg, 0.3)}]},
        {"featureType": "road.highway", "elementType": "geometry.fill", "stylers": [{"color": _mix(surf, pal["yellow"], 0.45)}]},
        {"featureType": "road.highway", "elementType": "geometry.stroke", "stylers": [{"color": _mix(bg, fg, 0.45)}]},
        {"featureType": "road.highway", "elementType": "labels.text.fill", "stylers": [{"color": fg}]},
        {"featureType": "transit", "elementType": "geometry", "stylers": [{"color": _mix(bg, acc, 0.35)}]},
        {"featureType": "transit.station", "elementType": "labels.text.fill", "stylers": [{"color": pal["orange"]}]},
        # water must read as water on every theme: a clear blue cast, well apart from the land shades
        {"featureType": "water", "elementType": "geometry", "stylers": [{"color": _mix(bg, sea, 0.32)}]},
        {"featureType": "water", "elementType": "labels.text.fill", "stylers": [{"color": _mix(fg, sea, 0.6)}]},
    ]


class ApiError(Exception):
    pass


# --- config / key ----------------------------------------------------------------------------

def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(CONF_DIR, exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, CONFIG)


def get_key():
    return os.environ.get("NOTAREAL_MAPS_KEY") or \
        Secret.password_lookup_sync(SCHEMA, {"service": "google-maps"}, None)


def store_key(key):
    Secret.password_store_sync(SCHEMA, {"service": "google-maps"}, Secret.COLLECTION_DEFAULT,
                               "notarealMaps Google API key", key, None)


# --- polyline --------------------------------------------------------------------------------

def decode_polyline(s):
    pts, i, lat, lng = [], 0, 0, 0
    while i < len(s):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += d
            else:
                lng += d
        pts.append((lat / 1e5, lng / 1e5))
    return pts


def dist(a, b):
    """Metres between (lat, lng) points (haversine)."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(min(1, math.sqrt(h)))


def bearing(a, b):
    la1, la2 = math.radians(a[0]), math.radians(b[0])
    dlo = math.radians(b[1] - a[1])
    y = math.sin(dlo) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlo)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def seconds(d):
    """Routes API duration "123s" -> 123."""
    try:
        return int(float(str(d).rstrip("s")))
    except ValueError:
        return 0


# --- client ----------------------------------------------------------------------------------

class Google:
    def __init__(self, key):
        self.key = key
        self.http = requests.Session()
        self.lock = threading.Lock()
        self.ac_session = None

    def _check(self, r):
        if r.status_code >= 400:
            try:
                e = r.json()["error"]
                msg = e.get("message") or e.get("status")
            except (ValueError, KeyError, TypeError):
                msg = r.text[:200]
            raise ApiError(msg)
        return r.json()

    def _post(self, url, body, mask=None):
        h = {"X-Goog-Api-Key": self.key, "Content-Type": "application/json"}
        if mask:
            h["X-Goog-FieldMask"] = mask
        try:
            return self._check(self.http.post(url, json=body, headers=h, timeout=20))
        except requests.RequestException as e:
            raise ApiError("offline: %s" % e) from e

    def _get(self, url, mask=None, **params):
        h = {"X-Goog-Api-Key": self.key}
        if mask:
            h["X-Goog-FieldMask"] = mask
        try:
            return self._check(self.http.get(url, params=params, headers=h, timeout=20))
        except requests.RequestException as e:
            raise ApiError("offline: %s" % e) from e

    # tiles --------------------------------------------------------------------------------
    def tile_session(self, traffic=False):
        """Session token for Google's 2D tiles (cached until close to expiry)."""
        cfg = load_config()
        name = "session_traffic" if traffic else "session"
        s = cfg.get(name)
        theme, styles = theme_style()
        if (s and s.get("expiry", 0) - time.time() > 3 * 86400 and s.get("v") == 3
                and s.get("theme", "vapor") == theme):
            return s["session"]
        body = {"mapType": "roadmap", "language": LANG + "-US", "region": cfg.get("region", "SE"),
                "scale": "scaleFactor2x", "highDpi": True, "styles": styles}
        if traffic:
            body["layerTypes"] = ["layerTraffic"]
        try:
            r = self.http.post(TILES + "/createSession", params={"key": self.key}, json=body, timeout=20)
        except requests.RequestException as e:
            raise ApiError("offline: %s" % e) from e
        d = self._check(r)
        cfg[name] = {"session": d["session"], "expiry": int(d.get("expiry", time.time() + 86400 * 13)), "v": 3,
                     "theme": theme}
        save_config(cfg)
        return d["session"]

    def tile_url(self, session):
        return TILES + "/2dtiles/{z}/{x}/{y}?session=%s&key=%s" % (session, self.key)

    # places -------------------------------------------------------------------------------
    def autocomplete(self, text, near=None):
        if not self.ac_session:
            self.ac_session = str(uuid.uuid4())
        body = {"input": text, "sessionToken": self.ac_session, "languageCode": LANG}
        if near:
            body["locationBias"] = {"circle": {"center": {"latitude": near[0], "longitude": near[1]},
                                               "radius": 30000.0}}
            body["origin"] = {"latitude": near[0], "longitude": near[1]}
        d = self._post(PLACES + "/places:autocomplete", body)
        out = []
        for s in d.get("suggestions", []):
            p = s.get("placePrediction")
            if not p:
                continue
            f = p.get("structuredFormat", {})
            out.append({"id": p["placeId"], "name": f.get("mainText", {}).get("text") or p["text"]["text"],
                        "sub": f.get("secondaryText", {}).get("text", ""),
                        "meters": p.get("distanceMeters")})
        return out

    def search(self, text, near=None):
        body = {"textQuery": text, "languageCode": LANG, "pageSize": 15}
        if near:
            body["locationBias"] = {"circle": {"center": {"latitude": near[0], "longitude": near[1]},
                                               "radius": 30000.0}}
        d = self._post(PLACES + "/places:searchText", body,
                       "places.id,places.displayName,places.formattedAddress,places.location,places.rating,"
                       "places.userRatingCount,places.primaryTypeDisplayName,places.currentOpeningHours.openNow,"
                       "places.currentOpeningHours.nextOpenTime,places.currentOpeningHours.nextCloseTime,"
                       "places.utcOffsetMinutes")
        return [_place(p) for p in d.get("places", [])]

    def search_cid(self, text, near=None):
        """Text search that also returns each place's CID (to match Takeout rows)."""
        body = {"textQuery": text, "languageCode": LANG, "pageSize": 10}
        if near:
            body["locationBias"] = {"circle": {"center": {"latitude": near[0], "longitude": near[1]},
                                               "radius": 50000.0}}
        d = self._post(PLACES + "/places:searchText", body,
                       "places.id,places.displayName,places.formattedAddress,places.location,places.googleMapsUri")
        out = []
        for p in d.get("places", []):
            r = _place(p)
            m = re.search(r"[?&]cid=(\d+)", p.get("googleMapsUri", ""))
            r["cid"] = m.group(1) if m else None
            r["gmaps"] = p.get("googleMapsUri")
            out.append(r)
        return out

    def details(self, place_id):
        self.ac_session_used = self.ac_session
        mask = ("id,displayName,formattedAddress,location,rating,userRatingCount,primaryTypeDisplayName,"
                "currentOpeningHours,regularOpeningHours.weekdayDescriptions,nationalPhoneNumber,"
                "internationalPhoneNumber,websiteUri,googleMapsUri,editorialSummary,priceLevel,"
                "reviews,photos,businessStatus,utcOffsetMinutes")
        params = {"languageCode": LANG}
        if self.ac_session:
            params["sessionToken"] = self.ac_session   # closes the autocomplete session (cheaper)
            self.ac_session = None
        return _place(self._get(PLACES + "/places/" + place_id, mask, **params), full=True)

    def photo(self, name, width=800):
        os.makedirs(os.path.join(CACHE_DIR, "photos"), exist_ok=True)
        path = os.path.join(CACHE_DIR, "photos", name.replace("/", "_")[-120:] + "_%d.jpg" % width)
        if os.path.exists(path):
            return path
        try:
            r = self.http.get("%s/%s/media" % (PLACES, name),
                              params={"maxWidthPx": width, "key": self.key}, timeout=30)
        except requests.RequestException as e:
            raise ApiError("offline: %s" % e) from e
        if r.status_code >= 400:
            raise ApiError("photo %s" % r.status_code)
        with open(path, "wb") as f:
            f.write(r.content)
        return path

    # routes -------------------------------------------------------------------------------
    def routes(self, origin, dest, mode="DRIVE", heading=None, alternatives=True):
        """origin: (lat, lng); dest: place dict (id or location). Returns [route dict]."""
        o = {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}}
        if heading is not None and mode != "TRANSIT":   # Google rejects a heading for transit
            o["location"]["heading"] = int(heading)
        d_ = {"placeId": dest["id"]} if dest.get("id") else \
            {"location": {"latLng": {"latitude": dest["lat"], "longitude": dest["lng"]}}}
        body = {"origin": o, "destination": d_, "travelMode": mode,
                "computeAlternativeRoutes": alternatives, "polylineQuality": "HIGH_QUALITY",
                "languageCode": LANG, "units": "METRIC"}
        if mode in ("DRIVE", "TWO_WHEELER"):
            body["routingPreference"] = "TRAFFIC_AWARE"
            body["extraComputations"] = ["TRAFFIC_ON_POLYLINE"]
        mask = ("routes.duration,routes.staticDuration,routes.distanceMeters,routes.description,"
                "routes.routeLabels,routes.polyline.encodedPolyline,routes.warnings,"
                "routes.travelAdvisory.speedReadingIntervals,routes.legs.steps.distanceMeters,"
                "routes.legs.steps.staticDuration,routes.legs.steps.polyline.encodedPolyline,"
                "routes.legs.steps.navigationInstruction,routes.legs.steps.travelMode,"
                "routes.legs.steps.transitDetails,routes.legs.steps.localizedValues")
        d = self._post(ROUTES, body, mask)
        out = []
        for r in d.get("routes", []):
            pts = decode_polyline(r["polyline"]["encodedPolyline"])
            steps = []
            for leg in r.get("legs", []):
                for st in leg.get("steps", []):
                    ni = st.get("navigationInstruction", {})
                    td = st.get("transitDetails")
                    steps.append({"m": st.get("distanceMeters", 0), "s": seconds(st.get("staticDuration", "0s")),
                                  "maneuver": ni.get("maneuver", "STRAIGHT"),
                                  "text": ni.get("instructions", ""),
                                  "mode": st.get("travelMode", mode),
                                  "transit": _transit(td) if td else None,
                                  "pts": decode_polyline(st["polyline"]["encodedPolyline"])
                                  if st.get("polyline") else []})
            out.append({"pts": pts, "steps": steps, "mode": mode,
                        "m": r.get("distanceMeters", 0), "s": seconds(r.get("duration", "0s")),
                        "static_s": seconds(r.get("staticDuration", r.get("duration", "0s"))),
                        "via": r.get("description", ""), "labels": r.get("routeLabels", []),
                        "warnings": r.get("warnings", []),
                        "traffic": [(i.get("startPolylinePointIndex", 0), i.get("endPolylinePointIndex", 0),
                                     i.get("speed", "NORMAL"))
                                    for i in r.get("travelAdvisory", {}).get("speedReadingIntervals", [])]})
        return out


def _transit(td):
    line = td.get("transitLine", {})
    st = td.get("stopDetails", {})
    return {"line": line.get("nameShort") or line.get("name", ""),
            "vehicle": line.get("vehicle", {}).get("name", {}).get("text", ""),
            "color": line.get("color"), "headsign": td.get("headsign", ""),
            "from": st.get("departureStop", {}).get("name", ""), "to": st.get("arrivalStop", {}).get("name", ""),
            "dep": td.get("localizedValues", {}).get("departureTime", {}).get("time", {}).get("text", ""),
            "stops": td.get("stopCount", 0)}


def _place(p, full=False):
    loc = p.get("location", {})
    out = {"id": p.get("id"), "name": p.get("displayName", {}).get("text", ""),
           "address": p.get("formattedAddress", ""), "lat": loc.get("latitude"), "lng": loc.get("longitude"),
           "rating": p.get("rating"), "ratings": p.get("userRatingCount"),
           "type": p.get("primaryTypeDisplayName", {}).get("text", ""),
           "open": p.get("currentOpeningHours", {}).get("openNow"),
           # RFC 3339 UTC; shown in the place's own time zone (utc_off minutes)
           "next_open": p.get("currentOpeningHours", {}).get("nextOpenTime"),
           "next_close": p.get("currentOpeningHours", {}).get("nextCloseTime"),
           "utc_off": p.get("utcOffsetMinutes")}
    if full:
        out.update({
            "hours": p.get("regularOpeningHours", {}).get("weekdayDescriptions", []),
            "phone": p.get("nationalPhoneNumber") or p.get("internationalPhoneNumber"),
            "phone_intl": p.get("internationalPhoneNumber"),
            "web": p.get("websiteUri"), "gmaps": p.get("googleMapsUri"),
            "summary": p.get("editorialSummary", {}).get("text", ""),
            "price": p.get("priceLevel"), "status": p.get("businessStatus"),
            "photos": [ph["name"] for ph in p.get("photos", [])[:10]],
            "reviews": [{"who": r.get("authorAttribution", {}).get("displayName", ""),
                         "stars": r.get("rating"), "when": r.get("relativePublishTimeDescription", ""),
                         "text": (r.get("text") or r.get("originalText") or {}).get("text", "")}
                        for r in p.get("reviews", [])]})
    return out
