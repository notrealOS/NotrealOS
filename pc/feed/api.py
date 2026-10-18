#!/usr/bin/env python3
"""Small JSON API in front of Redlib for the phone's notreal.feed app.

Redlib (docker, :8095) does the Reddit talking; this turns its HTML into JSON with
Pango markup bodies, so the phone needs no HTML parser. Media URLs stay Redlib paths
(/img/..., /preview/..., /hls/...); the app prefixes the Redlib base itself.

  GET /feed?subs=linux+pics&sort=hot&t=&after=t3_x   -> {posts, after}
  GET /post?path=/r/x/comments/id/[cid]&sort=top     -> {post, comments}
  GET /subs?q=keyboards                              -> {subs}
  GET /img?src=/img/x.jpg&w=720                      -> JPEG scaled to width w (phone RAM)
  GET /video?src=/hls/<id>/HLSPlaylist.m3u8?...      -> plain MP4 (the phone's GStreamer
      crashes in libsoup on HLS, so ffmpeg remuxes it here once and we serve the file)
"""
import collections
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bs4 import BeautifulSoup, NavigableString, Tag

REDLIB = "http://127.0.0.1:8095"
PORT = 8096
CACHE_S = 90
_cache = {}


def fetch(path):
    hit = _cache.get(path)
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    req = urllib.request.Request(REDLIB + path, headers={"User-Agent": "notreal.feed"})
    with urllib.request.urlopen(req, timeout=25) as r:
        html = r.read().decode("utf-8", "replace")
    if 'class="post' in html:  # don't cache Redlib error pages
        _cache[path] = (time.time(), html)
    if len(_cache) > 200:
        for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:100]:
            _cache.pop(k, None)
    return html


def soup(html):
    return BeautifulSoup(html, "lxml")


def num(s):
    try:
        return int(re.sub(r"[^\d-]", "", s or ""))
    except ValueError:
        return None


def txt(el):
    return el.get_text(" ", strip=True) if el else ""


# --- HTML -> Pango markup -----------------------------------------------------

