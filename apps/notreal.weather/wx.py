"""Weather data for notarealWeather: Open-Meteo (forecast + air quality + geocoding), no API key.

Same source Breezy Weather uses by default. Everything is plain dicts; fetch() caches the last
good answer per place in ~/.cache/nrweather so the app opens instantly and works offline.
"""
import json
import os
import time
import urllib.parse
import urllib.request

CACHE = os.path.expanduser("~/.cache/nrweather")
UA = {"User-Agent": "notrealOS-weather/1.0"}

FORECAST = "https://api.open-meteo.com/v1/forecast"
AIR = "https://air-quality-api.open-meteo.com/v1/air-quality"
GEO = "https://geocoding-api.open-meteo.com/v1/search"
REVERSE = "https://nominatim.openstreetmap.org/reverse"

CURRENT = ("temperature_2m,apparent_temperature,relative_humidity_2m,dew_point_2m,is_day,weather_code,"
           "cloud_cover,pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation,visibility")
HOURLY = ("temperature_2m,apparent_temperature,weather_code,precipitation_probability,precipitation,"
          "wind_speed_10m,wind_direction_10m,relative_humidity_2m,uv_index,is_day,visibility,pressure_msl")
DAILY = ("weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,apparent_temperature_min,"
         "sunrise,sunset,daylight_duration,uv_index_max,precipitation_sum,precipitation_probability_max,"
         "precipitation_hours,wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant")
AIR_CURRENT = ("european_aqi,us_aqi,pm2_5,pm10,nitrogen_dioxide,ozone,sulphur_dioxide,carbon_monoxide,uv_index,"
               "alder_pollen,birch_pollen,grass_pollen,mugwort_pollen,olive_pollen,ragweed_pollen")
AIR_HOURLY = "european_aqi,us_aqi"


def _get(url, params, timeout=12):
    full = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(full, headers=UA), timeout=timeout) as r:
        return json.load(r)


def search(text, lang="en"):
    """Place search -> [{name, sub, lat, lon}]"""
    d = _get(GEO, {"name": text, "count": 12, "language": lang, "format": "json"})
    out = []
    for p in d.get("results") or []:
        sub = ", ".join(x for x in (p.get("admin1"), p.get("country")) if x)
        out.append({"name": p["name"], "sub": sub, "lat": p["latitude"], "lon": p["longitude"]})
    return out


def place_name(lat, lon):
    """Town name for a GPS fix (OpenStreetMap Nominatim, one call per fix)."""
    try:
        d = _get(REVERSE, {"lat": lat, "lon": lon, "format": "json", "zoom": 12}, timeout=8)
        a = d.get("address") or {}
        return (a.get("city") or a.get("town") or a.get("village") or a.get("suburb")
                or a.get("municipality") or d.get("name") or "Here")
    except Exception:          # noqa: BLE001 - a name is cosmetic
        return "Here"


def _key(lat, lon):
    return f"{lat:.2f}_{lon:.2f}"


def cached(lat, lon):
    try:
        with open(os.path.join(CACHE, _key(lat, lon) + ".json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def fetch(lat, lon, units):
    """units: {"temp": "celsius"|"fahrenheit", "wind": "kmh"|"ms"|"mph"|"kn", "precip": "mm"|"inch"}"""
    common = {"latitude": lat, "longitude": lon, "timezone": "auto"}
    fc = _get(FORECAST, dict(common, current=CURRENT, hourly=HOURLY, daily=DAILY, minutely_15="precipitation",
                             forecast_days=10, forecast_minutely_15=8,
                             temperature_unit=units.get("temp", "celsius"),
                             wind_speed_unit=units.get("wind", "kmh"),
                             precipitation_unit=units.get("precip", "mm")))
    try:
        air = _get(AIR, dict(common, current=AIR_CURRENT, hourly=AIR_HOURLY, forecast_days=1))
    except Exception:          # noqa: BLE001 - air quality is optional (outside coverage, outage)
        air = {}
    d = {"fetched": time.time(), "units": units, "fc": fc, "air": air}
    os.makedirs(CACHE, exist_ok=True)
    tmp = os.path.join(CACHE, _key(lat, lon) + ".tmp")
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, tmp[:-4] + ".json")
    return d


# --- WMO weather codes ---------------------------------------------------------------------------
TEXT = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Rime fog",
        51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
        61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
        71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Showers", 81: "Showers",
        82: "Violent showers", 85: "Snow showers", 86: "Snow showers", 95: "Thunderstorm",
        96: "Thunder + hail", 99: "Thunder + hail"}


def kind(code, day=True):
    """WMO code -> icon name in icons.py"""
    c = int(code or 0)
    if c == 0:
        return "sun" if day else "moon"
    if c in (1, 2):
        return "partly" if day else "partly-night"
    if c == 3:
        return "cloud"
    if c in (45, 48):
        return "fog"
    if c in (51, 53, 55, 56, 57):
        return "drizzle"
    if c in (61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    if c in (71, 73, 75, 77, 85, 86):
        return "snow"
    return "thunder"


def aqi_text(v):
    """European AQI bands"""
    if v is None:
        return "—"
    for lim, t in ((20, "Excellent"), (40, "Good"), (60, "Moderate"), (80, "Poor"), (100, "Very poor")):
        if v <= lim:
            return t
    return "Extremely poor"


def uv_text(v):
    if v is None:
        return "—"
    for lim, t in ((2.5, "Low"), (5.5, "Moderate"), (7.5, "High"), (10.5, "Very high")):
        if v < lim:
            return t
    return "Extreme"


def pollen_text(v):
    if not v:
        return "None"
    for lim, t in ((10, "Low"), (50, "Moderate"), (200, "High")):
        if v < lim:
            return t
    return "Very high"


COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(deg):
    return COMPASS[int(((deg or 0) % 360) / 22.5 + 0.5) % 16]


def beaufort(kmh):
    for i, lim in enumerate((1, 6, 12, 20, 29, 39, 50, 62, 75, 89, 103, 118)):
        if kmh < lim:
            return i
    return 12


BEAUFORT = ["Calm", "Light air", "Light breeze", "Gentle breeze", "Moderate breeze", "Fresh breeze",
            "Strong breeze", "Near gale", "Gale", "Strong gale", "Storm", "Violent storm", "Hurricane"]


def moon_phase(t=None):
    """0..1 (0 new, .5 full) from a known new moon; good to a few hours."""
    t = time.time() if t is None else t
    return ((t - 947182440) / 86400 / 29.530588853) % 1.0


def moon_text(p):
    names = ["New moon", "Waxing crescent", "First quarter", "Waxing gibbous", "Full moon",
             "Waning gibbous", "Last quarter", "Waning crescent"]
    return names[int(p * 8 + 0.5) % 8]
