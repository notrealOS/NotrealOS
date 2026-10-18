"""Minimal Subsonic/OpenSubsonic client for Navidrome."""
import hashlib
import secrets
import requests

CLIENT = "spin"
API_VERSION = "1.16.1"


class ApiError(Exception):
    pass


class Offline(ApiError):
    pass


def make_token(password):
    salt = secrets.token_hex(6)
    return hashlib.md5((password + salt).encode()).hexdigest(), salt


class Subsonic:
    def __init__(self, server, user, token, salt):
        self.server = server.rstrip("/")
        self.user, self.token, self.salt = user, token, salt
        self.http = requests.Session()

    @classmethod
    def login(cls, server, user, password):
        token, salt = make_token(password)
        api = cls(server, user, token, salt)
        api.call("ping")
        return api

    def params(self, **kw):
        p = {"u": self.user, "t": self.token, "s": self.salt,
             "v": API_VERSION, "c": CLIENT, "f": "json"}
        p.update({k: v for k, v in kw.items() if v is not None})
        return p

    def url(self, endpoint, **kw):
        req = requests.Request("GET", f"{self.server}/rest/{endpoint}", params=self.params(**kw))
        return req.prepare().url

    def call(self, endpoint, timeout=12, **kw):
        try:
            r = self.http.get(f"{self.server}/rest/{endpoint}", params=self.params(**kw), timeout=timeout)
        except requests.RequestException as e:
            raise Offline(str(e)) from e
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code}")
        body = r.json().get("subsonic-response", {})
        if body.get("status") != "ok":
            raise ApiError(body.get("error", {}).get("message", "unknown error"))
        return body

    # --- library -------------------------------------------------------
    def album_list(self, kind="newest", size=100, offset=0):
        return self.call("getAlbumList2", type=kind, size=size, offset=offset) \
            .get("albumList2", {}).get("album", [])

    def album(self, album_id):
        return self.call("getAlbum", id=album_id)["album"]

    def artists(self):
        out = []
        for idx in self.call("getArtists")["artists"].get("index", []):
            out.extend(idx.get("artist", []))
        return out

    def artist(self, artist_id):
        return self.call("getArtist", id=artist_id)["artist"]

    def playlists(self):
        return self.call("getPlaylists")["playlists"].get("playlist", [])

    def playlist(self, pl_id):
        return self.call("getPlaylist", id=pl_id)["playlist"]

    def search(self, query):
        r = self.call("search3", query=query, artistCount=10, albumCount=20, songCount=40)
        return r.get("searchResult3", {})

    def lyrics(self, song_id):
        r = self.call("getLyricsBySongId", id=song_id)
        lst = r.get("lyricsList", {}).get("structuredLyrics", [])
        return lst[0] if lst else None

    def scrobble(self, song_id, submission, time_ms=None):
        self.call("scrobble", id=song_id, submission=str(submission).lower(), time=time_ms)

    def similar(self, song_id, count=50):
        """Songs like this one: Navidrome answers via its agents (AudioMuse-AI on this server)."""
        r = self.call("getSimilarSongs2", id=song_id, count=count, timeout=60)
        return r.get("similarSongs2", {}).get("song", [])

    def create_playlist(self, name, song_ids):
        return self.call("createPlaylist", name=name, songId=list(song_ids)).get("playlist", {})

    def starred(self):
        return self.call("getStarred2").get("starred2", {})

    def star(self, kind, item_id, on=True):
        key = {"song": "id", "album": "albumId", "artist": "artistId"}[kind]
        self.call("star" if on else "unstar", **{key: item_id})

    def set_rating(self, song_id, rating):
        """1-5 stars, 0 = remove the rating."""
        self.call("setRating", id=song_id, rating=rating)

    # --- media ---------------------------------------------------------
    def stream_url(self, song_id, fmt=None, max_bitrate=None):
        kw = {}
        if fmt and fmt != "raw":
            kw["format"] = fmt
            kw["maxBitRate"] = max_bitrate
        else:
            kw["format"] = "raw"
        return self.url("stream", id=song_id, **kw)

    def cover_url(self, cover_id, size=None):
        return self.url("getCoverArt", id=cover_id, size=size)
