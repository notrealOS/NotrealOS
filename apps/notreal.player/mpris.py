"""MPRIS2 so Phosh's lock screen / quick settings show Spin's media controls."""
import threading

from gi.repository import Gio, GLib

XML = """
<node>
 <interface name="org.mpris.MediaPlayer2">
  <method name="Raise"/><method name="Quit"/>
  <property name="CanQuit" type="b" access="read"/>
  <property name="CanRaise" type="b" access="read"/>
  <property name="HasTrackList" type="b" access="read"/>
  <property name="Identity" type="s" access="read"/>
  <property name="DesktopEntry" type="s" access="read"/>
  <property name="SupportedUriSchemes" type="as" access="read"/>
  <property name="SupportedMimeTypes" type="as" access="read"/>
 </interface>
 <interface name="org.mpris.MediaPlayer2.Player">
  <method name="Next"/><method name="Previous"/><method name="Pause"/>
  <method name="PlayPause"/><method name="Stop"/><method name="Play"/>
  <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
  <method name="SetPosition"><arg direction="in" name="TrackId" type="o"/>
   <arg direction="in" name="Position" type="x"/></method>
  <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
  <signal name="Seeked"><arg name="Position" type="x"/></signal>
  <property name="PlaybackStatus" type="s" access="read"/>
  <property name="LoopStatus" type="s" access="readwrite"/>
  <property name="Rate" type="d" access="readwrite"/>
  <property name="Shuffle" type="b" access="readwrite"/>
  <property name="Metadata" type="a{sv}" access="read"/>
  <property name="Volume" type="d" access="readwrite"/>
  <property name="Position" type="x" access="read"/>
  <property name="MinimumRate" type="d" access="read"/>
  <property name="MaximumRate" type="d" access="read"/>
  <property name="CanGoNext" type="b" access="read"/>
  <property name="CanGoPrevious" type="b" access="read"/>
  <property name="CanPlay" type="b" access="read"/>
  <property name="CanPause" type="b" access="read"/>
  <property name="CanSeek" type="b" access="read"/>
  <property name="CanControl" type="b" access="read"/>
 </interface>
</node>"""

ROOT, PLAYER = "org.mpris.MediaPlayer2", "org.mpris.MediaPlayer2.Player"
LOOP = {"off": "None", "all": "Playlist", "one": "Track"}


