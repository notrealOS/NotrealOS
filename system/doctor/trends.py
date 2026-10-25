"""Battery-log trends for nrdoctor, its card and the PC diagnosis (10-24).

Reads nrbatteryd's minute segments (~/.local/share/nrbattery/*.jsonl, see ~/battery/nrbatteryd.py):
t, dt, slept, pct, mah, chg, scr, app, skin, top=[[proc, % of a core], ...].

    summary(recs)            -> dict: hours on / off-awake / asleep, % used, %/h each, top apps + processes, heat
    today(), week()          -> records of today / the 7 days before today
    notes()                  -> plain-language findings worth a "minor" on the face (drain spikes, awake at night)
    text_report(days=7)      -> the weekly report as a few lines of text
"""
import datetime
import json
import os
import time

DATA = os.path.expanduser("~/.local/share/nrbattery")
ALIAS = {"chromium-browser": "chromium"}   # window ids without their own .desktop
DESIGN_MAH = 4500
AWAKE_OFF_MIN = 30       # screen off but awake longer than this in 12 h = something kept it up
HOT_SKIN_C = 45


def load(start, end):
    out = []
    d = datetime.date.fromtimestamp(start)
    while d <= datetime.date.fromtimestamp(end):
        try:
            with open(os.path.join(DATA, d.isoformat() + ".jsonl")) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if start < r.get("t", 0) <= end:
                        out.append(r)
        except OSError:
            pass
        d += datetime.timedelta(days=1)
    return out


def midnight(days_ago=0):
    d = datetime.date.today() - datetime.timedelta(days=days_ago)
    return time.mktime(d.timetuple())


def today():
    return load(midnight(), time.time())


def week():
    return load(midnight(7), midnight())


_names = {}


def app_label(app_id):
    """Window id -> the app's name ("dev.notareal.VaporMail" -> "notreal.mail")."""
    if app_id not in _names:
        name = None
        try:
            import gi
            from gi.repository import Gio
            try:
                gi.require_version("GioUnix", "2.0")
                from gi.repository.GioUnix import DesktopAppInfo
            except (ValueError, ImportError):
                DesktopAppInfo = Gio.DesktopAppInfo
            for cand in (app_id, app_id.lower(), ALIAS.get(app_id.lower(), app_id)):
                try:
                    info = DesktopAppInfo.new(cand + ".desktop")
                except TypeError:
                    info = None
                if info:
                    name = info.get_name()
                    break
        except Exception:  # noqa: BLE001 - no GI: raw ids are still readable enough
            pass
        _names[app_id] = name or ("Home / overview" if app_id == "overview" else app_id.split(".")[-1])
    return _names[app_id]


def pct(mah):
    return 100 * mah / DESIGN_MAH


def summary(recs):
    s = {"on": [0.0, 0.0], "off": [0.0, 0.0], "sleep": [0.0, 0.0], "chg": [0.0, 0.0]}
    apps, procs, off_procs = {}, {}, {}
    hot = (0.0, 0)
    for r in recs:
        dt, mah = r.get("dt", 0), r.get("mah") or 0
        if r.get("chg"):
            s["chg"][0] += dt
            continue
        slept = min(r.get("slept", 0), dt)
        awake = dt - slept
        w = slept / (slept + 8 * awake) if dt else 0      # suspended time draws ~1/8 of awake time
        s["sleep"][0] += slept
        s["sleep"][1] += mah * w
        k = "on" if r.get("scr") else "off"
        s[k][0] += awake
        s[k][1] += mah * (1 - w)
        if r.get("scr"):
            a = apps.setdefault(app_label(r.get("app") or "overview"), [0.0, 0.0])
            a[0] += awake
            a[1] += mah * (1 - w)
        for name, cpu in r.get("top", []):
            procs[name] = procs.get(name, 0) + cpu * dt / 100 / 60          # core-minutes
            if not r.get("scr") and awake > dt / 2:
                off_procs[name] = off_procs.get(name, 0) + cpu * dt / 100 / 60
        if (r.get("skin") or 0) > hot[0]:
            hot = (r["skin"], r["t"])

    def rate(k):
        return pct(s[k][1]) / (s[k][0] / 3600) if s[k][0] > 600 else None

    top = lambda d, n: [(k, round(v, 1)) for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]]  # noqa: E731
    return {
        "hours": {k: round(v[0] / 3600, 2) for k, v in s.items()},
        "used_pct": {k: round(pct(v[1]), 1) for k, v in s.items() if k != "chg"},
        "rate": {k: (round(rate(k), 1) if rate(k) is not None else None) for k in ("on", "off", "sleep")},
        "apps": [(k, round(v[0] / 60), round(pct(v[1]), 1)) for k, v in
                 sorted(apps.items(), key=lambda kv: -kv[1][1])[:5]],
        "procs": top(procs, 5),
        "awake_off_procs": top(off_procs, 3),
        "hottest": {"skin": hot[0], "at": time.strftime("%H:%M", time.localtime(hot[1])) if hot[1] else ""},
    }


