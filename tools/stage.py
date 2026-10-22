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
    "maps": "apps/notreal.maps", "reddit": "apps/notreal.feed", "tube": "apps/notreal.tube",
    "theme": "shell/theme",
    ".local/share/phantomshell": "shell/phantomshell", "arc": "shell/joystick", "swipe": "shell/keyboard",
    "gestures": "shell/gestures", "crt": "shell/crt", "overview-widget": "shell/overview-widget",
    "rotated": "shell/rotated", "shadewatch": "shell/shadewatch", "circle": "shell/circle",
    "voice": "system/voice", "alarm": "system/alarm", "sleepd": "system/sleepd", "doctor": "system/doctor",
    "backup": "system/backup", "haptics": "system/haptics", "ka5idle": "system/ka5idle", "glyph": "system/glyph",
}

# per-source paths (regex on the path inside the folder) that must stay private or aren't ours to publish
EXCLUDE = {
    ".local/share/phantomshell": r"\.jpg$|^notes\.json$|^reminders\.json$",
    "swipe": r"^dict/|^dl-freq/|vpkeep",
    "backup": r"^RESTORE\.md$",          # the author's personal restore notes
    "glyph": r"notarealchat-version$",
    "theme": r"^lock/vapor|^icons-before|\.v1$|^icon-src/(?!dev\.notareal\.(?!Droid)|org\.gnome\.|mobi\.phosh\.|sm\.puri\.)",
}

SKIP_DIR = {"__pycache__", ".git", "shots", "venv", ".venv", "node_modules", "models", "cache"}
SKIP_FILE = re.compile(r"(\.v\d$|\.ko$|\.ko\.zst$|\.before|\.bak|\.orig|\.log$|\.pyc$|~$|^core$|\.tar|\.gz$|\.zip$|\.db$|\.sqlite|"
                       r"token|secret|client\.json|password|\.pem$|\.key$|config-test\.json|\.before-opensrc|\.tmp(-opensrc)?$)", re.I)
MAX = 2 * 1024 * 1024
REPO_ONLY = re.compile(r"^(README\.md|get-[\w-]+\.sh)$")


def is_elf(p):
    """Compiled programs are built from their source, never committed."""
    try:
        with open(p, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def stage(src, dst):
    s, d = os.path.join(HOME, src), os.path.join(REPO, dst)
    copied, skipped = set(), []
    for root, dirs, files in os.walk(s):
        dirs[:] = [x for x in dirs if x not in SKIP_DIR and not x.endswith(".bak")]
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, s)
            if SKIP_FILE.search(f) or is_elf(p) or re.search(EXCLUDE.get(src, "$^"), rel) or os.path.islink(p) or os.path.getsize(p) > MAX:
                skipped.append(rel)
                continue
            os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
            shutil.copy2(p, os.path.join(d, rel))
            copied.add(rel)
            if f.endswith(".desktop"):  # launchers can't say "~": install with sed "s|@HOME@|$HOME|g"
                q = os.path.join(d, rel)
                with open(q) as fh:
                    txt = fh.read()
                with open(q, "w") as fh:
                    fh.write(txt.replace(HOME, "@HOME@"))
    # drop files that left the source, but keep what only lives in the repo (READMEs, downloaders)
    for root, dirs, files in os.walk(d):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), d)
            if rel not in copied and not REPO_ONLY.search(f):
                os.remove(os.path.join(root, f))
    print(f"{src:12} -> {dst:24} skipped: {', '.join(skipped) or '-'}")


if __name__ == "__main__":
    for k in sys.argv[1:] or APPS:
        stage(k, APPS[k])
