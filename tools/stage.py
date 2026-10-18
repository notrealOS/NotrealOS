#!/usr/bin/env python3
"""Copy app sources from the phone's home into the notrealOS repo (source files only).

Usage: tools/stage.py            copy everything in APPS
       tools/stage.py spin chat  copy only these source folders
Nothing personal should survive: run tools/scan.py afterwards.
"""
import os, re, shutil, sys

HOME = os.path.expanduser("~")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# source folder in $HOME -> path in the repo
APPS = {
    "camera": "apps/notreal.cam", "vapormail": "apps/notreal.mail", "chat": "apps/notreal.chat",
    "calls": "apps/notreal.phone", "calendar": "apps/notreal.calendar", "contacts": "apps/notreal.contacts",
    "gallery": "apps/notreal.gallery", "edit": "apps/notreal.edit", "link": "apps/notreal.link",
    "share": "apps/notreal.share", "nrvolume": "apps/notreal.sound", "spin": "apps/notreal.player",
    "maps": "apps/notreal.maps", "reddit": "apps/notreal.feed",
    "theme": "shell/theme",
}

# per-source paths (regex on the path inside the folder) that must stay private or aren't ours to publish
EXCLUDE = {
    "theme": r"^lock/vapor|^icons-before|\.v1$|^icon-src/(?!dev\.notareal\.(?!Droid)|org\.gnome\.|mobi\.phosh\.|sm\.puri\.)",
}

SKIP_DIR = {"__pycache__", ".git", "shots", "venv", ".venv", "node_modules", "models", "cache"}
SKIP_FILE = re.compile(r"(\.before|\.bak|\.orig|\.log$|\.pyc$|~$|^core$|\.tar|\.gz$|\.zip$|\.db$|\.sqlite|"
                       r"token|secret|client\.json|password|\.pem$|\.key$|config-test\.json|\.before-opensrc)", re.I)
MAX = 2 * 1024 * 1024


def stage(src, dst):
    s, d = os.path.join(HOME, src), os.path.join(REPO, dst)
    shutil.rmtree(d, ignore_errors=True)
    skipped = []
    for root, dirs, files in os.walk(s):
        dirs[:] = [x for x in dirs if x not in SKIP_DIR and not x.endswith(".bak")]
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, s)
            if SKIP_FILE.search(f) or re.search(EXCLUDE.get(src, "$^"), rel) or os.path.islink(p) or os.path.getsize(p) > MAX:
                skipped.append(rel)
                continue
            os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
            shutil.copy2(p, os.path.join(d, rel))
    print(f"{src:12} -> {dst:24} skipped: {', '.join(skipped) or '-'}")


if __name__ == "__main__":
    for k in sys.argv[1:] or APPS:
        stage(k, APPS[k])