def awake_screen_off(hours=12):
    """Minutes the phone stayed awake with the screen off (not charging) in the last `hours`."""
    recs = load(time.time() - hours * 3600, time.time())
    mins = sum(r.get("dt", 0) - min(r.get("slept", 0), r.get("dt", 0)) for r in recs
               if not r.get("scr") and not r.get("chg")) / 60
    return mins, summary(recs)["awake_off_procs"]


def notes():
    out = []
    mins, who = awake_screen_off()
    if mins > AWAKE_OFF_MIN:
        culprit = f" (busiest: {who[0][0]})" if who else ""
        out.append(f"stayed awake {mins:.0f} min with the screen off in the last 12 h{culprit}")
    t, w = summary(today()), summary(week())
    for k, label in (("off", "screen-off drain"), ("sleep", "sleep drain")):
        a, b = t["rate"][k], w["rate"][k]
        if a and b and a > 2 * b and a > 2:
            out.append(f"{label} today {a:.1f} %/h, usually {b:.1f} %/h")
    recent = load(time.time() - 3600, time.time())
    hot = max(recent, key=lambda r: r.get("skin") or 0, default=None)
    if hot and (hot.get("skin") or 0) >= HOT_SKIN_C:
        busy = summary(recent)["procs"]
        out.append(f"got hot in the last hour ({hot['skin']:.0f} °C" + (f", busiest: {busy[0][0]})" if busy else ")"))
    return out


def text_report(days=7):
    recs = load(midnight(days), time.time())
    if not recs:
        return "No battery data yet."
    s = summary(recs)
    h, u, r = s["hours"], s["used_pct"], s["rate"]
    lines = [f"Last {days} days: screen on {h['on']:.1f} h ({r['on'] or 0:.0f} %/h), "
             f"asleep {h['sleep']:.1f} h ({r['sleep'] or 0:.1f} %/h), screen off but awake {h['off']:.1f} h."]
    used = sum(u.values())
    if used > 0:
        lines.append(f"Battery went {u['on'] / used * 100:.0f} % to you using it, {100 - u['on'] / used * 100:.0f} % to the phone on its own.")
    if s["apps"]:
        lines.append("Top apps: " + ", ".join(f"{a} ({m} min, {p:.0f} %)" for a, m, p in s["apps"][:3]) + ".")
    if s["procs"]:
        lines.append("Busiest processes: " + ", ".join(f"{p} ({c:.0f} CPU-min)" for p, c in s["procs"][:3]) + ".")
    if s["hottest"]["skin"]:
        lines.append(f"Hottest: {s['hottest']['skin']:.0f} °C.")
    return "\n".join(lines)


if __name__ == "__main__":
    print(json.dumps(summary(today()), indent=1))
    print(notes())
    print(text_report())
