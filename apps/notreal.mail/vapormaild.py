#!/usr/bin/env python3
"""vaporMail background daemon.

- every FETCH_S (and on SIGUSR1 = "fetch now") looks for new INBOX mail and notifies;
- brings snoozed mail back into the inbox when it is due, with a notification + chime.
Tapping a notification opens vaporMail on that message.
"""
import os
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gmail  # noqa: E402
import graph  # noqa: E402

FETCH_S = 15 * 60
RETRY_S = 2 * 60
TICK_S = 30            # snooze due-time resolution
MAX_NOTIFY = 5         # more new mails than this -> one summary notification
APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vapormail")
ICON = "dev.notareal.VaporMail"

wake = threading.Event()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def notify(title, body, msgid=None, sound="message-new-email"):
    subprocess.Popen(["fbcli", "-E", sound], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def run():
        try:
            out = subprocess.run(["notify-send", "-a", "notreal.mail", "-i", ICON,
                                  "-h", "string:desktop-entry:dev.notareal.VaporMail",
                                  "-A", "default=Open", title, body],
                                 capture_output=True, text=True, timeout=24 * 3600).stdout
        except (OSError, subprocess.TimeoutExpired):
            return
        if out.strip() == "default":
            subprocess.Popen([APP] + (["--open", msgid] if msgid else []),
                             start_new_session=True)
    threading.Thread(target=run, daemon=True).start()


def check_new(mail):
    with gmail.locked():
        st = gmail.load_state()
    validity, msgs = mail.list_folder("INBOX", limit=gmail.LIST_LIMIT, since_uid=0 if not st else st.get("last_uid", 0))
    if not msgs:
        return
    top = max(m["uid"] for m in msgs)
    if not st or st.get("uidvalidity") != validity:
        log("first run / uidvalidity changed: baseline uid", top)
        gmail.mark_listed(validity, top)
        return
    fresh = [m for m in msgs if m["uid"] > st.get("last_uid", 0) and not m["seen"]]
    gmail.mark_listed(validity, top)
    log("new:", len(fresh))
    if not fresh:
        return
    mail.prefetch(fresh)
    if len(fresh) > MAX_NOTIFY:
        notify("%d new mails" % len(fresh),
               ", ".join(sorted({m["from_name"] for m in fresh}))[:200])
        return
    for m in reversed(fresh):  # oldest first, newest ends on top
        notify(m["from_name"], "%s\n%s" % (m["subject"], m["snippet"][:120]), m["msgid"])


def check_new_other(acct):
    """Microsoft accounts: notify unread inbox mail newer than what was already seen."""
    with gmail.locked():
        seen_until = gmail.load_state().get("other", {}).get(acct.key)
    _v, msgs = acct.list_folder("INBOX", limit=25)
    if not msgs:
        return
    newest = max(m["date"] for m in msgs)
    if seen_until is None:
        log(acct.key, "first run: baseline")
        gmail.mark_listed_other(acct.key, newest)
        return
    fresh = [m for m in msgs if m["date"] > seen_until and not m["seen"]]
    gmail.mark_listed_other(acct.key, newest)
    log(acct.key, "new:", len(fresh))
    tag = acct.address.split("@")[-1].split(".")[0]
    if len(fresh) > MAX_NOTIFY:
        notify("%d new mails · %s" % (len(fresh), tag),
               ", ".join(sorted({m["from_name"] for m in fresh}))[:200])
        return
    for m in reversed(fresh):
        notify("%s · %s" % (m["from_name"], tag), "%s\n%s" % (m["subject"], m["snippet"][:120]), m["msgid"])


def other_accounts():
    try:
        return [graph.Graph(ident) for ident, _ in graph.goa_accounts()]
    except Exception as e:  # noqa: BLE001
        log("goa:", e)
        return []


def check_snoozed(mail, others=()):
    now = time.time()
    due = [s for s in gmail.load_snoozed() if s["due"] <= now]
    by_key = {a.key: a for a in others}
    for s in due:
        try:
            by_key.get(s.get("acct") or "gmail", mail).unsnooze(s["msgid"])
        except gmail.MailError as e:
            log("unsnooze failed:", s["msgid"], e)
            continue
        if gmail.remove_snoozed(s["msgid"]):
            log("back from snooze:", s["subject"])
            notify("⏰ " + s["from_name"], s["subject"], s["msgid"], sound="alarm-clock-elapsed")


def main():
    signal.signal(signal.SIGUSR1, lambda *_: (setattr(main, "fetch_now", True), wake.set()))
    main.fetch_now = False
    mail = None
    others = other_accounts()
    next_fetch = 0
    while True:
        if mail is None:
            mail = gmail.Gmail.from_config()
            if mail is None:
                log("no account yet (open vaporMail to sign in)")
        if mail is not None:
            try:
                if gmail.load_snoozed():
                    check_snoozed(mail, others)
                if main.fetch_now or time.time() >= next_fetch:
                    main.fetch_now = False
                    others = other_accounts() or others   # picks up accounts added in Settings
                    for a in others:
                        try:
                            check_new_other(a)
                        except gmail.MailError as e:
                            log(a.key, "error:", e)
                    check_new(mail)
                    next_fetch = time.time() + FETCH_S
                mail.close()  # don't hold an IMAP connection between ticks
            except gmail.MailError as e:
                log("error:", e)
                next_fetch = time.time() + RETRY_S
                if "Login failed" in str(e):
                    mail = None  # password changed? re-read the keyring
            except Exception as e:  # noqa: BLE001 - keep the daemon alive
                log("error:", repr(e))
                mail.close()
                next_fetch = time.time() + RETRY_S
        wake.wait(TICK_S)
        wake.clear()


if __name__ == "__main__":
    main()
