#!/usr/bin/env python3
"""notreal.tube PC backend: one video hub for the phone (YouTube, TikTok, Instagram, Vimeo).

The phone never talks to the video sites. This server
  - keeps the subscription feed warm (YouTube channel RSS every 20 min, TikTok accounts hourly),
  - searches (YouTube through yt-dlp, the others through our SearXNG with site: queries),
  - resolves stream URLs with yt-dlp ahead of time (prefetch) so a tap plays at once,
  - streams: ffmpeg copies the chosen video + audio into one fragmented MP4 piped to the phone
    (starts in about a second, works off Wi-Fi because the PC fetches from the sites); a seek is a new
    /play with t=,
  - fetches SponsorBlock segments, and downloads files for offline viewing.

Runs as the user service videohub (~/.config/systemd/user/videohub.service), 127.0.0.1:8099,
reachable over Tailscale via `tailscale serve --tcp 8099`.

`hub.py --local` is the same code on the phone (127.0.0.1:8098, streaming only): on mobile data
Tailscale can't reach the PC directly and relays through a DERP server at ~0.2 MB/s, so the app
resolves and remuxes on the phone instead and fetches straight from the video site.
The source of truth is ~/tube/hub.py on the phone; deploy to the PC with ~/tube/deploy-hub.
"""
import concurrent.futures
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from yt_dlp import YoutubeDL

LOCAL = "--local" in sys.argv
PORT = 8098 if LOCAL else 8099
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.expanduser("~/.cache/nrtube-hub") if LOCAL else (os.environ.get("VIDEOHUB_DATA") or os.path.join(HERE, "data"))
DL_DIR = os.path.join(DATA, "dl")
SUBS_FILE = os.path.join(DATA, "subs.json")
SEARX = "http://127.0.0.1:8890/search"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
RSS_EVERY = 20 * 60
TIKTOK_EVERY = 60 * 60
RESOLVE_TTL = 2 * 3600          # googlevideo links live ~6 h; keep well inside that
PAGE = 40
FEED_DAYS = 45                  # older RSS entries are left out of the feed
os.makedirs(DL_DIR, exist_ok=True)

