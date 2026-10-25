#!/usr/bin/env python3
"""notarealWeather: Breezy-Weather-style forecasts in the Nothing weather app's look, themed.

Open-Meteo (wx.py): current, 48 h hourly, 10 days, 2 h rain nowcast, air quality + pollen.
Current location via GeoClue + saved places; swipe between them. Big temperature disc,
dot-matrix icons, tiles (air, humidity, wind, UV, feels like, rain, pressure, visibility,
sun, moon, pollen); tap a day for its own page, tap AIR QUALITY for the pollutants.
Left-handed: places + refresh on the left, settings (rare) on the right.
"""
import datetime
import json
import os
import sys
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.expanduser("~/theme"))
import notareal_theme  # noqa: E402
import draw  # noqa: E402
import radar  # noqa: E402
import wx  # noqa: E402

APP_ID = "dev.notareal.Weather"
CONF = os.path.expanduser("~/.config/nrweather/config.json")
STALE = 15 * 60          # refetch a place when its data is older than this
WIND = {"kmh": "km/h", "ms": "m/s", "mph": "mph", "kn": "kn"}
DEFAULT = {"locations": [{"gps": True, "name": "Current location", "lat": 59.33, "lon": 18.07}],
           "units": {"temp": "celsius", "wind": "kmh", "precip": "mm"}, "index": 0}


def bg(work, done=None, fail=None):
    def run():
        try:
            r = work()
        except Exception as e:     # noqa: BLE001 - shown to the user
            print("nrweather:", repr(e), flush=True)
            if fail:
                GLib.idle_add(fail, e)
            return
        if done:
            GLib.idle_add(done, r)
    threading.Thread(target=run, daemon=True).start()


def label(text="", css=None, xalign=0.0, ellipsize=True, wrap=False):
    lb = Gtk.Label(label=text, xalign=xalign)
    if ellipsize and not wrap:
        lb.set_ellipsize(Pango.EllipsizeMode.END)
    if wrap:
        lb.set_wrap(True)
    if css:
        for c in css.split():
            lb.add_css_class(c)
    return lb


def vbox(spacing=0, css=None, **kw):
    b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kw)
    for c in (css or "").split():
        b.add_css_class(c)
    return b


def deg(v):
    return "—" if v is None else f"{round(v)}°"


def load_conf():
    try:
        with open(CONF) as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = {}
    for k, v in DEFAULT.items():
        c.setdefault(k, json.loads(json.dumps(v)))
    return c


def save_conf(c):
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    with open(CONF + ".tmp", "w") as f:
        json.dump(c, f, indent=1)
    os.replace(CONF + ".tmp", CONF)


def local_now(fc):
    """Now at the place, as its local 'YYYY-MM-DDTHH:MM' (Open-Meteo times are local with timezone=auto)."""
    t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=fc.get("utc_offset_seconds", 0))
    return t.replace(tzinfo=None)


def parse(s):
    return datetime.datetime.fromisoformat(s)


# --- tiles ------------------------------------------------------------------------------------

def tile(title, sub=None, canvas=None, value="", unit="", wide=False, on_tap=None):
    box = vbox(4, "wx-card tile")
    box.append(label(title, "tile-title"))
    if sub:
        box.append(label(sub, "tile-sub"))
    if canvas:
        canvas.set_vexpand(True)
        canvas.set_margin_top(6)
        canvas.set_margin_bottom(4)
        box.append(canvas)
    row = Gtk.Box(spacing=4)
    row.append(label(value, "tile-value", ellipsize=False))
    if unit:
        u = label(unit, "tile-unit", ellipsize=False)
        u.set_valign(Gtk.Align.END)
        u.set_margin_bottom(6)
        row.append(u)
    box.append(row)
    box.set_size_request(-1, 300 if wide else 200)
    if on_tap:
        btn = Gtk.Button(child=box, css_classes=["tile-btn", "flat"])
        btn.connect("clicked", lambda *_: on_tap())
        return btn
    return box


