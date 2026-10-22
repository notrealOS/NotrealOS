#!/usr/bin/env python3
"""calremind: calendar reminders -> ~/.local/share/alarmd/calendar.json for alarmd to fire.

Reads the next LOOKAHEAD of events from every calendar (EDS, through ~/calendar/cal.py), turns
each relative VALARM into a due time and writes [{"due", "summary", "text", "key"}].
Run by alarmd every 15 min + after a resume, and by notreal.calendar after a save/delete.
alarmd removes an entry once it has fired; entries already due but not fired yet are kept,
so a rewrite at the wrong moment can't swallow a reminder.
"""
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.expanduser("~/calendar"))
import cal  # noqa: E402

OUT = os.path.expanduser("~/.local/share/alarmd/calendar.json")
LOOKAHEAD = datetime.timedelta(days=3)     # longest choice is 2 days before + its 9:00


def when(ev):
    if ev["all_day"]:
        return "all day"
    return ev["start"].strftime("%H:%M") + ("–" + ev["end"].strftime("%H:%M") if ev["end"] > ev["start"] else "")


def main():
    now = time.time()
    try:
        with open(OUT) as f:
            keep = [r for r in json.load(f) if r["due"] <= now]   # due, not fired yet
    except (OSError, ValueError):
        keep = []
    store = cal.Store()
    store.connect_all()
    start = datetime.datetime.now() - datetime.timedelta(days=1)
    items = {r["key"]: r for r in keep}
    for ev in store.events(start, datetime.datetime.now() + LOOKAHEAD):
        for mins in ev["remind"]:
            due = ev["start"].timestamp() - mins * 60
            if due <= now:
                continue
            key = f"{ev['cal']}|{ev['uid']}|{ev['rid']}|{mins}"
            day = "" if ev["start"].date() == datetime.date.fromtimestamp(due) else ev["start"].strftime("%a %d %b · ")
            text = day + when(ev) + (f" · {ev['location']}" if ev["location"] else "")
            items[key] = {"due": due, "summary": ev["title"], "text": text, "key": key}
    out = sorted(items.values(), key=lambda r: r["due"])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    print(f"calremind: {len(out)} reminders", flush=True)


if __name__ == "__main__":
    main()