SITES = {   # src -> (site: filter for SearXNG, regex a playable result URL must match)
    "tiktok": ("tiktok.com", re.compile(r"https://(?:www\.)?tiktok\.com/@[^/]+/video/\d+")),
    "instagram": ("instagram.com/reel", re.compile(r"https://(?:www\.)?instagram\.com/(?:[^/]+/)?(?:reel|p)/[\w-]+")),
    "vimeo": ("vimeo.com", re.compile(r"https://(?:www\.)?vimeo\.com/(\d{5,})(?:[/?#]|$)")),
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def http_get(url, timeout=15, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def detect_src(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    for name in ("youtube", "tiktok", "instagram", "vimeo"):
        if name in host or (name == "youtube" and "youtu.be" in host):
            return name
    return "web"


def yt_id(url):
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})", url)
    return m and m.group(1)


def canon(url):
    """One spelling per video, so caches and prefetches line up."""
    url = url.strip()
    if (vid := yt_id(url)) and detect_src(url) == "youtube":
        return "https://www.youtube.com/watch?v=" + vid
    if (m := SITES["vimeo"][1].match(url)):
        return "https://vimeo.com/" + m.group(1)
    return url.split("?")[0] if detect_src(url) in ("tiktok", "instagram") else url


# --- subscriptions + feed ----------------------------------------------------------

LOCK = threading.Lock()


def load_subs():
    try:
        with open(SUBS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_subs(subs):
    tmp = SUBS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(subs, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SUBS_FILE)


CHANNEL_ITEMS = {}      # (src, id) -> [items]
CHANNEL_FETCHED = {}    # (src, id) -> time


def yt_rss(cid):
    raw = http_get("https://www.youtube.com/feeds/videos.xml?channel_id=" + cid, timeout=12)
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
          "m": "http://search.yahoo.com/mrss/"}
    root = ET.fromstring(raw)
    out = []
    for e in root.findall("a:entry", ns):
        vid = e.findtext("yt:videoId", namespaces=ns)
        link = (e.find("a:link", ns).get("href") if e.find("a:link", ns) is not None else "")
        stats = e.find("m:group/m:community/m:statistics", ns)
        pub = e.findtext("a:published", namespaces=ns) or ""
        try:
            ts = time.mktime(time.strptime(pub[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        except ValueError:
            ts = 0
        out.append({
            "url": "https://www.youtube.com/watch?v=" + vid, "src": "youtube",
            "title": e.findtext("a:title", namespaces=ns) or "",
            "channel": e.findtext("a:author/a:name", namespaces=ns) or "",
            "channel_id": cid, "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            "published": ts, "views": int(stats.get("views")) if stats is not None and stats.get("views") else None,
            "short": "/shorts/" in link,
        })
    return out


def flat_list(url, n=15):
    with YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
                    "playlist_items": f"1:{n}", "skip_download": True}) as y:
        info = y.extract_info(url, download=False)
    return info


def tiktok_user(handle):
    info = flat_list(f"https://www.tiktok.com/@{handle.lstrip('@')}", 15)
    out = []
    for e in info.get("entries") or []:
        thumbs = e.get("thumbnails") or []
        out.append({"url": e.get("url") or e.get("webpage_url"), "src": "tiktok",
                    "title": (e.get("title") or e.get("description") or "")[:200],
                    "channel": "@" + handle.lstrip("@"), "channel_id": handle.lstrip("@"),
                    "thumb": thumbs[-1]["url"] if thumbs else "", "published": e.get("timestamp") or 0,
                    "views": e.get("view_count"), "duration": e.get("duration")})
    return out


def channel_items(src, cid, force=False):
    key = (src, cid)
    every = RSS_EVERY if src == "youtube" else TIKTOK_EVERY
    if not force and key in CHANNEL_ITEMS and time.time() - CHANNEL_FETCHED.get(key, 0) < every:
        return CHANNEL_ITEMS[key]
    try:
        items = yt_rss(cid) if src == "youtube" else tiktok_user(cid)
        CHANNEL_ITEMS[key] = items
        CHANNEL_FETCHED[key] = time.time()
    except Exception as e:  # noqa: BLE001
        log("channel", src, cid, repr(e)[:120])
        CHANNEL_FETCHED[key] = time.time() - every + 300   # retry in 5 min
    return CHANNEL_ITEMS.get(key, [])


FEED = {"items": [], "built": 0}


def build_feed(force=False):
    subs = load_subs()
    with concurrent.futures.ThreadPoolExecutor(16) as ex:
        lists = list(ex.map(lambda s: channel_items(s["src"], s["id"], force), subs))
    cutoff = time.time() - FEED_DAYS * 86400
    items = [i for lst in lists for i in lst if (i.get("published") or 0) >= cutoff]
    items.sort(key=lambda i: i.get("published") or 0, reverse=True)
    FEED["items"], FEED["built"] = items, time.time()
    log(f"feed: {len(items)} videos from {len(subs)} subscriptions")
    for i in items[:6]:
        prefetch(i["url"])


def feed_loop():
    while True:
        try:
            build_feed()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(300)    # channels themselves only refetch when stale (RSS_EVERY / TIKTOK_EVERY)


# --- search ------------------------------------------------------------------------

def search_youtube(q, n=20):
    info = flat_list("ytsearch%d:%s" % (n, q), n)
    out = []
    for e in info.get("entries") or []:
        if e.get("ie_key") not in (None, "Youtube") or not e.get("id") or len(e["id"]) != 11:
            continue    # channels / playlists in the results
        out.append({"url": "https://www.youtube.com/watch?v=" + e["id"], "src": "youtube",
                    "title": e.get("title") or "", "channel": e.get("channel") or e.get("uploader") or "",
                    "channel_id": e.get("channel_id") or "", "thumb": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg",
                    "duration": e.get("duration"), "views": e.get("view_count"),
                    "live": e.get("live_status") == "is_live"})
    return out


TITLE_JUNK = re.compile(r"\s*[|·•-]\s*(TikTok|Instagram|Vimeo)\s*$|\s+on Vimeo$|^Instagram$", re.I)


def search_site(src, q, n=15):
    site, pat = SITES[src]
    url = SEARX + "?" + urllib.parse.urlencode({"q": f"site:{site} {q}", "format": "json"})
    res = json.loads(http_get(url, timeout=20)).get("results", [])
    out, seen = [], set()
    for r in res:
        m = pat.match(r.get("url", ""))
        if not m:
            continue
        u = canon(m.group(0))
        if u in seen:
            continue
        seen.add(u)
        title = html.unescape(TITLE_JUNK.sub("", r.get("title") or "")).strip()
        handle = re.search(r"/@([^/]+)/", u)
        out.append({"url": u, "src": src, "title": title or (r.get("content") or "")[:120],
                    "channel": "@" + handle.group(1) if handle else "",
                    "channel_id": handle.group(1) if handle else "",
                    "thumb": r.get("thumbnail") or r.get("img_src") or "", "unverified": True})
        if len(out) >= n:
            break
    return out


SEARCH_CACHE = {}      # (src, query) -> (time, items): the search engines rate-limit repeats
SEARCH_TTL = 600


def cached_search(src, q):
    key = (src, q.strip().lower())
    hit = SEARCH_CACHE.get(key)
    if hit and time.time() - hit[0] < SEARCH_TTL:
        return [dict(i) for i in hit[1]]
    items = search_youtube(q) if src == "youtube" else search_site(src, q)
    if items:
        SEARCH_CACHE[key] = (time.time(), items)
    if len(SEARCH_CACHE) > 300:
        for k in sorted(SEARCH_CACHE, key=lambda k: SEARCH_CACHE[k][0])[:100]:
            del SEARCH_CACHE[k]
    return [dict(i) for i in items]


def search(q, src="all"):
    srcs = ["youtube", "tiktok", "instagram", "vimeo"] if src == "all" else [src]
    with concurrent.futures.ThreadPoolExecutor(4) as ex:
        futs = {s: ex.submit(cached_search, s, q) for s in srcs}
        lists = {}
        for s, f in futs.items():
            try:
                lists[s] = f.result(timeout=30)
            except Exception as e:  # noqa: BLE001
                log("search", s, repr(e)[:120])
                lists[s] = []
    # interleave: 3 YouTube, then one from each other site, repeat
    out = []
    while any(lists.values()):
        for s in srcs:
            take = 3 if s == "youtube" and src == "all" else 1
            for _ in range(take):
                if lists[s]:
                    out.append(lists[s].pop(0))
    out = [i for i in out if canon(i["url"]) not in FAILED]
    for i in out[:6]:
        prefetch(i["url"])
    for i in out:          # site results come without thumbnails/durations: resolve them in the background
        if i.get("unverified"):
            prefetch(i["url"], low=True)
    return out


# --- resolving ---------------------------------------------------------------------

RESOLVED = {}            # url -> (time, info)
RESOLVING = {}           # url -> Event
FAILED = {}              # url -> (time, message)
POOL = concurrent.futures.ThreadPoolExecutor(4)
LOW_POOL = concurrent.futures.ThreadPoolExecutor(3)

def video_spec(h):
    """H.264 first (the phone decodes it, the PC can copy it untouched), no bigger than h on the SHORT
    side: portrait TikToks are 576x1024, so a height-only limit skipped them all for an H.265 1080p."""
    vc = "[vcodec~='^(avc1|h264)']"
    alts = []
    for side in ("height", "width"):
        alts += [f"bv*[{side}<={h}]{vc}[protocol=https]+ba[ext=m4a][protocol=https]",
                 f"bv*[{side}<={h}]{vc}+ba[ext=m4a]", f"bv*[{side}<={h}]{vc}+ba", f"b[{side}<={h}]{vc}"]
    ok = "[vcodec~='^(avc1|h264|vp0?9|av01)']"     # the phone decodes these itself (VP9 = Instagram)
    for side in ("height", "width"):
        alts += [f"bv*[{side}<={h}]{ok}+ba", f"b[{side}<={h}]{ok}"]
    alts += [f"b{vc}", f"bv*{vc}+ba", f"bv*[height<={h}]+ba", f"b[height<={h}]", "b", "bv*+ba"]
    return "/".join(alts)


AUDIO_SPEC = "ba[ext=m4a][protocol=https]/ba[ext=m4a]/ba/b"
# visionos: ~1 s to resolve and its links play without a PO token; the default clients' (TV) links got 403
# on 5 of 8 tries (10-22). Default clients stay as the fallback.
YT_CLIENTS = [["visionos"], ["default"]]


def _fetch_url(url):
    if (m := SITES["vimeo"][1].match(url)):
        return "https://player.vimeo.com/video/" + m.group(1)   # vimeo.com pages want a login; the player doesn't
    return url


def resolve(url, wait=True):
    url = canon(url)
    hit = RESOLVED.get(url)
    if hit and time.time() - hit[0] < RESOLVE_TTL:
        return hit[1]
    with LOCK:
        ev = RESOLVING.get(url)
        mine = ev is None
        if mine:
            ev = RESOLVING[url] = threading.Event()
    if not mine:
        if not wait:
            return None
        ev.wait(60)
        hit = RESOLVED.get(url)
        if hit:
            return hit[1]
        raise RuntimeError(FAILED.get(url, (0, "could not load this video"))[1])
    try:
        t = time.time()
        info = None
        for client in (YT_CLIENTS if detect_src(url) == "youtube" else [None]):
            opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
            if client:
                opts["extractor_args"] = {"youtube": {"player_client": client}}
            y = YoutubeDL(opts)
            try:
                info = y.extract_info(_fetch_url(url), download=False)
                if info.get("formats") or info.get("url") or info.get("_type") == "playlist":
                    break
            except Exception:  # noqa: BLE001
                if client is YT_CLIENTS[-1] or client is None:
                    raise
        if info.get("_type") == "playlist":
            info = (info.get("entries") or [None])[0] or info
        info["_ydl"] = y
        info["_sponsor"] = sponsor_segments(url) if detect_src(url) == "youtube" else []
        RESOLVED[url] = (time.time(), info)
        FAILED.pop(url, None)
        log(f"resolved {url[-40:]} in {time.time() - t:.1f}s")
        return info
    except Exception as e:  # noqa: BLE001
        msg = re.sub(r"^ERROR: (\[[^\]]+\] )?[\w-]+: ", "", str(e)).split("\n")[0][:200]
        FAILED[url] = (time.time(), msg)
        log("resolve failed", url, msg)
        raise RuntimeError(msg)
    finally:
        with LOCK:
            RESOLVING.pop(url, None)
        ev.set()


def prefetch(url, low=False):
    url = canon(url)
    hit = RESOLVED.get(url)
    if (hit and time.time() - hit[0] < RESOLVE_TTL - 600) or url in RESOLVING:
        return
    if url in FAILED and time.time() - FAILED[url][0] < 600:
        return
    (LOW_POOL if low else POOL).submit(lambda: _quiet(resolve, url))


def _quiet(fn, *a):
    try:
        fn(*a)
    except Exception:  # noqa: BLE001
        pass


def pick(info, spec):
    """Run yt-dlp's own format selector on an already extracted info dict."""
    y = info["_ydl"]
    formats = info.get("formats") or [info]
    sel = y.build_format_selector(spec)
    ctx = {"formats": formats, "has_merged_format": any(f.get("acodec", "none") != "none" and
                                                         f.get("vcodec", "none") != "none" for f in formats),
           "incomplete_formats": False}
    chosen = next(iter(sel(ctx)), None)
    if not chosen:
        raise RuntimeError("no playable format")
    return chosen.get("requested_formats") or [chosen]


def sponsor_segments(url):
    vid = yt_id(url)
    if not vid:
        return []
    q = urllib.parse.urlencode({"videoID": vid, "categories": json.dumps(["sponsor", "selfpromo", "interaction"])})
    try:
        data = json.loads(http_get("https://sponsor.ajay.app/api/skipSegments?" + q, timeout=4))
        return [[s["segment"][0], s["segment"][1], s["category"]] for s in data]
    except Exception:  # noqa: BLE001  (404 = no segments)
        return []


def heights(info):
    hs = sorted({f["height"] for f in info.get("formats") or [] if f.get("height") and f.get("vcodec", "none") != "none"})
    return hs


def info_json(url):
    info = resolve(url)
    thumb = info.get("thumbnail") or ""
    if detect_src(url) == "youtube" and info.get("id"):
        thumb = f"https://i.ytimg.com/vi/{info['id']}/hqdefault.jpg"
    return {"url": canon(url), "src": detect_src(url), "title": info.get("title") or info.get("description", "")[:120],
            "channel": info.get("channel") or info.get("uploader") or "",
            "channel_id": info.get("channel_id") or info.get("uploader_id") or "",
            "duration": info.get("duration"), "thumb": thumb, "views": info.get("view_count"),
            "published": info.get("timestamp"), "description": (info.get("description") or "")[:4000],
            "heights": heights(info), "sponsor": info.get("_sponsor") or [],
            "live": bool(info.get("is_live")), "width": info.get("width"), "height": info.get("height")}


# --- streaming ---------------------------------------------------------------------

# YouTube throttles one long download to ~2x playback speed (0.3 MB/s on mobile, 10-22: endless buffering)
# but serves `&range=` pieces at full speed (7 MB/s), which is what yt-dlp itself does. So ffmpeg reads
# googlevideo files through /src, which fetches them in pieces and honours ffmpeg's Range seeks.
CHUNK = 10 * 1024 * 1024
SOURCES = {}             # key -> (url, headers, size)


def chunked_source(f):
    if "googlevideo.com" not in f["url"] or not f.get("filesize"):
        return None
    key = hashlib.sha1(f["url"].encode()).hexdigest()[:16]
    SOURCES[key] = (f["url"], {k: v for k, v in (f.get("http_headers") or {}).items()}, int(f["filesize"]))
    if len(SOURCES) > 200:
        for k in list(SOURCES)[:50]:
            del SOURCES[k]
    return f"http://127.0.0.1:{PORT}/src?k={key}"


def ffmpeg_inputs(fmts, t, ydl):
    args = []
    for f in fmts:
        if (local := chunked_source(f)):
            if t > 0:
                args += ["-ss", f"{t:.2f}"]
            args += ["-probesize", "1000000", "-analyzeduration", "1000000", "-i", local]
            continue
        headers = {k: v for k, v in (f.get("http_headers") or {}).items() if k.lower() != "user-agent"}
        cookie = ydl.cookiejar.get_cookie_header(f["url"])    # TikTok's CDN wants the tt_chain_token cookie
        if cookie:
            headers["Cookie"] = cookie
        hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        ua = (f.get("http_headers") or {}).get("User-Agent", UA)
        if t > 0:
            args += ["-ss", f"{t:.2f}"]
        args += ["-user_agent", ua]
        if hdr:
            args += ["-headers", hdr]
        # ffmpeg reads up to 5 MB / 5 s of each input to analyse it before writing anything: seconds on
        # mobile data. MP4/DASH/HLS headers already say what's inside.
        args += ["-probesize", "1000000", "-analyzeduration", "1000000",
                 "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "4", "-i", f["url"]]
    return args


def play_cmd(url, h, t, audio):
    info = resolve(url)
    fmts = pick(info, AUDIO_SPEC if audio else video_spec(h))
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"] + ffmpeg_inputs(fmts, t, info["_ydl"])
    if audio:
        cmd += ["-map", "0:a:0", "-vn"]
    elif len(fmts) == 2:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    vcodec = (fmts[0].get("vcodec") or "") if not audio else ""
    copy_ok = audio or vcodec.startswith(("avc1", "h264", "vp9", "vp09", "av01")) or not vcodec
    if copy_ok:
        cmd += ["-c", "copy"]
    else:   # VP9/H.265-only sources: re-encode, small and fast enough to stay ahead of playback
        cmd += ["-vf", f"scale=-2:'min({h},ih)'", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-crf", "24", "-c:a", "aac", "-b:a", "128k"]
    cmd +=["-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-frag_duration", "500000",
            "pipe:1"]
    return cmd


# --- offline downloads -------------------------------------------------------------

DOWNLOADS = {}           # key -> {"state": queued|running|done|error, "pct": float, "path":, "error":}


def dl_key(url, audio):
    return hashlib.sha1((canon(url) + ("|a" if audio else "|v")).encode()).hexdigest()[:16]


def start_download(url, audio, h=720):
    key = dl_key(url, audio)
    st = DOWNLOADS.get(key)
    path = os.path.join(DL_DIR, key + (".m4a" if audio else ".mp4"))
    if os.path.exists(path):
        DOWNLOADS[key] = {"state": "done", "pct": 100, "path": path}
        return key
    if st and st["state"] in ("queued", "running"):
        return key
    DOWNLOADS[key] = {"state": "queued", "pct": 0}

    def hook(d):
        if d.get("status") == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if tot:
                DOWNLOADS[key]["pct"] = round(100 * d.get("downloaded_bytes", 0) / tot, 1)

    def run():
        DOWNLOADS[key]["state"] = "running"
        try:
            opts = {"quiet": True, "no_warnings": True, "format": AUDIO_SPEC if audio else video_spec(h),
                    "outtmpl": os.path.join(DL_DIR, key + ".%(ext)s"), "progress_hooks": [hook],
                    "merge_output_format": None if audio else "mp4", "noplaylist": True,
                    "postprocessor_args": {"merger": ["-movflags", "+faststart"]}}
            with YoutubeDL(opts) as y:
                y.extract_info(_fetch_url(canon(url)), download=True)
            got = [f for f in os.listdir(DL_DIR) if f.startswith(key + ".") and not f.endswith((".part", ".ytdl"))]
            if not got:
                raise RuntimeError("download produced no file")
            final = os.path.join(DL_DIR, got[0])
            if audio and not final.endswith(".m4a"):
                os.replace(final, path)
                final = path
            DOWNLOADS[key] = {"state": "done", "pct": 100, "path": final}
            log("downloaded", url, final)
        except Exception as e:  # noqa: BLE001
            DOWNLOADS[key] = {"state": "error", "pct": 0, "error": str(e)[:200]}
            log("download failed", url, e)
    LOW_POOL.submit(run)
    return key


def prune_downloads(days=3):
    """Files only wait on the PC until the phone has fetched them."""
    for f in os.listdir(DL_DIR):
        p = os.path.join(DL_DIR, f)
        if time.time() - os.path.getmtime(p) > days * 86400:
            os.remove(p)


# --- HTTP --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
        try:
            if u.path == "/play":
                return self.play(q)
            if u.path == "/file":
                return self.file(q)
            if u.path == "/src":
                return self.src(q)
            fn = ROUTES.get(u.path)
            if not fn:
                return self.reply(404, {"error": "not found"})
            self.reply(200, fn(q))
        except Exception as e:  # noqa: BLE001
            if not isinstance(e, RuntimeError):
                traceback.print_exc()
            self.reply(502, {"error": str(e)})

    def reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def play(self, q):
        url = q.get("u", "")
        t = float(q.get("t") or 0)
        cmd = play_cmd(url, int(q.get("h") or 720), t, q.get("audio") == "1")
        for attempt in (1, 2):
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            first = p.stdout.read1(65536)
            if first:
                break
            err = p.stderr.read().decode(errors="replace")[-300:]
            p.wait()
            RESOLVED.pop(canon(url), None)     # expired/refused links: resolve fresh and try once more
            log(f"play failed (try {attempt})", url, err.strip()[-120:])
            if attempt == 2:
                return self.reply(502, {"error": "stream failed: " + err.strip()})
            cmd = play_cmd(url, int(q.get("h") or 720), t, q.get("audio") == "1")
        self.send_response(200)
        self.send_header("Content-Type", "audio/mp4" if q.get("audio") == "1" else "video/mp4")
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        threading.Thread(target=lambda: p.stderr.read(), daemon=True).start()
        try:
            chunk = first
            while chunk:
                self.wfile.write(chunk)
                chunk = p.stdout.read1(65536)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            p.kill()
            p.wait()

    def src(self, q):
        if q.get("k") not in SOURCES:
            return self.reply(404, {"error": "unknown source"})
        url, headers, size = SOURCES[q["k"]]
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        if m:
            start = int(m.group(1))
            end = min(int(m.group(2)), size - 1) if m.group(2) else end
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        off = start
        try:
            while off <= end:
                last = min(off + CHUNK - 1, end)
                req = urllib.request.Request(f"{url}&range={off}-{last}", headers=headers)
                before = off
                with urllib.request.urlopen(req, timeout=20) as r:
                    while (b := r.read(262144)):
                        self.wfile.write(b)
                        off += len(b)
                if off == before:        # a short piece is asked again from where it stopped; nothing at all = give up
                    break
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as e:  # noqa: BLE001
            log("src", q["k"], repr(e)[:120])

    def file(self, q):
        st = DOWNLOADS.get(q.get("key", ""))
        if not st or st.get("state") != "done":
            return self.reply(404, {"error": "not downloaded"})
        path = st["path"]
        size = os.path.getsize(path)
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else end
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "audio/mp4" if path.endswith(".m4a") else "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass


def r_feed(q):
    if q.get("refresh") == "1":
        build_feed(force=True)
    elif not FEED["built"]:
        build_feed()
    page = int(q.get("page") or 0)
    items = FEED["items"]
    if q.get("shorts") == "0":
        items = [i for i in items if not i.get("short")]
    return {"items": items[page * PAGE:(page + 1) * PAGE], "more": len(items) > (page + 1) * PAGE,
            "built": FEED["built"], "subs": len(load_subs())}


def r_channel(q):
    src, cid = q.get("src", "youtube"), q.get("id", "")
    items = channel_items(src, cid, force=q.get("refresh") == "1")
    for i in items[:4]:
        prefetch(i["url"])
    subbed = any(s["src"] == src and s["id"] == cid for s in load_subs())
    return {"items": items, "subscribed": subbed}


def r_search(q):
    return {"items": search(q.get("q", ""), q.get("src", "all"))}


def r_info(q):
    return info_json(q.get("u", ""))


def r_prefetch(q):
    for u in q.get("u", "").split("\n"):
        if u:
            prefetch(u)
    return {"ok": True}


def r_check(q):
    """Status of background-resolved search results: url -> ok/fail + filled-in fields."""
    out = {}
    for u in q.get("u", "").split("\n"):
        c = canon(u)
        if c in FAILED:
            out[u] = {"ok": False}
        elif c in RESOLVED:
            i = RESOLVED[c][1]
            thumbs = [t for t in i.get("thumbnails") or [] if t.get("url")]
            out[u] = {"ok": True, "duration": i.get("duration"), "title": i.get("title"),
                      "channel": i.get("channel") or i.get("uploader") or "",
                      "thumb": i.get("thumbnail") or (thumbs[-1]["url"] if thumbs else "")}
    return out


def r_subs(q):
    return {"subs": load_subs()}


def r_sub(q):
    src, cid, name = q.get("src", "youtube"), q.get("id", ""), q.get("name", "")
    with LOCK:
        subs = [s for s in load_subs() if not (s["src"] == src and s["id"] == cid)]
        if q.get("on", "1") == "1":
            subs.append({"src": src, "id": cid, "name": name or cid})
        save_subs(subs)
    if q.get("on", "1") == "1":
        threading.Thread(target=build_feed, daemon=True).start()
    else:
        FEED["items"] = [i for i in FEED["items"] if not (i["src"] == src and i.get("channel_id") == cid)]
    return {"ok": True, "subs": len(subs)}


def r_download(q):
    key = start_download(q.get("u", ""), q.get("audio") == "1", int(q.get("h") or 720))
    return {"key": key, **{k: v for k, v in DOWNLOADS[key].items() if k != "path"}}


ROUTES = {"/feed": r_feed, "/channel": r_channel, "/search": r_search, "/info": r_info,
          "/prefetch": r_prefetch, "/check": r_check, "/subs": r_subs, "/sub": r_sub, "/download": r_download,
          "/ping": lambda q: {"ok": True}}


if __name__ == "__main__":
    prune_downloads()
    if not LOCAL:
        threading.Thread(target=feed_loop, daemon=True).start()
    log("listening on", PORT)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()
