"""GIF search for the composer: Tenor (Google) or GIPHY, whichever key is configured.

~/.config/notarealchat/config.json: {"tenor_key": "..."} or {"giphy_key": "..."}.
Results: [{"id", "preview" (small still/gif URL), "mp4" (URL to send), "w", "h"}].
"""
import json
import os

import requests

CONFIG = os.path.expanduser("~/.config/notarealchat/config.json")
RECENT = os.path.expanduser("~/.local/share/notarealchat/recent-gifs.json")


def config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def available():
    c = config()
    return bool(c.get("tenor_key") or c.get("giphy_key"))


def search(query, limit=24):
    c = config()
    if c.get("tenor_key"):
        return _tenor(c["tenor_key"], query, limit)
    if c.get("giphy_key"):
        return _giphy(c["giphy_key"], query, limit)
    raise RuntimeError("no GIF key")


def _tenor(key, query, limit):
    path = "search" if query else "featured"
    params = {"key": key, "client_key": "notarealchat", "limit": limit,
              "media_filter": "tinygifpreview,nanogifpreview,mp4,tinymp4", "contentfilter": "medium"}
    if query:
        params["q"] = query
    r = requests.get("https://tenor.googleapis.com/v2/" + path, params=params, timeout=15)
    r.raise_for_status()
    out = []
    for g in r.json().get("results", []):
        f = g.get("media_formats", {})
        prev = f.get("tinygifpreview") or f.get("nanogifpreview") or {}
        vid = f.get("mp4") or f.get("tinymp4") or {}
        if prev.get("url") and vid.get("url"):
            dims = vid.get("dims") or [0, 0]
            out.append({"id": "tenor-" + g["id"], "preview": prev["url"], "mp4": vid["url"],
                        "w": dims[0], "h": dims[1]})
    return out


def _giphy(key, query, limit):
    path = "search" if query else "trending"
    params = {"api_key": key, "limit": limit, "rating": "pg-13"}
    if query:
        params["q"] = query
    r = requests.get("https://api.giphy.com/v1/gifs/" + path, params=params, timeout=15)
    r.raise_for_status()
    out = []
    for g in r.json().get("data", []):
        im = g.get("images", {})
        prev = im.get("fixed_width_still") or im.get("fixed_width_small_still") or {}
        vid = im.get("fixed_width") or im.get("original") or {}
        if prev.get("url") and vid.get("mp4"):
            out.append({"id": "giphy-" + g["id"], "preview": prev["url"], "mp4": vid["mp4"],
                        "w": int(vid.get("width") or 0), "h": int(vid.get("height") or 0)})
    return out


def recent():
    try:
        with open(RECENT) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def remember(gif):
    items = [g for g in recent() if g["id"] != gif["id"]]
    items.insert(0, gif)
    os.makedirs(os.path.dirname(RECENT), exist_ok=True)
    with open(RECENT, "w") as f:
        json.dump(items[:30], f)
