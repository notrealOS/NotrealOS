"""Precipitation (doppler) radar for notarealWeather: RainViewer's composite of national weather
radars, the last 2 h in 10-minute frames, looped over a map (libshumate).

Base map = notarealMaps' themed Google tiles (its key + tile session, ~/maps/gmaps.py), so both
apps look alike in every theme; Esri's grey canvas (no key) until that loads, or without a key.

RainViewer's free API serves radar up to zoom 7 and no nowcast (2026), so the map stops at 7.
"""
import datetime
import json
import os
import sys
import urllib.request

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Shumate", "1.0")
from gi.repository import GLib, Gtk, Shumate  # noqa: E402

import notareal_theme as T  # noqa: E402
import draw  # noqa: E402

API = "https://api.rainviewer.com/public/weather-maps.json"
SCHEME = 2            # RainViewer "universal blue" (the free API's colour scheme)
MAX_ZOOM = 7
STEP_MS = 450
HOLD_MS = 1600        # pause on the newest frame


def frames():
    """[(unix time, tile url template)] oldest -> newest"""
    req = urllib.request.Request(API, headers={"User-Agent": "notrealOS-weather/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.load(r)
    host = d["host"]
    return [(f["time"], f"{host}{f['path']}/256/{{z}}/{{x}}/{{y}}/{SCHEME}/1_1.png")
            for f in d["radar"]["past"] + d["radar"].get("nowcast", [])]


def google_tiles():
    """Tile URL template from notarealMaps' Google session (same theme styling), or None."""
    sys.path.insert(0, os.path.expanduser("~/maps"))
    try:
        import gmaps
        key = gmaps.get_key()
        if not key:
            return None
        g = gmaps.Google(key)
        return g.tile_url(g.tile_session())
    except Exception as e:      # noqa: BLE001 - fall back to the Esri canvas
        print("radar: google tiles:", e, flush=True)
        return None


def dark_theme():
    r, g, b = T.rgb("bg")
    return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.5


class Radar(Gtk.Box):
    """Map + loop controls. bg_fetch(work, done) runs `work` off the main loop."""
    def __init__(self, lat, lon, bg_fetch, height=380, zoom=6.0):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.lat, self.lon = lat, lon
        self.frames, self.layers, self.i = [], [], 0
        self.playing, self.timer = True, 0

        self.map = Shumate.Map(vexpand=True)
        self.map.set_size_request(-1, height)
        self.map.add_css_class("radar-map")
        vp = self.map.get_viewport()
        self.base = None
        self.bg_fetch = bg_fetch
        self.attrib = Gtk.Label(label="radar © RainViewer · map © Esri", xalign=0, css_classes=["tick"])
        self._base_layer()
        vp.set_min_zoom_level(3)
        vp.set_max_zoom_level(MAX_ZOOM)
        vp.set_zoom_level(zoom)
        vp.set_location(lat, lon)

        # "you are here" drawn over the map (a MarkerLayer marker stuck in the corner here)
        self.here = draw.Canvas()
        self.here.paint = self._paint_here
        for prop in ("notify::latitude", "notify::longitude", "notify::zoom-level"):
            vp.connect(prop, lambda *_: self.here.queue_draw())
        stack = Gtk.Overlay(child=self.map)
        stack.add_overlay(self.here)

        frame = Gtk.Frame(child=stack, css_classes=["radar-frame"])
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        self.append(frame)

        bar = Gtk.Box(spacing=10)                        # play on the left (left-handed)
        self.play = Gtk.Button(icon_name="media-playback-pause-symbolic", css_classes=["flat", "circular"])
        self.play.connect("clicked", lambda *_: self.toggle())
        bar.append(self.play)
        self.when = Gtk.Label(label="RADAR…", xalign=0, css_classes=["tile-title"])
        bar.append(self.when)
        self.scale = Gtk.Scale(hexpand=True, draw_value=False, sensitive=False)
        self.scale.connect("change-value", self._scrub)
        bar.append(self.scale)
        self.append(bar)
        self.append(self.attrib)

        self.connect("unmap", lambda *_: self._stop())
        self.connect("map", self._mapped)
        T.on_change(self._base_layer)
        bg_fetch(frames, self._got)

    def _base_layer(self):
        """Esri grey canvas right away (keeps the bottom slot), then swap in the themed Google tiles."""
        style = "Dark" if dark_theme() else "Light"
        src = Shumate.RasterRenderer.new_full_from_url(
            "esri-gray-" + style, "Esri", "© Esri, HERE, Garmin, OpenStreetMap", "https://www.esri.com/",
            0, 16, 256, Shumate.MapProjection.MERCATOR,
            "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/"
            f"World_{style}_Gray_Base/MapServer/tile/{{z}}/{{y}}/{{x}}")
        self._swap_base(Shumate.MapLayer.new(src, self.map.get_viewport()), src)
        self.bg_fetch(google_tiles, self._got_google)

    def _swap_base(self, layer, src):
        self.map.set_map_source(src)      # the viewport's reference source: lat/lon <-> pixels
        if self.base:
            self.map.insert_layer_behind(layer, self.base)
            self.map.remove_layer(self.base)
        else:
            self.map.add_layer(layer)
        self.base = layer

    def _got_google(self, url):
        if not url:
            return
        src = Shumate.RasterRenderer.new_full_from_url(
            "google-" + T.active(), "Google", "© Google", "https://www.google.com/maps",
            0, 22, 256, Shumate.MapProjection.MERCATOR, url)
        self._swap_base(Shumate.MapLayer.new(src, self.map.get_viewport()), src)
        self.attrib.set_text("radar © RainViewer · map © Google")

    def _paint_here(self, snap, w, h):
        x, y = self.map.get_viewport().location_to_widget_coords(self.map, self.lat, self.lon)
        if 0 <= x <= w and 0 <= y <= h:
            draw.fill(snap, draw.circle(x, y, 8), draw.ink())
            draw.fill(snap, draw.circle(x, y, 4.5), draw.accent())

    def _mapped(self, *_):
        # centre once the map has a size (set_location before allocation leaves us top-left)
        GLib.idle_add(lambda: self.map.center_on(self.lat, self.lon) and False)
        if self.playing:
            self._schedule(STEP_MS)

    def _got(self, fr):
        if not fr:
            self.when.set_text("NO RADAR")
            return
        self.frames = fr
        vp = self.map.get_viewport()
        for t, url in fr:
            src = Shumate.RasterRenderer.new_full_from_url(
                f"rainviewer-{t}", "RainViewer", "© RainViewer", "https://www.rainviewer.com/",
                0, MAX_ZOOM, 256, Shumate.MapProjection.MERCATOR, url)
            layer = Shumate.MapLayer.new(src, vp)
            layer.set_opacity(0)
            self.map.add_layer(layer)
            self.layers.append(layer)
        self.scale.set_range(0, len(fr) - 1)
        self.scale.set_increments(1, 1)
        self.scale.set_sensitive(True)
        self.i = len(fr) - 1
        self._show(self.i)
        if self.playing:
            self._schedule(HOLD_MS)

    def _show(self, i):
        for n, layer in enumerate(self.layers):
            layer.set_opacity(0.75 if n == i else 0)
        t = datetime.datetime.fromtimestamp(self.frames[i][0])
        ago = round((datetime.datetime.now() - t).total_seconds() / 60)
        self.when.set_text(f"{t:%H:%M}" + ("  NOW" if ago < 12 else f"  −{ago} MIN"))
        self.scale.set_value(i)

    def _tick(self):
        self.timer = 0
        if not self.layers:
            return False
        self.i = (self.i + 1) % len(self.layers)
        self._show(self.i)
        self._schedule(HOLD_MS if self.i == len(self.layers) - 1 else STEP_MS)
        return False

    def _schedule(self, ms):
        self._stop()
        if self.layers:
            self.timer = GLib.timeout_add(ms, self._tick)

    def _stop(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0

    def toggle(self):
        self.playing = not self.playing
        self.play.set_icon_name("media-playback-pause-symbolic" if self.playing else "media-playback-start-symbolic")
        if self.playing:
            self._schedule(STEP_MS)
        else:
            self._stop()

    def _scrub(self, _s, _scroll, value):
        if self.layers:
            if self.playing:
                self.toggle()
            self.i = max(0, min(len(self.layers) - 1, int(round(value))))
            self._show(self.i)
        return False
