"""A synthetic 'photo' (sunset over hills) for the photo editor screenshot. Usage: sample.py OUT.jpg"""
import math
import sys

import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

W, H = 900, 1200
buf = bytearray(W * H * 3)
for y in range(H):
    t = y / H
    sky = (int(40 + 200 * t), int(60 + 90 * t), int(140 - 60 * t))
    for x in range(W):
        r, g, b = sky
        d = math.hypot(x - 570, y - 615)
        if d < 112:
            r, g, b = 255, 214, 140
        elif d < 195:
            k = (195 - d) / 83 * 0.5
            r, g, b = int(r + (255 - r) * k), int(g + (190 - g) * k), int(b + (120 - b) * k)
        if y > 750 + 68 * math.sin(x / 128) + 30 * math.sin(x / 46):
            r, g, b = int(30 + 20 * math.sin(x / 68)), int(50 + 25 * t), int(60 - 20 * t)
        o = (y * W + x) * 3
        buf[o:o + 3] = bytes((min(r, 255), min(g, 255), min(b, 255)))
GdkPixbuf.Pixbuf.new_from_data(bytes(buf), GdkPixbuf.Colorspace.RGB, False, 8, W, H, W * 3).savev(sys.argv[1], "jpeg", ["quality"], ["90"])
