"""notreal.gallery demo: generated 'photos' and a screenshot in a sandbox Pictures folder."""
import math
import os

import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

CAM = os.path.expanduser("~/Pictures/Camera")
SHOTS = os.path.expanduser("~/Pictures/Screenshots")
os.makedirs(CAM, exist_ok=True)
os.makedirs(SHOTS, exist_ok=True)
PALETTES = [((255, 140, 90), (40, 30, 80)), ((120, 200, 255), (20, 70, 60)), ((250, 220, 160), (150, 70, 50)),
            ((180, 240, 200), (30, 90, 120)), ((240, 160, 200), (60, 40, 100)), ((200, 200, 210), (40, 45, 60)),
            ((255, 210, 120), (200, 80, 40)), ((150, 220, 230), (230, 240, 250)), ((90, 130, 220), (10, 10, 40)),
            ((230, 120, 120), (250, 220, 200)), ((140, 200, 120), (40, 70, 40)), ((250, 250, 240), (120, 160, 200))]


def photo(i, w=240, h=320):
    top, bot = PALETTES[i % len(PALETTES)]
    buf = bytearray(w * h * 3)
    hz = 0.55 + 0.1 * math.sin(i)
    for y in range(h):
        t = y / h
        for x in range(w):
            k = t
            if y / h > hz + 0.05 * math.sin(x / 60 + i):
                k = min(1, t + 0.35)
            o = (y * w + x) * 3
            buf[o:o + 3] = bytes(int(top[c] + (bot[c] - top[c]) * k) for c in range(3))
    return GdkPixbuf.Pixbuf.new_from_data(bytes(buf), GdkPixbuf.Colorspace.RGB, False, 8, w, h, w * 3)


for i in range(len(PALETTES)):
    p = os.path.join(CAM, "IMG_2026101%d_%02d0000.jpg" % (i % 8, 8 + i))
    if not os.path.exists(p):
        photo(i).savev(p, "jpeg", ["quality"], ["85"])
        os.utime(p, (1792300000 + i * 3600, 1792300000 + i * 3600))