def markup(el, images=None):
    """Reddit body HTML -> Pango markup a Gtk.Label can show (links stay clickable)."""
    if el is None:
        return ""
    out = _mk(el, images if images is not None else [], 0)
    out = re.sub(r"[ \t]*\n[ \t]*", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _wrap(tag, inner):
    return f"<{tag}>{inner}</{tag}>" if inner.strip() else inner


def _mk(el, images, depth):
    if isinstance(el, NavigableString):
        if el.__class__.__name__ in ("Comment", "Doctype"):
            return ""
        return escape(re.sub(r"\s+", " ", str(el)), quote=False)
    if not isinstance(el, Tag):
        return ""
    name = el.name
    kids = lambda: "".join(_mk(c, images, depth) for c in el.children)  # noqa: E731
    if name in ("script", "style", "svg"):
        return ""
    if name == "br":
        return "\n"
    if name == "hr":
        return "\n――――――\n"
    if name == "p":
        return kids().strip() + "\n\n"
    if name in ("strong", "b"):
        return _wrap("b", kids())
    if name in ("em", "i"):
        return _wrap("i", kids())
    if name in ("del", "s", "strike"):
        return _wrap("s", kids())
    if name == "sup":
        return _wrap("sup", kids())
    if name == "code":
        return "<tt>" + escape(el.get_text(), quote=False) + "</tt>"
    if name == "pre":
        return "<tt>" + escape(el.get_text().rstrip("\n"), quote=False) + "</tt>\n\n"
    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        return "<b><big>" + kids().strip() + "</big></b>\n\n"
    if name == "blockquote":
        inner = kids().strip()
        lines = inner.split("\n")
        return "\n".join("<span foreground='#f7a1e8'>▎</span><i>" + l + "</i>" if l.strip() else l
                         for l in lines) + "\n\n"
    if name in ("ul", "ol"):
        items, n = [], 0
        for li in el.find_all("li", recursive=False):
            n += 1
            mark = f"{n}." if name == "ol" else "•"
            body = "".join(_mk(c, images, depth + 1) for c in li.children).strip()
            items.append("   " * depth + f"{mark} " + body)
        return "\n".join(items) + "\n\n"
    if name == "li":
        return "• " + kids().strip() + "\n"
    if name == "table":
        rows = []
        for tr in el.find_all("tr"):
            rows.append(" │ ".join(escape(c.get_text(" ", strip=True), quote=False)
                                   for c in tr.find_all(["td", "th"])))
        return "<tt>" + "\n".join(rows) + "</tt>\n\n"
    if name == "img":
        src = el.get("src") or ""
        if src and "/emoji/" not in src:
            images.append(src)
            return "<a href='" + escape(src) + "'>[image]</a>"
        return ""
    if name == "span" and "md-spoiler-text" in (el.get("class") or []):
        return "<span background='#3a3f6b' foreground='#3a3f6b'>" + kids() + "</span>"
    if name == "a":
        href = el.get("href") or ""
        img = el.find("img")
        if img is not None:
            src = href if href.startswith(("/img/", "/preview/")) else (img.get("src") or href)
            images.append(src)
            return "<a href='" + escape(src) + "'>[image]</a>"
        inner = kids()
        if not href or href.startswith("#") or not inner.strip():
            return inner
        return "<a href='" + escape(href) + "'>" + inner + "</a>"
    return kids()


# --- listing ------------------------------------------------------------------

def media_of(post):
    """Inline media of a listing post (or the highlighted post on its own page)."""
    gal = post.select_one(".gallery")
    if gal:
        imgs = []
        for fig in gal.select("figure"):
            a = fig.select_one("a")
            img = fig.select_one("img")
            cap = txt(fig.select_one("figcaption"))
            src = (a.get("href") if a else None) or (img.get("src") if img else None)
            if src:
                imgs.append({"src": src, "caption": cap})
        if imgs:
            return {"type": "gallery", "images": imgs}
    v = post.select_one("video")
    if v:
        src = v.get("src") or ""
        hls = ""
        for s in v.select("source"):
            if "mpegurl" in (s.get("type") or "") and s.get("src"):
                hls = s["src"]
            elif s.get("src") and not src:
                src = s["src"]
        return {"type": "gif" if ("format=mp4" in src and not hls) else "video",
                "src": hls or src, "mp4": src, "poster": v.get("poster") or "",
                "w": num(v.get("width")), "h": num(v.get("height"))}
    a = post.select_one("a.post_media_image")
    if a:
        svg = a.select_one("svg")
        return {"type": "image", "src": a.get("href"),
                "w": num(svg.get("width")) if svg else None,
                "h": num(svg.get("height")) if svg else None}
    return None


def parse_post(p, full=False):
    hdr = p.select_one(".post_header")
    title_el = p.select_one(".post_title")
    flair_el = title_el.select_one(".post_flair") if title_el else None
    links = [a for a in title_el.select("a") if "post_flair" not in (a.get("class") or [])] if title_el else []
    if full:
        t = title_el.find_all(string=True, recursive=False) if title_el else []
        title = " ".join(s.strip() for s in t if s.strip())
        permalink = ""
    else:
        title = txt(links[0]) if links else txt(title_el)
        permalink = links[0].get("href") if links else ""
    flair = None
    if flair_el:
        m = re.search(r"background:\s*([^;\"]+)", flair_el.get("style") or "")
        flair = {"text": txt(flair_el), "bg": m.group(1).strip() if m else None}
    created = hdr.select_one(".created") if hdr else None
    score_el = p.select_one(".post_score")
    thumb_a = p.select_one("a.post_thumbnail")
    thumb, url, domain = None, None, None
    if thumb_a:
        im = thumb_a.select_one("image")
        thumb = im.get("href") if im else None
        href = thumb_a.get("href") or ""
        domain = txt(thumb_a.select_one("span"))
        if href and not href.startswith("/r/"):
            url = href
    post_url = p.select_one("#post_url")
    if post_url:
        url = post_url.get("href")
    body_el = p.select_one(".post_body")
    images = []
    body = markup(body_el, images) if body_el else ""
    classes = p.get("class") or []
    cm = p.select_one(".post_comments")
    out = {
        "id": p.get("id") or "",
        "sub": txt(hdr.select_one(".post_subreddit")) if hdr else "",
        "author": txt(hdr.select_one(".post_author")) if hdr else "",
        "created": created.get("title") if created else "",
        "age": txt(created),
        "title": title,
        "flair": flair,
        "score": num(score_el.get("title")) if score_el else None,
        "score_text": (score_el.find(string=True, recursive=False) or "").strip() if score_el else "",
        "comments": num(cm.get("title")) if cm else None,
        "permalink": permalink,
        "url": url,
        "domain": domain,
        "thumb": thumb,
        "media": media_of(p),
        "body": body,
        "body_images": images,
        "nsfw": bool(p.select_one(".nsfw")),
        "stickied": "stickied" in classes,
    }
    if not out["media"] and domain == "gallery":
        out["media"] = {"type": "gallery", "images": []}   # listing: only the thumbnail
    if full and not out["media"] and url and re.search(r"\.(jpe?g|png|gif|webp)(\?|$)", url, re.I):
        out["media"] = {"type": "image", "src": url}
    return out


CHUNK = 100   # Reddit combines at most ~100 subreddits in one r/a+b+... listing


def feed(q):
    subs = re.sub(r"[^A-Za-z0-9_+]", "", q.get("subs", "popular").replace(" ", "+")) or "popular"
    names = [n for n in subs.split("+") if n]
    if len(names) > CHUNK:
        return feed_chunked(names, q)
    return feed_one(subs, q)


def feed_chunked(names, q):
    """More subs than one listing takes: fetch groups of CHUNK in parallel, interleave the posts.
    The paging token is "<group>:<after>,..." so each group continues where it stopped."""
    groups = ["+".join(names[i:i + CHUNK]) for i in range(0, len(names), CHUNK)]
    want = {}
    if q.get("after"):
        for part in q["after"].split(","):
            i, _, a = part.partition(":")
            if i.isdigit() and a:
                want[int(i)] = a
    todo = [i for i in range(len(groups)) if not q.get("after") or i in want]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        res = dict(zip(todo, ex.map(lambda i: feed_one(groups[i], dict(q, after=want.get(i, ""))), todo)))
    lists = [res[i]["posts"] for i in todo]
    posts, seen = [], set()
    for k in range(max((len(x) for x in lists), default=0)):   # round-robin keeps each group's ranking
        for x in lists:
            if k < len(x) and x[k]["id"] not in seen:
                seen.add(x[k]["id"])
                posts.append(x[k])
    after = ",".join(f"{i}:{res[i]['after']}" for i in todo if res[i].get("after")) or None
    err = "" if posts else next((res[i]["error"] for i in todo if res[i].get("error")), "no posts")
    return {"posts": posts, "after": after, "error": err}


def feed_one(subs, q):
    params = {"sort": q.get("sort", "hot"), "t": q.get("t", "")}
    if q.get("after"):
        params["after"] = q["after"]
    s = soup(fetch(f"/r/{subs}?" + urllib.parse.urlencode(params)))
    posts = [parse_post(p) for p in s.select("#posts > div.post")]
    after = None
    for a in s.select("footer a, #nav a, a"):
        m = re.search(r"after=(t3_\w+)", a.get("href") or "")
        if m:
            after = m.group(1)
    err = (txt(s.select_one("#error h1")) or txt(s.select_one("title")) or "no posts") if not posts else ""
    out = {"posts": posts, "after": after, "error": err}
    if "+" not in subs and not q.get("after"):
        out["about"] = about_sub(s)
    return out


def about_sub(s):
    meta = s.select_one("#sub_meta")
    if not meta:
        return None
    icon = meta.select_one("#sub_icon")
    nums = meta.select("#sub_details > div")
    return {"title": txt(meta.select_one("#sub_title")), "name": txt(meta.select_one("#sub_name")),
            "icon": icon.get("src") if icon else None,
            "desc": txt(meta.select_one("#sub_description"))[:600],
            "members": num(nums[0].get("title")) if nums else None,
            "members_text": txt(nums[0]) if nums else "",
            "active_text": txt(nums[1]) if len(nums) > 1 else ""}


# --- users --------------------------------------------------------------------

def user(q):
    name = re.sub(r"[^A-Za-z0-9_-]", "", q.get("name", ""))
    if not name:
        raise ValueError("no user")
    tab = q.get("tab") if q.get("tab") in ("comments", "submitted") else "overview"
    params = {"sort": q.get("sort", "new"), "t": q.get("t", "")}
    if q.get("after"):
        params["after"] = q["after"]
    s = soup(fetch(f"/user/{name}/{tab}?" + urllib.parse.urlencode(params)))
    items = []
    box = s.select_one("#posts")
    for el in (box.find_all("div", recursive=False) if box else []):
        cls = el.get("class") or []
        if "comment" in cls:
            items.append(parse_user_comment(el))
        elif "post" in cls:
            items.append(dict(parse_post(el), kind="post"))
    after = None
    for a in s.select("a"):
        m = re.search(r"after=(t[13]_\w+)", a.get("href") or "")
        if m:
            after = m.group(1)
    out = {"posts": items, "after": after,
           "error": "" if items else (txt(s.select_one("#error h1")) or "nothing here")}
    if not q.get("after"):
        panel = s.select_one("#user")
        if panel:
            icon = panel.select_one("#user_icon")
            det = panel.select("#user_details > div")
            out["about"] = {"title": txt(panel.select_one("#user_title")), "name": txt(panel.select_one("#user_name")),
                            "icon": icon.get("src") if icon else None,
                            "desc": txt(panel.select_one("#user_description"))[:600],
                            "karma": num(txt(det[0])) if det else None,
                            "created": txt(det[1]) if len(det) > 1 else ""}
    return out


def parse_user_comment(c):
    link = c.select_one(".comment_link")
    sub = txt(c.select_one(".comment_subreddit"))
    created = c.select_one(".created")
    score = c.select_one(".comment_score")
    body = c.select_one(".comment_body")
    href = link.get("href") if link else ""
    # /user/x/comments/<post>/<slug>/<comment>/#.. -> /r/<sub>/comments/<post>/<slug>/<comment>/
    m = re.search(r"/comments/(\w+)/([^/]*)/(\w+)?", href)
    path = f"/{sub}/comments/{m.group(1)}/{m.group(2)}/" + (m.group(3) + "/" if m.group(3) else "") if m and sub else ""
    return {"kind": "comment", "id": c.get("id") or href, "title": link.get("title") or txt(link) if link else "",
            "sub": sub, "age": txt(created), "created": created.get("title") if created else "",
            "score_text": txt(score), "body": markup(body, []) if body else "", "permalink": path}


# --- suggestions --------------------------------------------------------------

_suggest = [0, None]
FALLBACK_SUGGEST = ["AskReddit", "worldnews", "science", "technology", "Damnthatsinteresting",
                    "interestingasfuck", "NatureIsFuckingLit", "EarthPorn", "space", "Music",
                    "movies", "gaming", "todayilearned", "mildlyinteresting", "aww", "Art"]


def suggest(q):
    """Subs that are busy on r/popular right now (with icon + members), cached an hour."""
    if _suggest[1] and time.time() - _suggest[0] < 3600:
        return _suggest[1]
    count = collections.Counter()
    for path in ("/r/popular?sort=hot", "/r/popular?sort=top&t=day"):
        try:
            for p in soup(fetch(path)).select("#posts > div.post"):
                sub = txt(p.select_one(".post_subreddit"))
                if sub.startswith("r/"):
                    count[sub[2:]] += 1
        except Exception as e:  # noqa: BLE001
            print("suggest:", repr(e), flush=True)
    names = [n for n, _ in count.most_common(24)]
    names += [n for n in FALLBACK_SUGGEST if n not in names]

    def info(n):
        try:
            a = about_sub(soup(fetch(f"/r/{n}")))
            return dict(a or {}, name=n)
        except Exception:  # noqa: BLE001
            return {"name": n}
    with concurrent.futures.ThreadPoolExecutor(4) as ex:
        out = {"subs": list(ex.map(info, names[:30]))}
    _suggest[:] = [time.time(), out]
    return out


# --- comments -----------------------------------------------------------------

def parse_comment(c):
    right = c.select_one(":scope > details.comment_right")
    data = right.select_one(":scope > summary.comment_data") if right else None
    author_el = data.select_one(".comment_author") if data else None
    created = data.select_one(".created") if data else None
    score_el = c.select_one(":scope > .comment_left .comment_score")
    body_el = right.select_one(":scope > .comment_body") if right else None
    images = []
    out = {
        "id": c.get("id") or "",
        "author": txt(author_el),
        "op": "op" in (author_el.get("class") or []) if author_el else False,
        "flair": txt(data.select_one(".author_flair")) if data else "",
        "created": created.get("title") if created else "",
        "age": txt(created),
        "edited": bool(data.select_one(".edited")) if data else False,
        "score": num(score_el.get("title")) if score_el else None,
        "score_text": txt(score_el),
        "body": markup(body_el, images),
        "images": images,
        "replies": [],
        "more": None,
    }
    rep = right.select_one(":scope > blockquote.replies") if right else None
    if rep:
        for ch in rep.find_all(recursive=False):
            if ch.name == "div" and "comment" in (ch.get("class") or []):
                out["replies"].append(parse_comment(ch))
            elif ch.name == "a" and "deeper_replies" in (ch.get("class") or []):
                out["more"] = {"path": ch.get("href"), "count": num(txt(ch))}
    return out


def post(q):
    path = q.get("path", "")
    if not re.match(r"^/(r|user|u)/[\w-]+/comments/\w+", path):
        raise ValueError("bad path")
    sort = q.get("sort", "top")
    s = soup(fetch(path + ("&" if "?" in path else "?") + urllib.parse.urlencode({"sort": sort})))
    p = s.select_one("div.post.highlighted")
    if p is None:
        return {"error": txt(s.select_one("#error h1")) or "post not found"}
    info = parse_post(p, full=True)
    info["permalink"] = re.sub(r"(/comments/\w+/[^/]*/?).*", r"\1", path)
    # every top-level comment comes in its own div.thread
    comments = [parse_comment(c) for t in s.select("div.thread")
                for c in t.find_all("div", class_="comment", recursive=False)]
    return {"post": info, "comments": comments}


def subs(q):
    query = q.get("q", "").strip()
    if not query:
        return {"subs": []}
    s = soup(fetch("/search?" + urllib.parse.urlencode({"q": query, "type": "sr_user"})))
    out = []
    for a in s.select("a.search_subreddit"):
        if a.get("id") == "more_subreddits":
            continue
        name = txt(a.select_one(".search_subreddit_name"))
        if not name.startswith("r/"):
            continue
        mem = a.select_one(".search_subreddit_members")
        out.append({"name": name[2:], "members": num(mem.get("title")) if mem else None,
                    "members_text": txt(mem), "desc": txt(a.select_one(".search_subreddit_description"))[:200]})
    return {"subs": out}


def search(q):
    """Post search (whole reddit, or one sub with sub=); first page also returns matching subs."""
    query = q.get("q", "").strip()
    if not query:
        return {"posts": [], "subs": [], "after": None}
    params = {"q": query, "sort": q.get("sort", "relevance"), "t": q.get("t", "")}
    sub = re.sub(r"[^A-Za-z0-9_]", "", q.get("sub", ""))
    if sub:
        params["restrict_sr"] = "on"
    if q.get("after"):
        params["after"] = q["after"]
    s = soup(fetch((f"/r/{sub}/search?" if sub else "/search?") + urllib.parse.urlencode(params)))
    posts = [parse_post(p) for p in s.select("div.post")]
    after = None
    for a in s.select("a"):
        m = re.search(r"after=(t3_\w+)", a.get("href") or "")
        if m:
            after = m.group(1)
    out = {"posts": posts, "after": after if posts else None, "error": "" if posts else "no posts found"}
    if not sub and not q.get("after"):
        out["subs"] = subs({"q": query})["subs"][:5]
    return out


ICACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icache")


def image_file(src, w):
    """Redlib image scaled down to width w as JPEG, cached in icache/."""
    if not src.startswith(("/img/", "/preview/", "/thumb/", "/emoji/", "/style/")):
        raise ValueError("bad image src")
    w = max(64, min(int(w or 720), 2160))
    key = hashlib.sha1(f"{src}|{w}".encode()).hexdigest()
    path = os.path.join(ICACHE, key + ".jpg")
    if not os.path.exists(path):
        import io
        from PIL import Image, ImageOps
        req = urllib.request.Request(REDLIB + src, headers={"User-Agent": "notreal.feed"})
        with urllib.request.urlopen(req, timeout=25) as r:
            im = Image.open(io.BytesIO(r.read()))
            im.seek(0)
        im = ImageOps.exif_transpose(im)
        if im.width > w:
            im = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
        if im.mode != "RGB":
            bgim = Image.new("RGB", im.size, (28, 35, 64))
            im = im.convert("RGBA")
            bgim.paste(im, mask=im.split()[-1])
            im = bgim
        tmp = path + ".part"
        im.save(tmp, "JPEG", quality=85, optimize=True)
        os.replace(tmp, path)
        if len(os.listdir(ICACHE)) > 4000:
            for f in sorted(os.listdir(ICACHE), key=lambda f: os.path.getmtime(os.path.join(ICACHE, f)))[:1000]:
                os.remove(os.path.join(ICACHE, f))
    return path


VCACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vcache")
_vlocks = {}


def video_file(src):
    """Remux a Redlib HLS (or any) video into vcache/<hash>.mp4, once; returns the path."""
    m = re.match(r"^/hls/(\w+)/", src)
    if not m:
        raise ValueError("bad video src")
    path = os.path.join(VCACHE, m.group(1) + ".mp4")
    lock = _vlocks.setdefault(path, threading.Lock())
    with lock:
        if not os.path.exists(path):
            tmp = path + ".part"
            try:
                inputs = cmaf_inputs(src)
            except Exception as e:  # noqa: BLE001
                print("cmaf:", repr(e), flush=True)
                inputs = None
            if inputs:
                cmd = ["-i", inputs[0]] + (["-i", inputs[1], "-map", "0:v:0", "-map", "1:a:0"] if inputs[1] else [])
            else:
                cmd = ["-i", REDLIB + src]  # old-style HLS: no -map, ffmpeg picks the best video + audio
            r = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y"] + cmd +
                               ["-c", "copy", "-movflags", "+faststart", "-f", "mp4", tmp],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0 or not os.path.exists(tmp):
                raise RuntimeError("ffmpeg: " + r.stderr[-300:])
            os.replace(tmp, path)
            prune_vcache()
    return path


def cmaf_inputs(src, max_side=1080):
    """Reddit's HLS is byte ranges into whole CMAF files; through Redlib ffmpeg only gets the
    first 4 s segment. Read the playlists and return (video file url, audio file url or None)."""
    base = REDLIB + src.split("?", 1)[0].rsplit("/", 1)[0] + "/"
    master = fetch(src)
    audio, best = {}, None
    lines = master.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in ln:
            g, u = re.search(r'GROUP-ID="([^"]+)"', ln), re.search(r'URI="([^"]+)"', ln)
            if g and u:
                audio.setdefault(g.group(1), u.group(1))
        elif ln.startswith("#EXT-X-STREAM-INF:") and i + 1 < len(lines):
            res = re.search(r"RESOLUTION=(\d+)x(\d+)", ln)
            bw = re.search(r"BANDWIDTH=(\d+)", ln)
            w, h = (int(res.group(1)), int(res.group(2))) if res else (0, 0)
            if min(w, h) > max_side:
                continue
            key = (w * h, int(bw.group(1)) if bw else 0)
            if best is None or key > best[0]:
                grp = re.search(r'AUDIO="([^"]+)"', ln)
                best = (key, lines[i + 1].strip(), grp.group(1) if grp else None)
    if not best:
        return None
    m = re.search(r'#EXT-X-MAP:URI="([^"]+)"', fetch_raw(base + best[1]))
    if not m:
        return None
    a = audio.get(best[2]) if best[2] else None
    if a:
        am = re.search(r'#EXT-X-MAP:URI="([^"]+)"', fetch_raw(base + a))
        a = base + am.group(1) if am else None
    return base + m.group(1), a


def fetch_raw(url):
    with urllib.request.urlopen(url, timeout=25) as r:
        return r.read().decode()


def prune_vcache(keep_bytes=2 * 1024 ** 3):
    files = sorted((os.path.join(VCACHE, f) for f in os.listdir(VCACHE) if f.endswith(".mp4")),
                   key=os.path.getmtime, reverse=True)
    total = 0
    for f in files:
        total += os.path.getsize(f)
        if total > keep_bytes:
            os.remove(f)


ROUTES = {"/feed": feed, "/post": post, "/subs": subs, "/user": user, "/suggest": suggest, "/search": search}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/img":
            q = dict(urllib.parse.parse_qsl(u.query))
            try:
                return self.file(image_file(q.get("src", ""), q.get("w")), "image/jpeg")
            except Exception as e:  # noqa: BLE001
                return self.reply(502, {"error": f"{type(e).__name__}: {e}"})
        if u.path == "/video":
            return self.video(dict(urllib.parse.parse_qsl(u.query)).get("src", ""))
        fn = ROUTES.get(u.path)
        if not fn:
            return self.reply(404, {"error": "not found"})
        q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
        try:
            self.reply(200, fn(q))
        except Exception as e:  # noqa: BLE001
            self.reply(502, {"error": f"{type(e).__name__}: {e}"})

    def video(self, src):
        try:
            path = video_file(src)
        except Exception as e:  # noqa: BLE001
            return self.reply(502, {"error": f"{type(e).__name__}: {e}"})
        self.file(path, "video/mp4")

    def file(self, path, ctype):
        size = os.path.getsize(path)
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else end
            else:
                start = size - int(m.group(2))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=86400")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def reply(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
