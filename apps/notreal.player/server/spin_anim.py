#!/usr/bin/env python3
"""spin-anim: converts Navidrome's animated cover.webp files into small looping mp4s for Spin.

GET /anim/<albumId>.mp4 -> 200 mp4 | 202 converting, retry | 404 no animated cover.
On start (and daily) it walks the whole library so most covers are ready in advance.
Config: ~/.config/spin-anim.json {"server", "user", "token", "salt", "port", "bind"}.
"""
import ctypes as C
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONF = json.load(open(os.path.expanduser("~/.config/spin-anim.json")))
OUT = os.path.expanduser("~/.cache/spin-anim")
STATE_FILE = os.path.join(OUT, "state.json")
SIZE, FPS = 480, 30
os.makedirs(OUT, exist_ok=True)

lock = threading.Lock()
try:
    state = json.load(open(STATE_FILE))          # album_id -> "ok" | "none" | "fail"
except (OSError, ValueError):
    state = {}
busy = set()
pool = ThreadPoolExecutor(max_workers=3)


def save_state():
    with lock:
        tmp = STATE_FILE + ".tmp"
        json.dump(state, open(tmp, "w"))
        os.replace(tmp, STATE_FILE)


def api(endpoint, raw=False, **kw):
    p = {"u": CONF["user"], "t": CONF["token"], "s": CONF["salt"], "v": "1.16.1", "c": "spin-anim", "f": "json"}
    p.update(kw)
    url = f"{CONF['server'].rstrip('/')}/rest/{endpoint}?{urllib.parse.urlencode(p)}"
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read()
        return (data, r.headers.get("content-type", "")) if raw else json.loads(data)["subsonic-response"]


# --- animated webp decoding through libwebpdemux -------------------------------
_d = C.CDLL("libwebpdemux.so.2")


class _Opts(C.Structure):
    _fields_ = [("color_mode", C.c_int), ("use_threads", C.c_int), ("pad", C.c_uint32 * 7)]


class _Data(C.Structure):
    _fields_ = [("bytes", C.c_void_p), ("size", C.c_size_t)]


class _Info(C.Structure):
    _fields_ = [("w", C.c_uint32), ("h", C.c_uint32), ("loop", C.c_uint32), ("bg", C.c_uint32),
                ("n", C.c_uint32), ("pad", C.c_uint32 * 4)]


_ABI = 0x0107
_d.WebPAnimDecoderOptionsInitInternal.argtypes = [C.POINTER(_Opts), C.c_int]
_d.WebPAnimDecoderNewInternal.restype = C.c_void_p
_d.WebPAnimDecoderNewInternal.argtypes = [C.POINTER(_Data), C.POINTER(_Opts), C.c_int]
_d.WebPAnimDecoderGetInfo.argtypes = [C.c_void_p, C.POINTER(_Info)]
_d.WebPAnimDecoderGetNext.argtypes = [C.c_void_p, C.POINTER(C.POINTER(C.c_uint8)), C.POINTER(C.c_int)]
_d.WebPAnimDecoderHasMoreFrames.argtypes = [C.c_void_p]
_d.WebPAnimDecoderDelete.argtypes = [C.c_void_p]


def is_animated_webp(b):
    return b[:4] == b"RIFF" and b[8:12] == b"WEBP" and b"ANIM" in b[:256]


def webp_to_mp4(raw, out):
    buf = C.create_string_buffer(raw, len(raw))
    o = _Opts()
    _d.WebPAnimDecoderOptionsInitInternal(C.byref(o), _ABI)
    o.color_mode, o.use_threads = 1, 1          # MODE_RGBA
    dec = _d.WebPAnimDecoderNewInternal(C.byref(_Data(C.cast(buf, C.c_void_p), len(raw))), C.byref(o), _ABI)
    if not dec:
        raise ValueError("webp decoder refused file")
    try:
        info = _Info()
        _d.WebPAnimDecoderGetInfo(dec, C.byref(info))
        w, h = info.w, info.h
        # crop to a centred square, then scale
        side = min(w, h)
        vf = f"crop={side}:{side},scale={SIZE}:{SIZE}:flags=lanczos"
        tmp = out + ".part.mp4"
        p = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba",
                              "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-", "-vf", vf, "-c:v", "libx264",
                              "-preset", "slow", "-crf", "22", "-profile:v", "high", "-pix_fmt", "yuv420p",
                              "-movflags", "+faststart", "-an", tmp], stdin=subprocess.PIPE)
        pb, ts = C.POINTER(C.c_uint8)(), C.c_int()
        written, prev = 0, None
        while _d.WebPAnimDecoderHasMoreFrames(dec):
            _d.WebPAnimDecoderGetNext(dec, C.byref(pb), C.byref(ts))
            frame = C.string_at(pb, w * h * 4)
            target = round(ts.value * FPS / 1000)   # ts = end time of this frame
            prev = frame
            while written < target:
                p.stdin.write(frame)
                written += 1
        if written == 0 and prev:
            p.stdin.write(prev)
        p.stdin.close()
        if p.wait() != 0:
            raise RuntimeError("ffmpeg failed")
        os.replace(tmp, out)
    finally:
        _d.WebPAnimDecoderDelete(dec)


def convert(album_id):
    out = os.path.join(OUT, f"{album_id}.mp4")
    try:
        raw, ctype = api("getCoverArt", raw=True, id=f"al-{album_id}")
        if not is_animated_webp(raw):
            result = "none"
        else:
            t = time.time()
            webp_to_mp4(raw, out)
            print(f"converted {album_id}: {len(raw) >> 10} KiB webp -> {os.path.getsize(out) >> 10} KiB mp4 "
                  f"in {time.time() - t:.1f}s", flush=True)
            result = "ok"
    except Exception as e:  # noqa: BLE001
        print("convert failed", album_id, e, flush=True)
        result = "fail"
    with lock:
        state[album_id] = result
        busy.discard(album_id)
    save_state()
    return result


def request(album_id):
    with lock:
        st = state.get(album_id)
        if st == "ok" and os.path.exists(os.path.join(OUT, f"{album_id}.mp4")):
            return "ok"
        if st == "none":
            return "none"
        if album_id in busy:
            return "busy"
        busy.add(album_id)
    pool.submit(convert, album_id)
    return "busy"


def walk_library():
    while True:
        try:
            offset, ids = 0, []
            while True:
                r = api("getAlbumList2", type="alphabeticalByName", size=500, offset=offset)
                albums = r.get("albumList2", {}).get("album", [])
                ids += [a["id"] for a in albums]
                if len(albums) < 500:
                    break
                offset += 500
            todo = [i for i in ids if i not in state]
            print(f"library: {len(ids)} albums, {len(todo)} unchecked", flush=True)
            for i in todo:
                request(i)
        except Exception as e:  # noqa: BLE001
            print("walk failed", e, flush=True)
        time.sleep(86400)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        m = re.fullmatch(r"/anim/([A-Za-z0-9_-]+)\.mp4", self.path.split("?")[0])
        if not m:
            return self.send_error(404)
        st = request(m.group(1))
        if st == "none":
            return self.send_error(404, "no animated cover")
        if st == "busy":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = os.path.join(OUT, f"{m.group(1)}.mp4")
        data = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=walk_library, daemon=True).start()
    srv = ThreadingHTTPServer((CONF.get("bind", "0.0.0.0"), CONF.get("port", 4535)), Handler)
    print("spin-anim listening on", srv.server_address, flush=True)
    srv.serve_forever()
