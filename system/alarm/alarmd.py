#!/usr/bin/env python3
"""alarmd - alarms that ring even when the phone is in deep sleep (s2idle).

Alarms live in ~/.local/share/alarmd/alarms.json  [{"id", "at" (epoch), "label"}]; voiced writes
it ("wake me up at 7"), this daemon watches it.
- The PMIC RTC (rtc0) wakes the phone WAKE_EARLY s before the next alarm. Its clock isn't wall time
  (it counts from 1970 at boot), so the alarm is written relative: "+<seconds>". Writing needs
  ~/alarm/install.sh once (udev rule: wakealarm owned by this user).
- Timers here use WALL time polled every POLL_S: GLib timeouts run on the monotonic clock, which
  stops during suspend.
- Shortly before an alarm a logind "sleep" block inhibitor keeps the phone awake until it is done.
- Ringing: chime loop on the speaker (volume raised, restored after), Glyph bars pulse, screen woken,
  notification with Stop / Snooze. Gives up after RING_MAX_S.
- Reminders (10-12, moved here from notarealShell): ~/.local/share/phantomshell/reminders.json
  [{"due" (epoch), "text"}], written by voiced and the shell's search box, shown by the overview
  widget. They wake the phone from sleep like alarms, then chime + vibrate, read the text aloud
  (piper) and post a notarealShell notification (glyphd lights its reminder LED for that app name).
"""
import json
import os
import signal
import subprocess
import threading
import time
import urllib.request
import uuid

from gi.repository import Gio, GLib, GLibUnix


def pc(key, default=""):
    """Address of our PC server (see docs/pc-link.md); set in ~/.config/notreal/pc.json."""
    try:
        with open(os.path.expanduser("~/.config/notreal/pc.json")) as f:
            return json.load(f).get(key) or default
    except (OSError, ValueError):
        return default

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.expanduser("~/.local/share/alarmd")
ALARMS = os.environ.get("ALARMD_FILE", os.path.join(DATA, "alarms.json"))   # env: tests
CHIME = os.path.join(HERE, "chime.wav")
WAKEALARM = "/sys/class/rtc/rtc0/wakealarm"
SPEAKER = os.environ.get("ALARMD_SINK", "alsa_output.platform-sound.HiFi__Speaker__sink")   # env: tests
GLYPH = "/sys/class/leds/glyph:bar-%d/brightness"
POLL_S = 10
WAKE_EARLY = 60       # RTC wakes the phone this long before the alarm
HOLD_BEFORE = 150     # take the sleep inhibitor this long before the alarm
RING_MAX_S = 600
SNOOZE_S = 600
RING_VOLUME = 70      # % speaker volume while ringing (if it was lower)
REMINDERS = os.path.expanduser("~/.local/share/phantomshell/reminders.json")
PIPER_VOICE = os.path.expanduser("~/voice/models/en_US-amy-medium.onnx")
PC_TTS = "http://%s:8097/tts" % pc("host", "localhost")   # Kokoro on the PC (same voice as voiced); piper is the fallback
FIRE_HOLD_S = 25      # stay awake this long after a reminder fires (sound + speech)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load():
    try:
        with open(ALARMS) as f:
            return [a for a in json.load(f) if isinstance(a, dict) and "at" in a]
    except (OSError, ValueError):
        return []


def save(alarms):
    os.makedirs(DATA, exist_ok=True)
    tmp = ALARMS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(alarms, key=lambda a: a["at"]), f, indent=1)
    os.replace(tmp, ALARMS)


def load_reminders():
    try:
        with open(REMINDERS) as f:
            return [r for r in json.load(f) if isinstance(r, dict) and "due" in r]
    except (OSError, ValueError):
        return []


def save_reminders(items):
    os.makedirs(os.path.dirname(REMINDERS), exist_ok=True)
    tmp = REMINDERS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(items, key=lambda r: r["due"]), f, indent=1)
    os.replace(tmp, REMINDERS)


