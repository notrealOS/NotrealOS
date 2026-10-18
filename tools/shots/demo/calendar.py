"""notreal.calendar demo: two made-up calendars with a week of events (no Evolution, no accounts)."""
import datetime

import cal


class FakeCal:
    def __init__(self, uid, name, account, color, backend):
        self.uid, self.name, self.account, self.color, self.backend = uid, name, account, color, backend
        self.client, self.readonly, self.src = object(), False, None

    service = cal.Calendar.service

    def connect(self):
        pass


CALS = [FakeCal("work", "Work", "you@example.com", "#e2572b", "caldav"),
        FakeCal("home", "Personal", "you@example.com", "#5b8def", "caldav")]
D = datetime.date.today()
SAT = (5 - D.weekday()) % 7 or 7          # the coming Saturday, whatever day the demo runs
EVENTS = [  # day offset, hour, minutes, length (min), title, cal, location
    (0, 9, 0, 30, "Stand-up", "work", ""), (0, 12, 30, 60, "Lunch with Tom", "home", "Ramen bar"),
    (0, 18, 0, 90, "Synth club rehearsal", "home", "Studio 4"), (1, 10, 0, 60, "Design review", "work", "Room Orbit"),
    (1, 17, 30, 45, "Climbing", "home", ""), (2, 8, 0, 0, "Dentist", "home", ""), (3, 9, 0, 480, "Hackspace day", "home", ""),
    (SAT, 9, 0, 180, "Saturday ride", "home", "Old stone bridge"), (SAT + 1, 0, 0, -1, "Mum's birthday", "home", ""),
]


def events(self, start, end, hidden=()):
    out = []
    for i, (dd, h, m, length, title, c, loc) in enumerate(EVENTS):
        day = D + datetime.timedelta(days=dd)
        all_day = length < 0
        s = datetime.datetime.combine(day, datetime.time(h, m))
        e = s + datetime.timedelta(days=1) if all_day else s + datetime.timedelta(minutes=max(length, 15))
        if start <= s < end and c not in hidden:
            out.append({"cal": c, "uid": "ev%d" % i, "rid": None, "title": title, "start": s, "end": e,
                        "all_day": all_day, "location": loc, "notes": "", "recurring": False})
    return sorted(out, key=lambda e: (e["start"], not e["all_day"]))


def init(self):
    self.registry, self.cals = None, list(CALS)


cal.Store.__init__ = init
cal.Store.connect_all = lambda self: None
cal.Store.events = events
