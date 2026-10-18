#!/usr/bin/env python3
"""Log the Instagram bridge in with the Instagram session from this phone's Firefox.

Run after `login instagram` in the "Instagram bridge" room (the bridge then waits for
cookies). Reads the instagram.com cookies from Firefox, sends them as JSON to the bridge
room on our own Synapse, prints the bridge's answer, and redacts the cookie message.
"""
import glob
import json
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request

ROOM = "!esDHfReU74gTIi3-Df1qduVeh9pqjtJNxl1odxpzGqg"   # Instagram bridge management room
NAMES = ("sessionid", "csrftoken", "ds_user_id", "mid", "ig_did", "datr")


def firefox_cookies():
    db = max(glob.glob(os.path.expanduser("~/.mozilla/firefox/*/cookies.sqlite")), key=os.path.getmtime)
    with tempfile.TemporaryDirectory() as tmp:   # Firefox keeps the db locked: read a copy
        for f in glob.glob(db + "*"):
            shutil.copy(f, tmp)
        con = sqlite3.connect(os.path.join(tmp, "cookies.sqlite"))
        ck = dict(con.execute("select name, value from moz_cookies where host like ? "
                              "and name in (" + ",".join("?" * len(NAMES)) + ")",
                              ("%instagram.com", *NAMES)))
        con.close()
    return ck


def main():
    ck = firefox_cookies()
    if "sessionid" not in ck:
        raise SystemExit("No Instagram session in Firefox: log in on instagram.com there first.")
    s = json.load(open(os.path.expanduser("~/.config/notarealchat/session.json")))
    tok = s.get("access_token") or s.get("token")
    hs = s.get("homeserver") or s.get("base_url") or json.load(open(os.path.expanduser("~/.config/notreal/pc.json")))["matrix"]
    headers = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
    room = urllib.parse.quote(ROOM)

    def req(path, data=None, method="GET"):
        r = urllib.request.Request(hs + path, data=data, headers=headers, method=method)
        return json.load(urllib.request.urlopen(r, timeout=60))

    body = json.dumps({"msgtype": "m.text", "body": json.dumps(ck)}).encode()
    sent = req(f"/_matrix/client/v3/rooms/{room}/send/m.room.message/ig{int(time.time())}", body, "PUT")["event_id"]
    print("Sent cookies:", ", ".join(sorted(ck)))
    reply = []
    for _ in range(20):
        time.sleep(3)
        for e in req(f"/_matrix/client/v3/rooms/{room}/messages?dir=b&limit=8")["chunk"]:
            if e["event_id"] == sent:
                break
            if e["type"] == "m.room.message":
                reply.append(e["content"].get("body", ""))
        if reply:
            break
    print("Bridge:", "\n".join(reversed(reply)) or "(no answer within 60 s; check the room)")
    req(f"/_matrix/client/v3/rooms/{room}/redact/{urllib.parse.quote(sent)}/r{int(time.time())}", b"{}", "PUT")
    print("Cookie message removed from the room history.")


if __name__ == "__main__":
    main()
