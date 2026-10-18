#!/usr/bin/env python3
"""Synthetic accuracy/speed benchmark: sloppy simulated swipes of common words."""
import random, sys, time
import numpy as np
from decoder import Decoder, KEYS

lang = sys.argv[1] if len(sys.argv) > 1 else "en"
ROW = 1.4
d = Decoder(lang)
rnd = random.Random(1)
pool = [i for i in range(3000) if len(d.paths[i]) >= 2]


def fake(word):
    path = d.paths[d.words.index(word)]
    pts = []
    for c in path:
        x, y = KEYS[c]
        pts.append((x + rnd.gauss(0, 0.28), y * ROW + rnd.gauss(0, 0.28)))
    out = []
    for a, b in zip(pts, pts[1:]):
        for t in np.linspace(0, 1, 8, endpoint=False):
            out.append((a[0] + (b[0] - a[0]) * t + rnd.gauss(0, .05), a[1] + (b[1] - a[1]) * t + rnd.gauss(0, .05)))
    out.append(pts[-1])
    return out


words = [d.words[rnd.choice(pool)] for _ in range(400)]
for w in words[:40]:  # warm the template cache
    d.decode(fake(w), ROW)
top1 = top3 = 0
t0 = time.time()
miss = []
for w in words:
    r = [x for x, _ in d.decode(fake(w), ROW)]
    top1 += bool(r) and r[0] == w
    top3 += w in r[:3]
    if r and r[0] != w:
        miss.append((w, r[:3]))
dt = (time.time() - t0) / len(words)
print(f"{lang}: top1 {top1/len(words):.1%} top3 {top3/len(words):.1%}  {dt*1000:.1f} ms/decode")
for m in miss[:15]:
    print("  ", m)
