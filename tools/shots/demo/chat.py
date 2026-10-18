"""notreal.chat demo: made-up chats in a fresh database; no Matrix server, no modem."""
import json
import os
import time

import matrix
import sms as smsmod
from store import Store

os.makedirs(os.path.dirname(matrix.SESSION), exist_ok=True)
with open(matrix.SESSION, "w") as f:
    json.dump({"token": "demo", "user": "@you:example.com"}, f)
matrix.Matrix.start = lambda self: None
matrix.Matrix._detect_selves = lambda self: None
smsmod.Sms.start = lambda self: None
smsmod.import_chatty = lambda store, name_for: None

NOW = int(time.time() * 1000)
MIN = 60_000
CHATS = [  # id, network, name, unread, minutes ago, messages (mine?, text)
    ("!anna", "wa", "Anna Berg", 2, 3, [(0, "Are you coming on Saturday?"), (1, "Yes! Which bridge?"),
                                        (0, "The old stone one, 9:00"), (0, "Bring the spare tube 🚲")]),
    ("!band", "wa", "Synth club", 5, 11, [(0, "New patch is up in the drive"), (1, "That pad is huge"),
                                          (0, "Rehearsal Thursday?")]),
    ("!marco", "fb", "Marco Rossi", 0, 42, [(1, "Thanks for the presets!"), (0, "Any time 👍")]),
    ("!lea", "ig", "Lea Park", 1, 95, [(0, "Look at this lake 😍")]),
    ("!mum", "sms", "Mum", 0, 180, [(0, "Call me when you land"), (1, "Landed, calling in 10")]),
    ("!hack", "wa", "Hackspace", 0, 600, [(0, "Pizza is here")]),
    ("!tom", "fb", "Tom Ek", 0, 1500, [(1, "Ramen on Tuesday?"), (0, "Deal")]),
]
st = Store()
for cid, net, name, unread, ago, msgs in CHATS:
    ts0 = NOW - ago * MIN - len(msgs) * MIN
    for i, (mine, text) in enumerate(msgs):
        st.add_message(cid, "%s-%d" % (cid, i), "@you:example.com" if mine else "@%s:example.com" % cid[1:],
                       ts0 + i * MIN, "text", text, mine=bool(mine))
    st.upsert_chat(cid, net=net, name=name, unread=unread, dm=int(cid != "!band" and cid != "!hack"),
                   last_ts=ts0 + (len(msgs) - 1) * MIN, last_text=msgs[-1][1], last_mine=msgs[-1][0])