class Mpris:
    def __init__(self, app, player):
        self.app, self.player = app, player
        self.art = ""
        self.conn = None
        self.node = Gio.DBusNodeInfo.new_for_xml(XML)
        Gio.bus_own_name(Gio.BusType.SESSION, "org.mpris.MediaPlayer2.notarealPlayer",
                         Gio.BusNameOwnerFlags.NONE, self._on_bus, None, None)
        player.connect("state-changed", lambda *a: self.changed(PLAYER, "PlaybackStatus", "LoopStatus", "Shuffle"))
        player.connect("track-changed", lambda *a: self.changed(PLAYER, "Metadata"))

    def _on_bus(self, conn, _name):
        self.conn = conn
        for iface in self.node.interfaces:
            conn.register_object("/org/mpris/MediaPlayer2", iface, self._call, self._get, self._set)

    def set_art(self, path):
        self.art = "file://" + path if path else ""
        self.changed(PLAYER, "Metadata")

    # --- properties ---------------------------------------------------------
    def _props(self, iface):
        p = self.player
        if iface == ROOT:
            return {
                "CanQuit": GLib.Variant("b", True), "CanRaise": GLib.Variant("b", True),
                "HasTrackList": GLib.Variant("b", False), "Identity": GLib.Variant("s", "notreal.player"),
                "DesktopEntry": GLib.Variant("s", "dev.notareal.Player"),
                "SupportedUriSchemes": GLib.Variant("as", []), "SupportedMimeTypes": GLib.Variant("as", []),
            }
        status = "Playing" if p.playing else ("Paused" if p.current else "Stopped")
        return {
            "PlaybackStatus": GLib.Variant("s", status),
            "LoopStatus": GLib.Variant("s", LOOP[p.s["repeat"]]),
            "Rate": GLib.Variant("d", 1.0), "MinimumRate": GLib.Variant("d", 1.0),
            "MaximumRate": GLib.Variant("d", 1.0),
            "Shuffle": GLib.Variant("b", p.s["shuffle"]),
            "Metadata": GLib.Variant("a{sv}", self._metadata()),
            "Volume": GLib.Variant("d", p.s["volume"]),
            "Position": GLib.Variant("x", int(p.position() * 1e6)),
            "CanGoNext": GLib.Variant("b", True), "CanGoPrevious": GLib.Variant("b", True),
            "CanPlay": GLib.Variant("b", True), "CanPause": GLib.Variant("b", True),
            "CanSeek": GLib.Variant("b", True), "CanControl": GLib.Variant("b", True),
        }

    def _metadata(self):
        t = self.player.current
        if not t:
            return {"mpris:trackid": GLib.Variant("o", "/org/mpris/MediaPlayer2/TrackList/NoTrack")}
        md = {
            "mpris:trackid": GLib.Variant("o", "/dev/notareal/player/t" + "".join(c for c in t["id"] if c.isalnum())),
            "xesam:title": GLib.Variant("s", t.get("title", "")),
            "xesam:artist": GLib.Variant("as", [t.get("artist", "")]),
            "xesam:album": GLib.Variant("s", t.get("album", "")),
            "mpris:length": GLib.Variant("x", int((t.get("duration") or 0) * 1e6)),
        }
        if self.art:
            md["mpris:artUrl"] = GLib.Variant("s", self.art)
        return md

    def changed(self, iface, *names):
        if not self.conn:
            return
        props = self._props(iface)
        changed = {n: props[n] for n in names}
        self.conn.emit_signal(None, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties",
                              "PropertiesChanged", GLib.Variant("(sa{sv}as)", (iface, changed, [])))

    def seeked(self):
        if self.conn:
            self.conn.emit_signal(None, "/org/mpris/MediaPlayer2", PLAYER, "Seeked",
                                  GLib.Variant("(x)", (int(self.player.position() * 1e6),)))

    def _get(self, _conn, _sender, _path, iface, prop):
        return self._props(iface).get(prop)

    def _set(self, _conn, _sender, _path, _iface, prop, value):
        p = self.player
        if prop == "Volume":
            p.set_volume(max(0.0, min(1.0, value.unpack())))
        elif prop == "Shuffle" and value.unpack() != p.s["shuffle"]:
            p.toggle_shuffle()
        elif prop == "LoopStatus":
            want = {v: k for k, v in LOOP.items()}.get(value.unpack(), "off")
            p.s.set("repeat", want)
            p.emit("state-changed")
        return True

    def _call(self, _conn, _sender, _path, _iface, method, params, inv):
        p = self.player
        if method == "Raise":
            self.app.activate()
        elif method == "Quit":
            self.app.quit()
        elif method in ("Play", "Pause", "PlayPause", "Next", "Previous"):
            {"Play": p.play, "Pause": p.pause, "PlayPause": p.toggle,
             "Next": p.next, "Previous": p.previous}[method]()
        elif method == "Stop":
            p.stop()
        elif method == "Seek":
            p.seek(p.position() + params.unpack()[0] / 1e6)
            self.seeked()
        elif method == "SetPosition":
            p.seek(params.unpack()[1] / 1e6)
            self.seeked()
        elif method == "OpenUri":
            self.open_uri(params.unpack()[0])
        inv.return_value(None)

    # spin:play?q=<words> (used by voiced): search the library, play the best match
    def open_uri(self, uri):
        import urllib.parse
        u = urllib.parse.urlparse(uri)
        q = urllib.parse.parse_qs(u.query).get("q", [""])[0].strip()
        if u.scheme != "spin" or not q:
            return
        p = self.player
        api = p.store.api
        if not api:
            return

        def norm(t):
            return " ".join("".join(c for c in (t or "").lower() if c.isalnum() or c == " ").split())

        filler = {"some", "music", "songs", "song", "by", "the", "album", "artist", "from", "a", "me", "please"}

        def genre_tracks(nq):
            gs = api.call("getGenres").get("genres", {}).get("genre", [])
            g = next((x["value"] for x in gs if norm(x["value"]) == nq), None) or \
                next((x["value"] for x in gs if nq and nq in norm(x["value"])), None)
            if not g:
                return []
            return api.call("getSongsByGenre", genre=g, count=200).get("songsByGenre", {}).get("song", [])

        def work():
            try:
                nq = " ".join(w for w in norm(q).split() if w not in filler) or norm(q)
                r = api.search(nq)
                artists, albums, songs = r.get("artist", []), r.get("album", []), r.get("song", [])
                tracks, shuffle = [], None
                a = next((x for x in artists if norm(x["name"]) == nq), None)
                al = next((x for x in albums if norm(x["name"]) == nq or
                           norm(x["name"] + " " + x.get("artist", "")) == nq), None)
                if a:                       # "play radiohead": the whole artist, shuffled
                    for alb in api.artist(a["id"]).get("album", []):
                        tracks += api.album(alb["id"]).get("song", [])
                    shuffle = True
                elif al:                    # "play ok computer": the album in order
                    tracks, shuffle = api.album(al["id"]).get("song", []), False
                elif songs:                 # anything else: matching songs, best first
                    tracks = songs
                elif artists:
                    for alb in api.artist(artists[0]["id"]).get("album", []):
                        tracks += api.album(alb["id"]).get("song", [])
                    shuffle = True
                else:                       # "play some jazz": a genre, shuffled
                    tracks, shuffle = genre_tracks(nq), True
                print("open_uri:", q, "->", len(tracks), "tracks", flush=True)
                if tracks:
                    GLib.idle_add(lambda: (p.play_tracks(tracks, shuffle=shuffle), False)[1])
            except Exception as e:  # noqa: BLE001
                print("open_uri failed:", e, flush=True)
        threading.Thread(target=work, daemon=True).start()
