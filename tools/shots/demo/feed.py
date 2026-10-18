"""notreal.feed demo: a fake feed API with made-up posts and generated images (no Reddit content)."""
import json
import math
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

POSTS = [  # sub, author, age, title, score, comments, body, image (colours) or None
    ("r/mobilelinux", "u/pocket_kernel", "2h ago", "My daily driver: mainline Linux on a 2022 flagship",
     "1.2k", 214, "", ((40, 40, 60), (250, 120, 60))),
    ("r/synthesizers", "u/warm_pad", "4h ago", "Finished my first DIY oscillator, it's alive!",
     "856", 97, "", ((20, 60, 70), (120, 230, 200))),
    ("r/linux", "u/tty_dreams", "5h ago", "What's the one tool you install on every new machine?",
     "642", 1033, "Mine is ripgrep. Curious what everyone else can't live without.", None),
    ("r/analog", "u/silver_halide", "7h ago", "Portra 400, golden hour by the lake",
     "2.3k", 158, "", ((250, 190, 120), (60, 70, 120))),
    ("r/cycling", "u/spare_tube", "9h ago", "Morning loop before work, 42 km and zero cars",
     "301", 40, "", ((160, 210, 240), (60, 120, 70))),
]


def image(seed, w=720, h=480):
    (a, b) = POSTS[seed][7]
    buf = bytearray(w * h * 3)
    for y in range(h):
        t = y / h
        for x in range(w):
            k = t + 0.08 * math.sin(x / 47 + seed) * math.cos(y / 31)
            k = min(1, max(0, k))
            o = (y * w + x) * 3
            buf[o:o + 3] = bytes(int(a[c] + (b[c] - a[c]) * k) for c in range(3))
    pb = GdkPixbuf.Pixbuf.new_from_data(bytes(buf), GdkPixbuf.Colorspace.RGB, False, 8, w, h, w * 3)
    return pb.save_to_bufferv("jpeg", ["quality"], ["88"])[1]


IMAGES = {i: image(i) for i, p in enumerate(POSTS) if p[7]}


def post(i):
    sub, author, age, title, score, comments, body, img = POSTS[i]
    return {"id": "demo%d" % i, "sub": sub, "author": author, "created": "", "age": age, "title": title,
            "flair": None, "score": 0, "score_text": score, "comments": comments, "permalink": "/%s/demo%d" % (sub, i),
            "url": None, "domain": None, "thumb": None, "body": body, "body_images": [], "nsfw": False,
            "stickied": False, "media": {"type": "image", "src": "/img/demo%d.jpg" % i, "w": 720, "h": 480} if img else None}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/img":
            n = int("".join(ch for ch in q.get("src", "0") if ch.isdigit()) or 0)
            return self.send(IMAGES.get(n, IMAGES[0]), "image/jpeg")
        if u.path == "/feed":
            return self.send(json.dumps({"posts": [post(i) for i in range(len(POSTS))], "after": None,
                                         "about": None, "error": None}).encode())
        self.send(json.dumps({"posts": [], "subs": [], "results": [], "error": None}).encode())


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["NOTAREAL_REDDIT_API"] = "http://127.0.0.1:%d" % srv.server_address[1]