class PlacePage(Gtk.ScrolledWindow):
    """One place's whole forecast (rebuilt on every data/theme change)."""
    def __init__(self, win, loc):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, hexpand=True)
        self.win, self.loc = win, loc
        self.data = wx.cached(loc["lat"], loc["lon"])
        self.fetching = False
        self.render()

    def stale(self):
        return not self.data or time.time() - self.data.get("fetched", 0) > STALE \
            or self.data.get("units") != self.win.conf["units"]

    def refresh(self, force=False):
        if self.fetching or not (force or self.stale()):
            return
        self.fetching = True
        lat, lon, units = self.loc["lat"], self.loc["lon"], dict(self.win.conf["units"])

        def done(d):
            self.fetching = False
            self.data = d
            self.render()

        def fail(e):
            self.fetching = False
            self.win.toast("no weather: " + (str(e)[:60] or type(e).__name__))
        bg(lambda: wx.fetch(lat, lon, units), done, fail)

    # .........................................................................................
    def render(self):
        box = vbox(14, "place", margin_start=14, margin_end=14, margin_top=6, margin_bottom=30)
        self.set_child(box)
        if not self.data:
            box.append(label("LOADING…", "status", 0.5))
            return
        fc, air = self.data["fc"], (self.data.get("air") or {}).get("current") or {}
        cur, hourly, daily = fc["current"], fc["hourly"], fc["daily"]
        units = self.data.get("units") or self.win.conf["units"]
        wind_u = WIND.get(units.get("wind", "kmh"), "km/h")
        now = local_now(fc)
        hour_key = now.strftime("%Y-%m-%dT%H")
        h0 = next((i for i, t in enumerate(hourly["time"]) if t[:13] >= hour_key), 0)
        today = 0 if daily["time"][0] >= now.strftime("%Y-%m-%d") else 1
        aqi = air.get("european_aqi")
        uv = air.get("uv_index")
        if uv is None:
            uv = hourly["uv_index"][h0]

        # top: the disc + two pills ---------------------------------------------------------
        top = Gtk.Box(spacing=14)
        ov = Gtk.Overlay()
        disc = draw.Disc(184, 184)
        ov.set_child(disc)
        inner = vbox(2, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        inner.append(label(deg(cur["temperature_2m"]), "big-temp", 0.5, False))
        inner.append(label(f"H:{deg(daily['temperature_2m_max'][today])} L:{deg(daily['temperature_2m_min'][today])}",
                           "disc-sub", 0.5, False))
        inner.append(label(wx.TEXT.get(cur["weather_code"], "").upper(), "disc-cond", 0.5))
        ov.add_overlay(inner)
        top.append(ov)
        pills = vbox(14, hexpand=True)
        for title, val, txt in (("AIR QUALITY", "—" if aqi is None else str(round(aqi)), wx.aqi_text(aqi)),
                                ("UV INDEX", f"{round(uv or 0):02d}", wx.uv_text(uv))):
            p = vbox(2, "wx-card pill", vexpand=True, valign=Gtk.Align.FILL)
            p.set_valign(Gtk.Align.FILL)
            head = Gtk.Box(spacing=10)
            head.append(label(title, "pill-title", ellipsize=False))
            head.append(label(val, "pill-title", ellipsize=False))
            p.append(head)
            p.append(label(txt, "pill-sub"))
            pills.append(p)
        top.append(pills)
        box.append(top)
        box.append(Gtk.Separator(css_classes=["wx-rule"]))

        # rain in the next two hours ---------------------------------------------------------
        m15 = (fc.get("minutely_15") or {}).get("precipitation") or []
        if any(v and v > 0.05 for v in m15):
            card = vbox(6, "wx-card nowcast")
            first = next(i for i, v in enumerate(m15) if v and v > 0.05)
            last_wet = max(i for i, v in enumerate(m15) if v and v > 0.05)
            if first == 0:
                msg = "RAIN NOW" + (f" · STOPS IN {(last_wet + 1) * 15} MIN" if last_wet < len(m15) - 1 else "")
            else:
                msg = f"RAIN IN {first * 15} MIN"
            card.append(label(msg, "tile-title"))
            bars = draw.Bars(54)
            top_v = max(max(m15), 0.5)
            bars.values = [(v or 0) / top_v for v in m15]
            card.append(bars)
            ticks = Gtk.Box(homogeneous=True)
            for t in ("NOW", "30", "60", "90", "120"):
                ticks.append(label(t, "tick", 0.5))
            card.append(ticks)
            box.append(card)

        # hourly ------------------------------------------------------------------------------
        hrow = Gtk.Box(spacing=26, css_classes=["strip"])
        for i in range(h0, min(h0 + 25, len(hourly["time"]))):
            col = vbox(6)
            hh = parse(hourly["time"][i]).hour
            col.append(label("NOW" if i == h0 else f"{hh:02d}", "strip-head" + (" now" if i == h0 else ""), 0.5, False))
            line = Gtk.Box(spacing=8)
            line.append(draw.DotIcon(wx.kind(hourly["weather_code"][i], hourly["is_day"][i]), 24))
            line.append(label(deg(hourly["temperature_2m"][i]), "strip-temp", ellipsize=False))
            col.append(line)
            pop = hourly["precipitation_probability"][i] or 0
            if any((v or 0) >= 20 for v in hourly["precipitation_probability"][h0:h0 + 25]):
                col.append(label(f"{pop}%" if pop >= 20 else " ", "strip-pop", 0.5))
            hrow.append(col)
        box.append(self._strip(hrow))

        # daily ------------------------------------------------------------------------------
        drow = Gtk.Box(spacing=4, css_classes=["strip"])
        for i in range(today, len(daily["time"])):
            d = parse(daily["time"][i])
            col = vbox(8)
            col.append(label("TODAY" if i == today else d.strftime("%a").upper(),
                             "strip-head" + (" now" if i == today else ""), 0.5, False))
            col.append(draw.DotIcon(wx.kind(daily["weather_code"][i]), 26))
            col.append(label(deg(daily["temperature_2m_max"][i]), "strip-temp", 0.5, False))
            col.append(label(deg(daily["temperature_2m_min"][i]), "strip-low", 0.5, False))
            b = Gtk.Button(child=col, css_classes=["flat", "day-btn"])
            b.connect("clicked", lambda *_, i=i: self.win.nav.push(DayPage(self, i, i == today)))
            drow.append(b)
        box.append(self._strip(drow))

        # tiles ------------------------------------------------------------------------------
        grid = Gtk.Grid(column_spacing=14, row_spacing=14, column_homogeneous=True)
        tiles = []
        g = draw.AqiGauge()
        g.value = aqi or 0
        tiles.append(tile("AIR QUALITY", wx.aqi_text(aqi), g, "—" if aqi is None else str(round(aqi)),
                          on_tap=lambda: self.win.nav.push(AirPage(self))))
        hd = draw.HumidityDial()
        hd.value = cur["relative_humidity_2m"]
        tiles.append(tile("HUMIDITY", f"Dew point {deg(cur['dew_point_2m'])}", hd,
                          f"{cur['relative_humidity_2m']}", "%"))
        wc = draw.WindCompass()
        wc.direction = cur["wind_direction_10m"]
        kmh = cur["wind_speed_10m"] * {"kmh": 1, "ms": 3.6, "mph": 1.609, "kn": 1.852}[units.get("wind", "kmh")]
        wc.speed = kmh
        tiles.append(tile("WIND SPEED", f"{wx.compass(cur['wind_direction_10m'])} · gusts {round(cur['wind_gusts_10m'])}",
                          wc, str(round(cur["wind_speed_10m"])), wind_u))
        us = draw.UvStack()
        us.value = uv or 0
        tiles.append(tile("UV INDEX", wx.uv_text(uv), us, f"{round(uv or 0):02d}"))
        th = draw.Thermo()
        th.value = cur["apparent_temperature"] if units.get("temp") != "fahrenheit" \
            else (cur["apparent_temperature"] - 32) / 1.8
        diff = cur["apparent_temperature"] - cur["temperature_2m"]
        tiles.append(tile("FEELS LIKE", "Similar" if abs(diff) < 1.5 else ("Colder" if diff < 0 else "Warmer"),
                          th, deg(cur["apparent_temperature"])))
        bk = draw.Bucket()
        rain = daily["precipitation_sum"][today] or 0
        bk.value = rain if units.get("precip") != "inch" else rain * 25.4
        tiles.append(tile("PRECIPITATION", f"{daily['precipitation_probability_max'][today] or 0}% chance today", bk,
                          f"{rain:g}", units.get("precip", "mm")))
        pg = draw.PressureGauge()
        pg.value = cur["pressure_msl"]
        tiles.append(tile("PRESSURE", self._trend(hourly, h0), pg, str(round(cur["pressure_msl"])), "hPa"))
        ey = draw.EyeLines()
        ey.value = cur.get("visibility") or 0
        vis = (cur.get("visibility") or 0) / 1000
        tiles.append(tile("VISIBILITY", "Clear" if vis >= 10 else "Hazy" if vis >= 2 else "Poor", ey,
                          f"{vis:.0f}" if vis >= 10 else f"{vis:.1f}", "km"))
        for n, t in enumerate(tiles):
            grid.attach(t, n % 2, n // 2, 1, 1)
        row = len(tiles) // 2
        # sun (wide)
        sr, ss = parse(daily["sunrise"][today]), parse(daily["sunset"][today])
        sa = draw.SunArc()
        sa.pos = (now - sr) / (ss - sr) if sr <= now <= ss else None
        daylen = daily["daylight_duration"][today] / 3600
        grid.attach(tile("SUNRISE · SUNSET", f"{int(daylen)} h {int(daylen % 1 * 60)} min of daylight", sa,
                         f"{sr:%H:%M} · {ss:%H:%M}", wide=True), 0, row, 2, 1)
        # moon + cloud cover
        mp = wx.moon_phase()
        md = draw.MoonDisc()
        md.phase = mp
        grid.attach(tile("MOON", wx.moon_text(mp), md, f"{round((1 - abs(1 - 2 * mp)) * 100)}", "% lit"), 0, row + 1, 1, 1)
        cc = draw.HumidityDial()
        cc.value = cur["cloud_cover"]
        grid.attach(tile("CLOUD COVER", "Sky covered", cc, str(cur["cloud_cover"]), "%"), 1, row + 1, 1, 1)
        # pollen (wide), only where Open-Meteo has it (Europe)
        pollen = [(n, air.get(k)) for n, k in (("ALDER", "alder_pollen"), ("BIRCH", "birch_pollen"),
                                                ("GRASS", "grass_pollen"), ("MUGWORT", "mugwort_pollen"),
                                                ("OLIVE", "olive_pollen"), ("RAGWEED", "ragweed_pollen"))]
        if any(v is not None for _n, v in pollen):
            card = vbox(6, "wx-card tile")
            worst = max((v or 0) for _n, v in pollen)
            card.append(label("POLLEN", "tile-title"))
            card.append(label(wx.pollen_text(worst), "tile-sub"))
            bars = draw.Bars(70)
            bars.values = [min(1.0, (v or 0) / 200) for _n, v in pollen]
            bars.set_margin_top(10)
            card.append(bars)
            names = Gtk.Box(homogeneous=True)
            for n, _v in pollen:
                names.append(label(n, "tick", 0.5))
            card.append(names)
            grid.attach(card, 0, row + 2, 2, 1)
        box.append(grid)

        age = int((time.time() - self.data["fetched"]) / 60)
        box.append(label(f"OPEN-METEO · UPDATED {'NOW' if age < 1 else f'{age} MIN AGO'}", "footer", 0.5))

    def _strip(self, row):
        card = vbox(0, "wx-card strip-card")
        sw = Gtk.ScrolledWindow(child=row, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                hscrollbar_policy=Gtk.PolicyType.EXTERNAL, propagate_natural_height=True)
        card.append(sw)
        return card

    @staticmethod
    def _trend(hourly, h0):
        p = [v for v in (hourly.get("pressure_msl") or []) if v]
        if len(p) > h0 + 3:
            d = p[h0 + 3] - p[h0]
            return "Rising" if d > 1 else "Falling" if d < -1 else "Steady"
        return "Sea level"


class DayPage(Adw.NavigationPage):
    def __init__(self, place, i, is_today=False):
        fc = place.data["fc"]
        daily, hourly = fc["daily"], fc["hourly"]
        units = place.data.get("units") or {}
        wind_u = WIND.get(units.get("wind", "kmh"), "km/h")
        d = parse(daily["time"][i])
        super().__init__(title=d.strftime("%A"))
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        hb.set_title_widget(label(d.strftime("%a %-d %b").upper(), "city-title", 0.5, False))
        tv.add_top_bar(hb)
        box = vbox(14, margin_start=14, margin_end=14, margin_top=6, margin_bottom=30)
        head = Gtk.Box(spacing=18)
        head.append(draw.DotIcon(wx.kind(daily["weather_code"][i]), 96))
        hv = vbox(2, valign=Gtk.Align.CENTER)
        hv.append(label(f"{deg(daily['temperature_2m_max'][i])} / {deg(daily['temperature_2m_min'][i])}",
                        "day-temp", ellipsize=False))
        hv.append(label(wx.TEXT.get(daily["weather_code"][i], "").upper(), "pill-sub"))
        hv.append(label(f"Feels {deg(daily['apparent_temperature_max'][i])} / "
                        f"{deg(daily['apparent_temperature_min'][i])}", "tile-sub"))
        head.append(hv)
        box.append(head)
        box.append(Gtk.Separator(css_classes=["wx-rule"]))
        if is_today:              # radar = what fell in the last 2 h, so only on today's page
            card = vbox(8, "wx-card tile")
            card.append(label("PRECIPITATION RADAR", "tile-title"))
            at = os.environ.get("NRWX_RADAR_AT")        # test: "lat,lon,zoom" somewhere wet
            lat, lon, z = map(float, at.split(",")) if at else (place.loc["lat"], place.loc["lon"], 6.0)
            card.append(radar.Radar(lat, lon, bg, zoom=z))
            box.append(card)
        key = daily["time"][i]
        idx = [j for j, t in enumerate(hourly["time"]) if t.startswith(key)]
        if idx:
            card = vbox(6, "wx-card tile")
            card.append(label("TEMPERATURE · RAIN CHANCE", "tile-title"))
            ch = draw.TempChart(190)
            ch.temps = [hourly["temperature_2m"][j] for j in idx]
            ch.pops = [hourly["precipitation_probability"][j] or 0 for j in idx]
            ch.labels = [hourly["time"][j][11:13] for j in idx]
            card.append(ch)
            box.append(card)
        rows = (("PRECIPITATION", f"{daily['precipitation_sum'][i] or 0:g} {units.get('precip', 'mm')} · "
                                  f"{daily['precipitation_probability_max'][i] or 0}% · "
                                  f"{daily['precipitation_hours'][i] or 0:g} h"),
                ("WIND", f"{round(daily['wind_speed_10m_max'][i])} {wind_u} "
                         f"{wx.compass(daily['wind_direction_10m_dominant'][i])} · gusts "
                         f"{round(daily['wind_gusts_10m_max'][i])}"),
                ("UV INDEX", f"{daily['uv_index_max'][i] or 0:.0f} · {wx.uv_text(daily['uv_index_max'][i])}"),
                ("SUNRISE", f"{parse(daily['sunrise'][i]):%H:%M}"),
                ("SUNSET", f"{parse(daily['sunset'][i]):%H:%M}"),
                ("DAYLIGHT", f"{daily['daylight_duration'][i] / 3600:.1f} h"))
        card = vbox(10, "wx-card tile")
        for k, v in rows:
            r = Gtk.Box(spacing=10)
            r.append(label(k, "tile-title", ellipsize=False))
            v = label(v, "row-value", 1.0)
            v.set_hexpand(True)
            r.append(v)
            card.append(r)
        box.append(card)
        tv.set_content(Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.set_child(tv)


class AirPage(Adw.NavigationPage):
    def __init__(self, place):
        super().__init__(title="Air quality")
        air = (place.data.get("air") or {}).get("current") or {}
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        hb.set_title_widget(label("AIR QUALITY", "city-title", 0.5))
        tv.add_top_bar(hb)
        box = vbox(14, margin_start=14, margin_end=14, margin_top=6, margin_bottom=30)
        aqi = air.get("european_aqi")
        g = draw.AqiGauge()
        g.value = aqi or 0
        box.append(tile("EUROPEAN AQI", wx.aqi_text(aqi), g, "—" if aqi is None else str(round(aqi)), wide=True))
        card = vbox(10, "wx-card tile")
        for name, k, unit in (("US AQI", "us_aqi", ""), ("PM2.5", "pm2_5", "µg/m³"), ("PM10", "pm10", "µg/m³"),
                              ("NO₂", "nitrogen_dioxide", "µg/m³"), ("O₃", "ozone", "µg/m³"),
                              ("SO₂", "sulphur_dioxide", "µg/m³"), ("CO", "carbon_monoxide", "µg/m³")):
            v = air.get(k)
            r = Gtk.Box(spacing=10)
            r.append(label(name, "tile-title", ellipsize=False))
            vl = label("—" if v is None else f"{v:g} {unit}", "row-value", 1.0)
            vl.set_hexpand(True)
            r.append(vl)
            card.append(r)
        box.append(card)
        if not air:
            box.append(label("No air-quality data for this place.", "status", 0.5))
        tv.set_content(Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.set_child(tv)


# --- places sheet -----------------------------------------------------------------------------

class PlacesSheet(Adw.Dialog):
    def __init__(self, win):
        super().__init__(title="Places")
        self.set_presentation_mode(Adw.DialogPresentationMode.BOTTOM_SHEET)
        self.set_content_height(760)
        self.win = win
        box = vbox(8, margin_start=16, margin_end=16, margin_top=10, margin_bottom=16)
        box.append(label("PLACES", "sheet-title", 0.5))
        self.entry = Gtk.SearchEntry(placeholder_text="Add a city…")
        self.entry.connect("search-changed", self.on_search)
        box.append(self.entry)
        self.results = vbox(4)
        box.append(self.results)
        self.saved = vbox(6)
        box.append(self.saved)
        refresh = Gtk.Button(label="⟳  REFRESH ALL", css_classes=["wx-btn"], halign=Gtk.Align.START,
                             margin_top=10)
        refresh.connect("clicked", lambda *_: (win.refresh_all(True), self.close()))
        box.append(refresh)
        self.set_child(Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.fill_saved()
        self.seq = 0

    def fill_saved(self):
        while (c := self.saved.get_first_child()):
            self.saved.remove(c)
        for n, loc in enumerate(self.win.conf["locations"]):
            row = Gtk.Box(spacing=8)
            go = Gtk.Button(css_classes=["wx-card", "place-row", "flat"], hexpand=True)
            inner = Gtk.Box(spacing=12)
            data = wx.cached(loc["lat"], loc["lon"])
            if data:
                c = data["fc"]["current"]
                inner.append(draw.DotIcon(wx.kind(c["weather_code"], c["is_day"]), 24))
            name = label(("◎ " if loc.get("gps") else "") + loc["name"].upper(), "pill-title")
            name.set_hexpand(True)
            inner.append(name)
            if data:
                inner.append(label(deg(data["fc"]["current"]["temperature_2m"]), "strip-temp", 1.0, False))
            go.set_child(inner)
            go.connect("clicked", lambda *_, n=n: (self.win.show_place(n), self.close()))
            row.append(go)
            if not loc.get("gps"):
                rm = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"],
                                valign=Gtk.Align.CENTER, tooltip_text="Remove")
                rm.connect("clicked", lambda *_, n=n: (self.win.remove_place(n), self.fill_saved()))
                row.append(rm)                       # rare + destructive: right side
            self.saved.append(row)

    def on_search(self, entry):
        text = entry.get_text().strip()
        self.seq += 1
        seq = self.seq
        if len(text) < 2:
            self.show_results([])
            return

        def later():
            if seq == self.seq:
                bg(lambda: wx.search(text), lambda r: seq == self.seq and self.show_results(r),
                   lambda e: self.win.toast("search failed"))
            return False
        GLib.timeout_add(350, later)

    def show_results(self, items):
        while (c := self.results.get_first_child()):
            self.results.remove(c)
        for it in items:
            b = Gtk.Button(css_classes=["flat", "result-row"])
            v = vbox(0)
            v.append(label(it["name"], "pill-title"))
            v.append(label(it["sub"], "tile-sub"))
            b.set_child(v)
            b.connect("clicked", lambda *_, it=it: (self.win.add_place(it), self.close()))
            self.results.append(b)


class SettingsSheet(Adw.Dialog):
    def __init__(self, win):
        super().__init__(title="Settings")
        self.set_presentation_mode(Adw.DialogPresentationMode.BOTTOM_SHEET)
        self.win = win
        box = vbox(10, margin_start=16, margin_end=16, margin_top=10, margin_bottom=20)
        box.append(label("SETTINGS", "sheet-title", 0.5))
        for key, title, opts in (("temp", "TEMPERATURE", (("celsius", "°C"), ("fahrenheit", "°F"))),
                                 ("wind", "WIND", (("kmh", "km/h"), ("ms", "m/s"), ("mph", "mph"), ("kn", "kn"))),
                                 ("precip", "RAIN", (("mm", "mm"), ("inch", "in")))):
            box.append(label(title, "tile-title"))
            row = Gtk.Box(css_classes=["linked"])
            first = None
            for val, text in opts:
                b = Gtk.ToggleButton(label=text, active=win.conf["units"].get(key) == val, css_classes=["unit-btn"])
                if first:
                    b.set_group(first)
                first = first or b
                b.connect("toggled", lambda b, k=key, v=val: b.get_active() and self.set_unit(k, v))
                row.append(b)
            box.append(row)
        box.append(label(f"Look follows the phone theme ({notareal_theme.active()}); switch it from the ◐ picker.",
                         "tile-sub", wrap=True))
        box.append(label("Data: Open-Meteo.com (CC BY 4.0) · places: OpenStreetMap", "tile-sub", wrap=True))
        self.set_child(box)

    def set_unit(self, key, val):
        self.win.conf["units"][key] = val
        save_conf(self.win.conf)
        self.win.refresh_all(True)


# --- window -----------------------------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Weather", default_width=540, default_height=1100)
        self.conf = load_conf()
        self.pages = []
        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)

        page = Adw.NavigationPage(title="Weather", tag="main")
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        places = Gtk.Button(icon_name="view-list-bullet-symbolic", css_classes=["flat"], tooltip_text="Places")
        places.connect("clicked", lambda *_: PlacesSheet(self).present(self))
        hb.pack_start(places)
        settings = Gtk.Button(icon_name="emblem-system-symbolic", css_classes=["flat"], tooltip_text="Settings")
        settings.connect("clicked", lambda *_: SettingsSheet(self).present(self))
        hb.pack_end(settings)
        title = vbox(2, valign=Gtk.Align.CENTER)
        self.title = label("", "city-title", 0.5, False)
        title.append(self.title)
        tv.add_top_bar(hb)
        self.carousel = Adw.Carousel(vexpand=True, allow_scroll_wheel=False)
        self.carousel.connect("page-changed", self.on_page)
        self.dots = Adw.CarouselIndicatorDots(carousel=self.carousel)
        title.append(self.dots)
        hb.set_title_widget(title)
        tv.set_content(self.carousel)
        page.set_child(tv)
        self.nav.add(page)

        self.build_pages()
        notareal_theme.on_change(self.on_theme)
        self.on_theme()
        self.connect("notify::is-active", lambda *_: self.is_active() and self.refresh_all())
        GLib.timeout_add_seconds(30 * 60, lambda: (self.refresh_all(), True)[1])
        self.start_gps()
        if os.environ.get("NRWX_SCROLL"):          # headless screenshots of the lower tiles
            GLib.timeout_add(4000, self._test_scroll)
        if os.environ.get("NRWX_DAY"):
            GLib.timeout_add(4000, lambda: self.nav.push(DayPage(self.pages[0], int(os.environ["NRWX_DAY"]),
                                                                 os.environ["NRWX_DAY"] in ("0", "1"))))
        if os.environ.get("NRWX_SHEETS"):
            GLib.timeout_add(3000, lambda: (AirPage(self.pages[0]), SettingsSheet(self),
                                            PlacesSheet(self).present(self), False)[-1])

    def _test_scroll(self):
        adj = self.pages[0].get_vadjustment()
        adj.set_value(adj.get_upper())
        return False

    def build_pages(self):
        for p in self.pages:
            self.carousel.remove(p)
        self.pages = [PlacePage(self, loc) for loc in self.conf["locations"]]
        for p in self.pages:
            self.carousel.append(p)
        self.dots.set_visible(len(self.pages) > 1)
        n = min(self.conf.get("index", 0), len(self.pages) - 1)
        GLib.idle_add(lambda: self.show_place(n, animate=False) and False)
        self.refresh_all()

    def show_place(self, n, animate=True):
        if 0 <= n < len(self.pages):
            self.carousel.scroll_to(self.pages[n], animate)
            self.title.set_text(self.conf["locations"][n]["name"])
            self.pages[n].refresh()
        return True

    def on_page(self, _c, n):
        self.conf["index"] = n
        save_conf(self.conf)
        self.title.set_text(self.conf["locations"][n]["name"])
        self.pages[n].refresh()

    def refresh_all(self, force=False):
        for p in self.pages:
            p.refresh(force)

    def add_place(self, it):
        self.conf["locations"].append({"name": it["name"], "lat": it["lat"], "lon": it["lon"]})
        save_conf(self.conf)
        self.build_pages()
        GLib.idle_add(lambda: self.show_place(len(self.pages) - 1) and False)

    def remove_place(self, n):
        del self.conf["locations"][n]
        self.conf["index"] = 0
        save_conf(self.conf)
        self.build_pages()

    def on_theme(self):
        (self.add_css_class if notareal_theme.value("corners", "1") == "0" else self.remove_css_class)("square")
        for p in self.pages:
            p.render()

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    # GeoClue: city accuracy is plenty for weather; the last fix stays in the config ---------
    def start_gps(self):
        gps = next((n for n, l in enumerate(self.conf["locations"]) if l.get("gps")), None)
        if gps is None:
            return
        try:
            gi.require_version("Geoclue", "2.0")
            from gi.repository import Geoclue
        except (ValueError, ImportError):
            return

        def ready(_src, res):
            try:
                self._geo = Geoclue.Simple.new_finish(res)
            except GLib.Error as e:
                print("nrweather: geoclue:", e.message, flush=True)
                return
            loc = self._geo.get_location()
            lat, lon = loc.get_property("latitude"), loc.get_property("longitude")
            entry = self.conf["locations"][gps]
            moved = abs(lat - entry["lat"]) > 0.02 or abs(lon - entry["lon"]) > 0.02
            entry.update(lat=round(lat, 4), lon=round(lon, 4))
            if moved or entry["name"] == "Current location":
                def named(name):
                    entry["name"] = name
                    save_conf(self.conf)
                    page = self.pages[gps]
                    page.loc = entry
                    page.data = wx.cached(entry["lat"], entry["lon"])
                    page.refresh(True)
                    if self.carousel.get_position() == gps:
                        self.title.set_text(name)
                bg(lambda: wx.place_name(lat, lon), named)
        Geoclue.Simple.new(APP_ID, Geoclue.AccuracyLevel.CITY, None, ready)


class App(Adw.Application):
    def __init__(self):
        flags = Gio.ApplicationFlags.NON_UNIQUE if os.environ.get("NRWX_TEST") else Gio.ApplicationFlags.DEFAULT_FLAGS
        super().__init__(application_id=APP_ID, flags=flags)    # NRWX_TEST: headless runs beside the real one
        self.win = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        notareal_theme.apply(os.path.join(HERE, "style.css"))

    def do_activate(self):
        if not self.win:
            self.win = Window(self)
        self.win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
