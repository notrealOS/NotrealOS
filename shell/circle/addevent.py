#!/usr/bin/env python3
"""Add one event to the user's calendar (EDS, via notarealCalendar's cal.py). circled runs this
in a subprocess so the ECal/EDS stack stays out of the overlay process.

stdin: {"title", "start": "YYYY-MM-DD" | "YYYY-MM-DDTHH:MM", "end": same (optional),
        "all_day": bool, "tentative": bool, "location", "notes"}
stdout: the calendar's name. Default calendar: notarealCalendar's last used, else the main Google one.
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.expanduser("~/calendar"))
import cal  # noqa: E402
from cal import ICalGLib  # noqa: E402

CONF = os.path.expanduser("~/.config/nrcalendar/config.json")


def parse(s):
    return datetime.datetime.fromisoformat(s.strip())


def main():
    ev = json.load(sys.stdin)
    start = parse(ev["start"])
    all_day = bool(ev.get("all_day")) or len(ev["start"].strip()) == 10
    end = parse(ev["end"]) if ev.get("end") else start + datetime.timedelta(hours=1)
    if all_day and len(ev.get("end", "").strip()) == 10:
        end += datetime.timedelta(days=1)          # "until Dec 1" includes Dec 1; DTEND is exclusive
    store = cal.Store()
    store.connect_all()
    writable = store.writable()
    if not writable:
        sys.exit("no writable calendar")
    try:
        with open(CONF) as f:
            last = json.load(f).get("last_cal")
    except (OSError, ValueError):
        last = None
    google = [c for c in writable if c.service == "Google"]
    google.sort(key=lambda c: "@" not in c.name)   # the account's main calendar (named after the address) first
    c = store.by_uid(last) if last in [w.uid for w in writable] else (google or writable)[0]
    title = ev.get("title") or "Event"
    uid = store.create(c.uid, title, start, end, all_day, ev.get("location", ""), ev.get("notes", ""))
    if ev.get("tentative") and uid:
        ok, comp = c.client.get_object_sync(uid, None, None)
        comp.set_status(ICalGLib.PropertyStatus.TENTATIVE)
        c.client.modify_object_sync(comp, cal.ECal.ObjModType.ALL, cal.ECal.OperationFlags.NONE, None)
    print(c.name)


if __name__ == "__main__":
    main()
