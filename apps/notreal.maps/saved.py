"""Saved places: your own lists plus an importer for Google Takeout.

Google has no API for saved lists, so they come from a Takeout export (takeout.google.com:
"Saved" = one CSV per list, "Maps (your places)" = starred/labelled places as GeoJSON).
CSV rows carry only a title and a URL with the place's CID, so those are looked up with a
Places text search and matched by CID (googleMapsUri ends in ?cid=N).

Store: ~/.local/share/notarealmaps/saved.json  {"lists": {"Name": [place, ...]}}
place = {id, name, address, lat, lng, note, gmaps, approx?}
"""
import csv
import glob
import io
import json
import os
import re
import urllib.parse
import zipfile

DATA = os.path.expanduser("~/.local/share/notarealmaps")
STORE = os.path.join(DATA, "saved.json")
DEFAULT_LIST = "Saved"
KEYS = ("id", "name", "address", "lat", "lng", "note", "gmaps", "approx")


def load():
    try:
        with open(STORE) as f:
            d = json.load(f)
        if isinstance(d.get("lists"), dict):
            return d
    except (OSError, ValueError):
        pass
    return {"lists": {}}


def save(d):
    os.makedirs(DATA, exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1, ensure_ascii=False)
    os.replace(tmp, STORE)


def all_places(d=None):
    seen, out = set(), []
    for lst in (d or load())["lists"].values():
        for p in lst:
            k = _key(p)
            if k not in seen and p.get("lat") is not None:
                seen.add(k)
                out.append(p)
    return out


def _key(p):
    return p.get("id") or "%.5f,%.5f" % (p.get("lat") or 0, p.get("lng") or 0)


def is_saved(p):
    k = _key(p)
    return any(_key(q) == k for q in all_places())


def add(p, list_name=DEFAULT_LIST):
    d = load()
    lst = d["lists"].setdefault(list_name, [])
    if not any(_key(q) == _key(p) for q in lst):
        lst.insert(0, {k: p.get(k) for k in KEYS if p.get(k) is not None})
    save(d)


def remove(p):
    d = load()
    k = _key(p)
    for name in list(d["lists"]):
        d["lists"][name] = [q for q in d["lists"][name] if _key(q) != k]
    save(d)


def find(name):
    """Saved place whose name matches what was said (voice: "take me to <saved place>")."""
    n = _norm(name)
    if not n:
        return None
    places = all_places()
    for p in places:
        if _norm(p.get("name")) == n or _norm(p.get("note")) == n:
            return p
    return None


def _norm(t):
    return " ".join(re.sub(r"[^\w\s]", " ", (t or "").lower()).split())


# --- Takeout -------------------------------------------------------------------------

def find_takeout():
    """Newest takeout zip in ~/Downloads (or an extracted Takeout folder)."""
    cands = glob.glob(os.path.expanduser("~/Downloads/takeout*.zip")) + \
        glob.glob(os.path.expanduser("~/Downloads/Takeout"))
    return max(cands, key=os.path.getmtime) if cands else None


def _files(path):
    """(relative path, bytes) for the files we care about, from a zip or a folder."""
    want = re.compile(r"(^|/)(Saved/[^/]+\.csv|Maps \(your places\)/[^/]+\.json|Maps/My label+ed places/[^/]+\.json)$", re.I)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if want.search(n):
                    yield n, z.read(n)
    else:
        for root, _dirs, files in os.walk(path):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, path).replace(os.sep, "/")
                if want.search(rel):
                    with open(full, "rb") as fh:
                        yield rel, fh.read()


def parse_takeout(path):
    """{list name: [raw place]} — raw places may lack lat/lng (then they have cid/name)."""
    lists = {}
    for rel, data in _files(path):
        stem = os.path.splitext(os.path.basename(rel))[0]
        if rel.lower().endswith(".csv"):
            items = _parse_csv(data.decode("utf-8-sig", "replace"))
        else:
            if re.search(r"review", stem, re.I):
                continue
            items = _parse_geojson(data)
            stem = {"saved places": "Starred", "labelled places": "Labelled", "labeled places": "Labelled"}.get(
                stem.lower(), stem)
        if items:
            lists.setdefault(stem, []).extend(items)
    return lists


def _parse_csv(text):
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if isinstance(k, str) and isinstance(v, (str, type(None)))}
        url = row.get("url", "")
        title = row.get("title", "")
        if not url and not title:
            continue
        p = {"name": title, "note": row.get("note") or row.get("comment") or "", "gmaps": url}
        p.update(_from_url(url))
        if not p["name"]:
            p["name"] = _url_name(url) or ("Dropped pin" if p.get("lat") is not None else "")
        if p["name"] or p.get("lat") is not None:
            out.append(p)
    return out


