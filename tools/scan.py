#!/usr/bin/env python3
"""Fail if anything personal is left in the repo. Run before every commit/push.

Usage: tools/scan.py [path...]     (default: whole repo)
"""
import os, re, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATTERNS = {
    "tailscale ip": r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b",
    "lan ip": r"\b192\.168\.\d{1,3}\.\d{1,3}\b",
    "tailnet host": r"\b[\w-]+\.tail[0-9a-f]+\.ts\.net\b|notarealcachy",
    "phone number": r"\+\d{9,14}\b",
    "email": r"\b(?!(?:you|user|me|name)@)[\w.+-]+@(?!example\.)[\w-]+\.[\w.]+\b",
    "google key": r"AIza[0-9A-Za-z_-]{30,}",
    "oauth secret": r"GOCSPX-[\w-]+|\b\d{6,}-[0-9a-z]{20,}\.apps\.googleusercontent\.com",
    "matrix token": r"\bsy[tc]_[\w-]{10,}",
    "github token": r"\bgh[pousr]_[A-Za-z0-9]{20,}",
    "home path": r"/home/notareal\w*",
    "user name": r"\bnotarealphone\b|\bnotarealinux\b",
}
# Your own name, company, street... one regex per line, kept OUT of the repo:
PRIVATE = os.path.expanduser("~/.config/notreal/private-patterns")
if os.path.exists(PRIVATE):
    for i, l in enumerate(open(PRIVATE).read().splitlines()):
        if l.strip() and not l.startswith("#"):
            PATTERNS["private %d" % i] = l.strip()
ALLOW = re.compile(r"scan-ok")  # put "scan-ok" in a line comment to allow a reviewed hit


def main():
    paths = sys.argv[1:] or [REPO]
    hits = 0
    for top in paths:
        for root, dirs, files in os.walk(top):
            dirs[:] = [d for d in dirs if d != ".git"]
            for f in files:
                p = os.path.join(root, f)
                if p == os.path.abspath(__file__) or f == "LICENSE":
                    continue
                try:
                    lines = open(p, encoding="utf-8").read().splitlines()
                except (UnicodeDecodeError, OSError):
                    continue
                for n, line in enumerate(lines, 1):
                    if ALLOW.search(line):
                        continue
                    for kind, pat in PATTERNS.items():
                        for m in re.finditer(pat, line):
                            hits += 1
                            print(f"{os.path.relpath(p, REPO)}:{n}: [{kind}] {m.group(0)}")
    print(f"{hits} hit(s)", file=sys.stderr)
    sys.exit(1 if hits else 0)


if __name__ == "__main__":
    main()
