"""notreal.mail demo: a made-up inbox, no network, no keyring."""
import email.message
import time

import gmail

NOW = time.time()
PEOPLE = [
    ("Anna Berg", "anna@example.com", "Saturday ride?", "Weather looks great, meet at the bridge at 9? Bring the spare tube"),
    ("Studio Lumen", "hello@example.com", "Your prints are ready", "All 12 prints passed the colour check and can be picked up from tomorrow"),
    ("Marco Rossi", "marco@example.com", "Re: synth patches", "Sent you the presets, the third one is the warm pad from the demo"),
    ("Hackspace", "events@example.com", "Thursday: soldering night", "Bring your own boards, we have irons, flux and pizza"),
    ("Lea Park", "lea@example.com", "Photos from the trip", "Finally sorted them! A few of the lake are my favourites"),
    ("Bank", "noreply@example.com", "Your monthly statement", "Your statement for September is ready to view in the app"),
    ("Tom Ek", "tom@example.com", "Lunch next week", "Tuesday or Wednesday works for me, the new ramen place?"),
    ("Linux Weekly", "news@example.com", "Mainline phones are getting good", "This week: cameras, calls and battery life on mainline Linux phones"),
]


def fake(i, p, folder="INBOX"):
    name, addr, subj, snip = p
    return {"acct": "gmail", "uid": 1000 - i, "msgid": "<demo%d@example.com>" % i, "message_id": "<demo%d@example.com>" % i,
            "thrid": "t%d" % i, "folder": folder, "from_name": name, "from_addr": addr, "to": "you@example.com", "cc": "",
            "reply_to": "", "references": "", "subject": subj, "snippet": snip, "seen": i > 2, "size": 4000,
            "date": NOW - i * 5400}


def list_folder(self, folder="INBOX", limit=60, since_uid=0):
    return 1, ([fake(i, p, folder) for i, p in enumerate(PEOPLE)] if folder in ("INBOX", "inbox") else [])


def get_raw(self, msg):
    m = email.message.EmailMessage()
    m["From"] = "%s <%s>" % (msg["from_name"], msg["from_addr"])
    m["To"] = "you@example.com"
    m["Subject"] = msg["subject"]
    m.set_content(msg["snippet"] + ".\n\nSee you soon!\n")
    return m.as_bytes()


gmail.get_password = lambda addr: "demo"
gmail.load_config = lambda: {"address": "you@example.com", "name": "You", "signature": ""}
gmail.Gmail.list_folder = list_folder
gmail.Gmail.list_sent = lambda self, limit=60: []
gmail.Gmail.prefetch = lambda self, msgs: None
gmail.Gmail.get_raw = get_raw
gmail.Gmail.check_login = lambda self: True
gmail.Gmail.special = lambda self, flag: None