def _parse_geojson(data):
    try:
        d = json.loads(data)
    except ValueError:
        return []
    out = []
    for f in d.get("features", []) if isinstance(d, dict) else []:
        props = f.get("properties") or {}
        loc = props.get("location") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or []
        url = props.get("google_maps_url") or props.get("Google Maps URL") or ""
        note = props.get("Comment") or props.get("comment") or ""
        if note.startswith("No location information"):
            note = ""
        p = {"name": loc.get("name") or props.get("name") or props.get("Title") or "",
             "address": loc.get("address") or props.get("address") or "", "note": note, "gmaps": url}
        q = _url_query(url)          # rows without a location: ?q=<address>&ftid=0x..:0x<cid>
        if q:
            p["query"] = q
            p["address"] = p["address"] or q
            p["name"] = p["name"] or q.split(",")[0].strip()
        if len(coords) >= 2 and (coords[0] or coords[1]):
            p["lat"], p["lng"] = float(coords[1]), float(coords[0])
        else:
            p.update(_from_url(url))
        if not p["name"]:
            p["name"] = p["address"] or ("Dropped pin" if p.get("lat") is not None else "")
        if p["name"]:
            out.append(p)
    return out


def _from_url(url):
    """lat/lng and/or cid from the shapes of Google Maps URLs Takeout uses."""
    out = {}
    if not url:
        return out
    u = urllib.parse.unquote(url)
    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", u) or \
        re.search(r"/maps/search/(-?\d+\.\d+),\s*(-?\d+\.\d+)", u) or \
        re.search(r"[?&](?:q|query|ll)=(-?\d+\.\d+),\s*(-?\d+\.\d+)", u)
    if m:
        out["lat"], out["lng"] = float(m.group(1)), float(m.group(2))
    m = re.search(r"[?&]cid=(\d+)", u)
    if m:
        out["cid"] = m.group(1)
    else:
        m = re.search(r"0x[0-9a-f]+:0x([0-9a-f]+)", u, re.I)
        if m:
            out["cid"] = str(int(m.group(1), 16))
    return out


def _url_query(url):
    """The text of ?q= when it's a place/address rather than coordinates."""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url or "").query).get("q", [""])[0].strip()
    return "" if not q or re.fullmatch(r"-?\d+\.\d+,\s*-?\d+\.\d+", q) else q


def _url_name(url):
    m = re.search(r"/maps/place/([^/@]+)", urllib.parse.unquote(url or ""))
    return m.group(1).replace("+", " ").strip() if m else ""


def import_takeout(path, g, near=None, progress=None):
    """Merge a Takeout export into the store. g = gmaps.Google (for lookups). Returns a summary."""
    lists = parse_takeout(path)
    if not lists:
        raise ValueError("no saved places in that export (include “Saved” and “Maps (your places)”)")
    total = sum(len(v) for v in lists.values())
    d = load()
    done = added = approx = missed = 0
    cache = {}
    for name, items in lists.items():
        lst = d["lists"].setdefault(name, [])
        have = {_key(q) for q in lst} | {q.get("gmaps") for q in lst if q.get("gmaps")}
        for raw in items:
            done += 1
            if progress:
                progress(done, total)
            if raw.get("gmaps") and raw["gmaps"] in have:
                continue
            p = dict(raw)
            if p.get("lat") is None or (not p.get("address") and p.get("cid")):
                hit = _lookup(g, p, near, cache)
                if hit:
                    exact = hit.pop("_exact", False)
                    if p.get("lat") is None or exact:
                        p.update({k: v for k, v in hit.items() if v is not None and k != "name"})
                        p["name"] = p.get("name") or hit.get("name")
                        if not exact:
                            p["approx"] = True
                            approx += 1
            if p.get("lat") is None:
                missed += 1
                continue
            if not p.get("address"):
                p["address"] = "%.5f, %.5f" % (p["lat"], p["lng"])
            p.pop("cid", None)
            q = {k: p.get(k) for k in KEYS if p.get(k) not in (None, "")}
            if _key(q) in have:
                continue
            lst.append(q)
            have.add(_key(q))
            if q.get("gmaps"):
                have.add(q["gmaps"])
            added += 1
        if not lst:
            del d["lists"][name]
    save(d)
    return {"lists": len(lists), "total": total, "added": added, "approx": approx, "missed": missed}


def _lookup(g, p, near, cache):
    """Find the place behind a Takeout row. Prefers the result whose CID matches."""
    q = p.get("query") or p.get("name") or p.get("address")
    if not q:
        return None
    cid = p.get("cid")
    biases = [near, None] if near else [None]
    if p.get("lat") is not None:
        biases = [(p["lat"], p["lng"])]
    first = None
    for b in biases:
        k = (q, b)
        if k not in cache:
            try:
                cache[k] = g.search_cid(q, b)
            except Exception:  # noqa: BLE001 — one bad row must not stop the import
                cache[k] = []
        res = cache[k]
        if cid:
            for r in res:
                if r.get("cid") == cid:
                    return dict(r, _exact=True)
        if res and first is None:
            first = res[0]
        if not cid and first:
            break
    if first and not cid:
        return dict(first, _exact=True)    # nothing better to match on
    return dict(first) if first else None