def add_alarm(at, label=""):
    """Used by voiced (import alarmd) as well."""
    alarms = load()
    alarms.append({"id": uuid.uuid4().hex[:8], "at": float(at), "label": label})
    save(alarms)


class Alarmd:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
        self.inhibit_fd = None
        self.ringing = None          # the alarm dict while ringing
        self.ring_proc = None
        self.ring_stop = threading.Event()
        self.saved_volume = None
        self.armed_for = None
        self.warned = False
        self.exact = set()           # alarm ids with a precise timer already scheduled
        os.makedirs(DATA, exist_ok=True)
        if not os.path.exists(ALARMS):
            save([])
        self.mon = Gio.File.new_for_path(ALARMS).monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.mon.connect("changed", lambda *a: GLib.timeout_add(300, self._changed))
        self.fired_until = 0.0
        self.rmon = Gio.File.new_for_path(REMINDERS).monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.rmon.connect("changed", lambda *a: GLib.timeout_add(300, self._changed))
        # after a resume, re-check right away (the poll timer was frozen during suspend)
        self.bus.signal_subscribe("org.freedesktop.login1", "org.freedesktop.login1.Manager",
                                  "PrepareForSleep", "/org/freedesktop/login1", None,
                                  Gio.DBusSignalFlags.NONE, self._on_sleep)
        GLib.timeout_add_seconds(POLL_S, self.poll)
        self._changed()

    # --- schedule ------------------------------------------------------------------------
    def _changed(self):
        self.arm()
        self.poll()
        return False

    def _on_sleep(self, _c, _s, _p, _i, _sig, params):
        going = params.unpack()[0]
        if going:
            self.arm()          # make sure the RTC is set for the next alarm
        else:
            log("resumed")
            GLib.idle_add(lambda: (self.poll(), False)[1])   # poll() returns True (for the repeating timer): an idle must not

    def arm(self):
        """Set the RTC to wake the phone WAKE_EARLY s before the next alarm."""
        now = time.time()
        upcoming = [a["at"] for a in load() if a["at"] > now] + [r["due"] for r in load_reminders() if r["due"] > now]
        if not upcoming:
            self._write_wake("0")
            self.armed_for = None
            return
        nxt = min(upcoming)
        secs = int(nxt - now - WAKE_EARLY)
        if secs < 20:
            return               # too close: the inhibitor keeps us awake instead
        if self._write_wake("0") and self._write_wake("+%d" % secs):
            self.armed_for = nxt
            log("rtc armed: wake in %d s (alarm %s)" % (secs, time.strftime("%a %H:%M", time.localtime(nxt))))

    def _write_wake(self, value):
        try:
            with open(WAKEALARM, "w") as f:
                f.write(value)
            return True
        except OSError as e:
            if not self.warned:
                self.warned = True
                log("can't set the RTC wake alarm:", e)
                subprocess.Popen(["notify-send", "-a", "Alarm", "Alarms can't wake the phone yet",
                                  "Run once in the terminal:  sudo ~/alarm/install.sh\n"
                                  "(until then alarms only ring while the phone is awake or charging)"])
            return False

    def poll(self):
        now = time.time()
        alarms = load()
        due = [a for a in alarms if a["at"] <= now]
        soon = [a for a in alarms if now < a["at"] <= now + HOLD_BEFORE]
        reminders = load_reminders()
        r_due = [r for r in reminders if r["due"] <= now]
        r_soon = [r for r in reminders if now < r["due"] <= now + HOLD_BEFORE]
        if r_due:
            save_reminders([r for r in reminders if r["due"] > now])
            for r in r_due:
                self.fire_reminder(r, late=now - r["due"] > 90)
            self.fired_until = now + FIRE_HOLD_S
            GLib.timeout_add_seconds(FIRE_HOLD_S + 1, lambda: (self.poll(), False)[1])
            self.arm()
        soon += [{"id": "r%s" % r["due"], "at": r["due"]} for r in r_soon]
        busy = now < self.fired_until
        if (soon or due or self.ringing or busy) and self.inhibit_fd is None:
            self._inhibit(True)
        for a in soon:                      # awake + inhibited now: an exact one-shot timer
            if a["id"] not in self.exact:
                self.exact.add(a["id"])
                GLib.timeout_add(max(0, int((a["at"] - now) * 1000)), lambda: (self.poll(), False)[1])
        if due and not self.ringing:
            a = min(due, key=lambda x: x["at"])
            save([x for x in alarms if x["id"] != a["id"]])
            if now - a["at"] < 3 * 3600:      # phone was off for hours: don't ring stale alarms
                self.ring(a)
            self.arm()
        elif not soon and not due and not self.ringing and not busy and self.inhibit_fd is not None:
            self._inhibit(False)
        return True

    @staticmethod
    def fire_reminder(r, late=False):
        text = r.get("text", "")
        log("REMINDER", r)
        Alarmd._wake_screen()
        # alarm sound + vibration (feedbackd), then the reminder read aloud (Kokoro on the PC, else piper)
        threading.Thread(target=Alarmd._say_reminder, args=(text,), daemon=True).start()
        try:
            Gio.bus_get_sync(Gio.BusType.SESSION, None).call_sync(
                "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                "org.freedesktop.Notifications", "Notify",
                GLib.Variant("(susssasa{sv}i)", (
                    "notarealShell", 0, "alarm-symbolic", "Reminder" + (" (late, phone was off)" if late else ""),
                    text, [], {"urgency": GLib.Variant("y", 2),
                               "category": GLib.Variant("s", "x-phantomshell.reminder")}, -1)),
                None, Gio.DBusCallFlags.NONE, 2000, None)
        except GLib.Error as e:
            log("notify failed:", e.message)

    @staticmethod
    def _say_reminder(text):
        wav = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "alarmd-reminder.wav")
        subprocess.run(["sh", "-c", "fbcli -E alarm-clock-elapsed -t 4 >/dev/null 2>&1"])
        time.sleep(1)
        try:
            body = json.dumps({"text": "Reminder: " + text, "voice": "af_heart", "speed": 1.05}).encode()
            req = urllib.request.Request(PC_TTS, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                audio = resp.read()
            with open(wav, "wb") as f:
                f.write(audio)
        except Exception as e:
            log("pc tts unavailable, using piper:", e)
            if not os.path.exists(PIPER_VOICE):
                return
            subprocess.run(["sh", "-c", 'printf "Reminder: %s" "$1" | piper -m "$2" --length-scale 0.88 -f "$3"',
                            "sh", text, PIPER_VOICE, wav], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["paplay", wav], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # --- keep awake --------------------------------------------------------------------------
    def _inhibit(self, on):
        if on:
            try:
                r, fds = self.bus.call_with_unix_fd_list_sync(
                    "org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                    "Inhibit", GLib.Variant("(ssss)", ("sleep:idle", "alarmd", "alarm due", "block")),
                    GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, 3000, None, None)
                self.inhibit_fd = fds.get(r.unpack()[0])
                log("holding sleep inhibitor")
            except GLib.Error as e:
                log("inhibit failed:", e.message)
        elif self.inhibit_fd is not None:
            os.close(self.inhibit_fd)
            self.inhibit_fd = None
            log("released sleep inhibitor")

    # --- ringing -------------------------------------------------------------------------------
    def ring(self, a):
        log("RING", a)
        self.ringing = a
        self.ring_stop.clear()
        self._wake_screen()
        self._raise_volume()
        threading.Thread(target=self._sound_loop, daemon=True).start()
        threading.Thread(target=self._glyph_loop, daemon=True).start()
        threading.Thread(target=self._ask, args=(a,), daemon=True).start()
        GLib.timeout_add_seconds(RING_MAX_S, lambda: (self.stop("timeout") if self.ringing is a else None, False)[1])

    def _ask(self, a):
        when = time.strftime("%H:%M", time.localtime(a["at"]))
        # -p prints the notification id first, then (with --wait) the chosen action
        proc = subprocess.Popen(["notify-send", "-p", "-a", "Alarm", "-u", "critical", "-t", str(RING_MAX_S * 1000),
                                 "-A", "stop=Stop", "-A", "snooze=Snooze 10 min",
                                 "⏰ " + when, a.get("label") or "Wake up"],
                                stdout=subprocess.PIPE, text=True)
        self.notif_proc = proc
        first = proc.stdout.readline().strip()
        self.notif_id = int(first) if first.isdigit() else None
        out = proc.stdout.readline().strip()
        proc.wait()
        GLib.idle_add(lambda: (self.stop(out or "dismissed") if self.ringing is a else None, False)[1])

    def _close_notification(self):
        """The alarm ended some other way (timeout, exit): don't leave its banner on screen."""
        proc, nid = getattr(self, "notif_proc", None), getattr(self, "notif_id", None)
        self.notif_proc = self.notif_id = None
        if nid:
            subprocess.run(["gdbus", "call", "--session", "-d", "org.freedesktop.Notifications",
                            "-o", "/org/freedesktop/Notifications", "-m",
                            "org.freedesktop.Notifications.CloseNotification", str(nid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        if proc and proc.poll() is None:
            proc.kill()

    def stop(self, why):
        a, self.ringing = self.ringing, None
        if not a:
            return
        log("stop:", why)
        self.ring_stop.set()
        if self.ring_proc:
            self.ring_proc.kill()
        self._close_notification()
        self._restore_volume()
        if why == "snooze":
            add_alarm(time.time() + SNOOZE_S, a.get("label", ""))
            subprocess.Popen(["notify-send", "-a", "Alarm", "Snoozed",
                              "Ringing again at " + time.strftime("%H:%M", time.localtime(time.time() + SNOOZE_S))])
        self.arm()
        self.poll()

    def _sound_loop(self):
        while not self.ring_stop.is_set():
            self.ring_proc = subprocess.Popen(["paplay", "--device=" + SPEAKER, CHIME])
            self.ring_proc.wait()
            self.ring_stop.wait(0.4)

    def _glyph_loop(self):
        bars = [i for i in range(1, 8) if os.path.exists(GLYPH % i)]
        k = 0
        while not self.ring_stop.is_set():
            for i in bars:
                self._glyph(i, 255 if (i + k) % 2 else 0)
            k += 1
            self.ring_stop.wait(0.5)
        for i in bars:
            self._glyph(i, 0)

    @staticmethod
    def _glyph(i, v):
        try:
            with open(GLYPH % i, "w") as f:
                f.write(str(v))
        except OSError:
            pass

    def _raise_volume(self):
        try:
            out = subprocess.run(["pactl", "get-sink-volume", SPEAKER], capture_output=True, text=True,
                                 timeout=3).stdout
            cur = int(out.split("/")[1].strip().rstrip("%")) if "/" in out else None
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
            cur = None
        self.saved_volume = cur
        subprocess.run(["pactl", "set-sink-mute", SPEAKER, "0"], timeout=3)
        if cur is None or cur < RING_VOLUME:
            subprocess.run(["pactl", "set-sink-volume", SPEAKER, "%d%%" % RING_VOLUME], timeout=3)

    def _restore_volume(self):
        if self.saved_volume is not None:
            subprocess.run(["pactl", "set-sink-volume", SPEAKER, "%d%%" % self.saved_volume], timeout=3)
            self.saved_volume = None

    @staticmethod
    def _wake_screen():
        # Phosh implements org.gnome.ScreenSaver; SetActive(false) turns the display back on
        subprocess.Popen(["gdbus", "call", "--session", "-d", "org.gnome.ScreenSaver", "-o", "/org/gnome/ScreenSaver",
                          "-m", "org.gnome.ScreenSaver.SetActive", "false"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    loop = GLib.MainLoop()
    d = Alarmd()

    def bye():
        d.stop("exit")          # the RTC stays armed across restarts on purpose
        d._inhibit(False)
        loop.quit()
        return False
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLibUnix.signal_add(GLib.PRIORITY_HIGH, sig, bye)
    log("alarmd up")
    loop.run()


if __name__ == "__main__":
    main()
