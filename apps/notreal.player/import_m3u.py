import json, os, re, sys, unicodedata, difflib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api import Subsonic
c = json.load(open(os.path.expanduser("~/.config/spin/config.json")))
api = Subsonic(c["server"], c["user"], c["token"], c["salt"])

def n(s):
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^\w]+", " ", s).strip()

songs, off = [], 0
while True:
    b = api.call("search3", timeout=60, query="", songCount=500, songOffset=off, artistCount=0, albumCount=0) \
        .get("searchResult3", {}).get("song", [])
    songs += b
    if len(b) < 500: break
    off += 500
by_path = {n(s.get("path", "")): s for s in songs}
by_title = {}
for s in songs:
    by_title.setdefault(n(s.get("title")), []).append(s)

def core(t):
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", t or "")
    t = re.split(r"\s(feat\.?|ft\.?|featuring)\s", t, flags=re.I)[0]
    return n(t)

by_core = {}
for s in songs:
    by_core.setdefault(core(s.get("title")), []).append(s)

def file_title(path):
    t = re.sub(r"\.\w+$", "", path.rsplit("/", 1)[-1])
    return re.sub(r"^\d+(-\d+)?\s*-\s*", "", t).strip()

def score(s, artist, album, dur):
    a = max(difflib.SequenceMatcher(None, artist, n(s.get(k, ""))).ratio() for k in ("artist", "albumArtist", "displayArtist"))
    al = difflib.SequenceMatcher(None, album, n(s.get("album", ""))).ratio()
    d = abs((s.get("duration") or 0) - dur) if dur else 0
    return a * 2 + al + (1 if d <= 3 else 0.5 if d <= 10 else -1)

def match(path, dur):
    s = by_path.get(n(path))
    if s: return s, "path"
    parts = path.split("/")
    artist, album = n(parts[0]), n(parts[1]) if len(parts) > 2 else ""
    title = n(file_title(path))
    cands = by_title.get(title, [])
    if not cands:  # titles with small differences
        keys = difflib.get_close_matches(title, by_title.keys(), n=5, cutoff=0.85)
        cands = [s for k in keys for s in by_title[k]]
    best = max(cands, key=lambda s: score(s, artist, album, dur), default=None)
    if best and score(best, artist, album, dur) >= 2.2:
        return best, "fuzzy"
    # last pass: title without (...) / feat., duration must agree
    ok = [s for s in by_core.get(core(file_title(path)), []) if dur and abs((s.get("duration") or 0) - dur) <= 4]
    best = max(ok, key=lambda s: score(s, artist, album, dur), default=None)
    if best and score(best, artist, album, dur) >= 1.8:
        return best, "fuzzy"
    return None, None

lines = [l.strip() for l in open(sys.argv[1], encoding="utf-8-sig") if l.strip()]
ids, miss, fz, dur = [], [], [], 0
for l in lines:
    if l.startswith("#EXTINF:"):
        dur = int(float(l[8:].split(",")[0] or 0)); continue
    if l.startswith("#"): continue
    s, how = match(l, dur)
    if s:
        ids.append(s["id"])
        if how == "fuzzy": fz.append(f"{l}  =>  {s['path']}")
    else:
        miss.append(l)
    dur = 0
print(f"matched {len(ids)}/{len(ids)+len(miss)} ({len(fz)} fuzzy)")
print("FUZZY:"); print("\n".join(fz))
print("MISSING:"); print("\n".join(miss))
json.dump(ids, open(sys.argv[2], "w"))
