#!/usr/bin/env python3
"""Recolor our vapor icons for the active theme.

The first run copies every vapor-colored icon in ~/.local/share/icons/hicolor/scalable/apps
(ours + ~/icons-vapor's stock-app icons) to ~/theme/icon-src/ - those are the ORIGINALS.
Each run writes them back recolored by NAME (vapor hex -> same-named color of the active theme),
also into the personal Papirus overlay copies, with corners scaled by --nr-corners."""
import glob
import os
import re
import subprocess
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notareal_theme as t

HOME = os.path.expanduser("~")
HICOLOR = f"{HOME}/.local/share/icons/hicolor/scalable/apps"
PAPIRUS = f"{HOME}/.local/share/icons/Papirus"
SRC = os.path.join(t.DIR, "icon-src")
EXTRA = {"#3a2d63": "bg-deep", "#0b0e1c": "fg-bright", "#2a3157": "accent"}  # icon-only vapor shades
VAPOR_HEX = re.compile(r"#[0-9a-fA-F]{6}\b")


def snapshot():
    os.makedirs(SRC, exist_ok=True)
    for p in glob.glob(f"{HICOLOR}/*.svg"):
        dst = os.path.join(SRC, os.path.basename(p))
        if os.path.exists(dst):
            continue
        svg = open(p).read()
        if sum(1 for h in VAPOR_HEX.findall(svg) if h.lower() in t.VAPOR) >= 2:
            open(dst, "w").write(svg)


def recolor(svg, pal, corners):
    if t.active() == "vapor":
        return svg              # the originals ARE vapor
    def col(m):
        h = m.group(0).lower()
        name = t.VAPOR.get(h) or EXTRA.get(h)
        return pal.get(name, m.group(0)) if name else m.group(0)
    svg = VAPOR_HEX.sub(col, svg)
    if t.value("icon-outline", "1") == "0":     # NASA v4: smooth tiles, no frame stroke
        svg = re.sub(r'(<rect[^>]*\brx="26"[^>]*?)\s+stroke="[^"]*"', r'\1', svg)
        svg = re.sub(r'(<rect[^>]*\brx="26"[^>]*?)\s+stroke-width="[^"]*"', r'\1', svg)
    if corners != 1:
        svg = re.sub(r'\b(rx|ry)="([\d.]+)"', lambda m: f'{m[1]}="{float(m[2]) * corners:g}"', svg)
    return svg


def main():
    snapshot()
    pal = t.palette()
    try:
        corners = float(t.value("corners", "1"))
    except ValueError:
        corners = 1.0
    n = 0
    for src in sorted(glob.glob(f"{SRC}/*.svg")):
        name = os.path.basename(src)
        svg = recolor(open(src).read(), pal, corners)
        for dst in [os.path.join(HICOLOR, name)] + glob.glob(f"{PAPIRUS}/*/apps/{name}"):
            if not os.path.islink(os.path.dirname(os.path.dirname(dst))) or dst.startswith(HICOLOR):
                old = open(dst).read() if os.path.exists(dst) else ""
                if old != svg:
                    open(dst, "w").write(svg)
                    n += 1
    for d in (f"{HOME}/.local/share/icons/hicolor", PAPIRUS):
        if os.path.isdir(d):
            subprocess.run(["gtk4-update-icon-cache", "-f", "-t", d], capture_output=True)
            subprocess.run(["gtk-update-icon-cache", "-f", "-t", d], capture_output=True)
    print(f"icons: {n} files updated for {t.active()}")


if __name__ == "__main__":
    main()
