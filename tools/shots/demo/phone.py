"""notreal.phone demo: a made-up call history and contact names (555 numbers, fictional people)."""
import datetime
import json
import os
import sqlite3

import nrcalls

os.makedirs(os.path.dirname(nrcalls.RECORDS), exist_ok=True)
os.makedirs(os.path.dirname(nrcalls.CONTACTS), exist_ok=True)
PEOPLE = [("Anna Berg", "+15555550101"), ("Marco Rossi", "+15555550102"), ("Mum", "+15555550103"),
          ("Lea Park", "+15555550104"), ("Tom Ek", "+15555550105")]
with open(nrcalls.CONTACTS, "w") as f:
    json.dump([[n, [[num, num]]] for n, num in PEOPLE], f)
db = sqlite3.connect(nrcalls.RECORDS)
db.execute("CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT, inbound INTEGER, "
           "start BLOB, answered BLOB, end BLOB, protocol TEXT)")
now = datetime.datetime.now(datetime.timezone.utc)
CALLS = [  # who, inbound, minutes ago, answered?, seconds
    (4, 1, 4800, 1, 300), (1, 0, 3000, 1, 95), (3, 1, 1500, 0, 0), (2, 0, 400, 1, 610),
    (0, 1, 140, 1, 42), ("+15555550199", 1, 60, 0, 0), (0, 0, 12, 1, 188),
]
for who, inbound, ago, answered, secs in CALLS:
    num = who if isinstance(who, str) else PEOPLE[who][1]
    s = now - datetime.timedelta(minutes=ago)
    a = s + datetime.timedelta(seconds=6) if answered else None
    e = (a or s) + datetime.timedelta(seconds=secs or 20)
    iso = lambda d: d.isoformat().replace("+00:00", "Z") if d else None
    db.execute("INSERT INTO calls (target, inbound, start, answered, end, protocol) VALUES (?,?,?,?,?,?)",
               (num, inbound, iso(s), iso(a), iso(e), "tel"))
db.commit()
