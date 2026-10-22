#!/usr/bin/env python3
"""voiced - hold volume up, speak, let go: a local voice assistant for reminders and notes.

Takes over the volume-up key (EVIOCGRAB on gpio-keys) and passes short presses on to
Phosh through a uinput clone, so the volume still works. Holding it for HOLD_MS starts
recording from the internal mic; releasing it runs:
  whisper.cpp (speech -> text)  ->  Qwen3 1.7B via llama-server (text -> JSON)
and the result is written to PhantomShell's reminders.json / notes.json, which
PhantomShell (and the overview widget) already show and fire.
Everything runs on the phone. llama-server is started on first use and stopped
after LLM_IDLE_S without requests, so the model is not kept in RAM.

Feedback: vibration (fbcli), the Glyph bar while listening, and a notification.
"""

import datetime
import fcntl
import json
import os
import re
import signal
import struct
import subprocess
import threading
import time
import urllib.request
import array
import wave

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GLibUnix, Gtk  # noqa: E402

import iotas_notes  # noqa: E402  notes go to the Iotas app (same dir)
import actions  # noqa: E402  calls, WhatsApp, maps, music (same dir)

import sys  # noqa: E402
sys.path.insert(0, os.path.expanduser("~/alarm"))
import alarmd  # noqa: E402  alarms + timers (alarmd watches its alarms.json)


def pc(key, default=""):
    """Address of our PC server (see docs/pc-link.md); set in ~/.config/notreal/pc.json."""
    try:
        with open(os.path.expanduser("~/.config/notreal/pc.json")) as f:
            return json.load(f).get(key) or default
    except (OSError, ValueError):
        return default
sys.path.insert(0, os.path.expanduser("~/rotated"))
try:
    from glitch import Thinking  # noqa: E402  subtle glitches while Qwen thinks
except Exception as e:  # never let the overlay take the assistant down
    print("no thinking overlay:", e, flush=True)
    Thinking = None
try:
    from glitch import Listening  # noqa: E402  "SPEAK NOW" band
except Exception as e:
    print("no listening overlay:", e, flush=True)
    Listening = None
try:
    from glitch import Stop  # noqa: E402  tappable STOP pill (cancel)
except Exception as e:
    print("no stop button:", e, flush=True)
    Stop = None

MUSIC = "org.mpris.MediaPlayer2.notarealPlayer"


def music_call(method, prop=None):
    """notarealPlayer over MPRIS; returns the PlaybackStatus for prop calls, None on any error."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        if prop:
            r = bus.call_sync(MUSIC, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties", "Get",
                              GLib.Variant("(ss)", ("org.mpris.MediaPlayer2.Player", prop)), None,
                              Gio.DBusCallFlags.NONE, 400, None)
            return r.unpack()[0]
        bus.call_sync(MUSIC, "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", method, None, None,
                      Gio.DBusCallFlags.NONE, 1000, None)
    except GLib.Error:
        return None

HOME = os.path.expanduser("~")
VOICE = os.path.join(HOME, "voice")
LLM_MODEL = os.path.join(VOICE, "models", "qwen3-1.7b-q4_k_m.gguf")
STT_MODEL = os.path.join(VOICE, "models", "ggml-base.bin")
BIG_MODEL = ""  # questions use the 1.7B too (4B: 18 s load + 6 tok/s, too slow; 10-15). Old: models/qwen3-4b-q4_k_m.gguf
TTS_VOICE = os.path.join(VOICE, "models", "en_US-amy-medium.onnx")
TTS_SPEED = "0.88"  # piper length scale: <1 = faster, livelier
PC_TTS_VOICE = "af_heart"   # Kokoro on the PC (nrtts via nrbrain /tts): far more natural than piper
PC_TTS_SPEED = 1.05
ANSWER_PROMPT = ("Answer this spoken question for someone on their phone in one to three short "
                 "sentences of plain text, no lists or markdown. If you are not sure, say so.\n"
                 "Question: %s /no_think")
DATA_DIR = os.path.join(HOME, ".local", "share", "phantomshell")
REMINDERS = os.path.join(DATA_DIR, "reminders.json")
NOTES = os.path.join(DATA_DIR, "notes.json")
RUN = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
WAV = os.path.join(RUN, "voiced.wav")
MIC = "alsa_input.platform-sound.HiFi__Mic__source"

HOLD_MS = 450        # held longer than this = talk (shorter = normal volume up)
CUE_MS = 1000        # "speak now" chirp this long after the press; recording already runs
MAX_REC_S = 20
LLM_PORT = 8771
# 10-16: the PC (RTX 5060 Ti, Qwen3.6 35B-A3B via ~/llm/nrbrain.py on the PC) answers first when
# reachable over Tailscale; the phone's own model is the fallback (PC off, away, error).
PC_BRAIN = "http://%s:8097" % pc("host", "localhost")
PC_RETRY_S = 60      # after a failure, don't wait on the PC again for this long
LLM_IDLE_S = 300
THREADS = "6"        # llama: 8 cores; leave some for the UI
STT_THREADS = "4"    # whisper: 4 is faster than 6 or 8 here
# Words whisper (base) otherwise mishears ("pharmacy" -> "partner, senior"): biases the decoder
STT_PROMPT = ("Call dad. Call mom. Message Boris. Open EasyPark. Open Parkster. Open Uber. Find a pharmacy near me. "
              "Take me to Gustavsberg. Play Fontaines D.C. Next song. Remind me. Deyse, Boris, Franco, Juancho, "
              "Stockholm, Södermalm, Värmdö, notarealMaps, WhatsApp, Spotify, jazz.")
STT_LANG = os.environ.get("VOICED_LANG", "en")  # "auto" costs ~8 s extra
GLYPH = "/sys/class/leds/glyph:bar-%d/brightness"

EV_SYN, EV_KEY, KEY_VOLUMEUP = 0, 1, 115
EVENT = struct.Struct("qqHHi")
EVIOCGRAB = 0x40044590
UI_SET_EVBIT, UI_SET_KEYBIT = 0x40045564, 0x40045565
UI_DEV_SETUP, UI_DEV_CREATE = 0x405C5503, 0x5501

# Clear-cut commands skip Qwen (~10 s): (pattern on the lowercased, unpunctuated text, type, target group)
FAST = [
    (r"(pause|stop)( the)?( music| song)?", "pause", None),
    (r"(resume|continue|unpause)( the)?( music| song)?", "resume", None),
    (r"(next|skip)( the| this)?( song| track)?|play the next (song|track)", "next", None),
    (r"(previous|last)( song| track)?|go back( a| one)?( song| track)?", "previous", None),
    (r"(call|phone|ring) (?!me\b)(.+)", "call", 2),
    (r"(navigate|directions|take me|drive me|get me|bring me) (to )?(.+)", "navigate", 3),
    (r"play (.+)", "play", 1),
    (r"(open|launch|start|show me)( the| my)? (.+?)( app)?", "open", 3),
    (r"(search the web for|search online for|search for|search|google|look up)(?!.*( near me| nearby| around here| close by| on the map)$)"
     r" (.+?)( online| on the web| on google| in firefox)?", "web", 3),
    (r"(find|search for|search|where is|where's|look up|show me)( me)?( a| an| the| some)? (.+?)"
     r"( near me| nearby| around here| close by| on the map)?", "find", 4),
]


UNITS = {"second": 1 / 60, "sec": 1 / 60, "minute": 1, "min": 1, "hour": 60, "hr": 60}


def _num(w):
    w = w.strip()
    if w in ("a", "an", "one"):
        return 1
    return NUMBERS.get(w, float(w) if re.fullmatch(r"\d+(\.\d+)?", w) else None)


def parse_span(t):
    """'10 minutes' / '1 hour and 30 minutes' / 'an hour and a half' / 'half an hour' -> minutes."""
    t = t.replace("-", " ")
    if re.fullmatch(r"half an? hour", t):
        return 30
    total, found = 0.0, False
    for m in re.finditer(r"(\w+(?:\.\d+)?) (second|sec|minute|min|hour|hr)s?( and a half)?", t):
        n = _num(m.group(1))
        if n is None:
            return None
        total += (n + (0.5 if m.group(3) else 0)) * UNITS[m.group(2)]
        found = True
    if found and re.search(r"hours? and a half|hour and a half", t) and total % 60 == 0:
        total += 30
    return total if found and total > 0 else None


def parse_clock(t, now):
    """'7' / '7:30' / '6 am' / '7 30 pm' / 'seven tomorrow' -> datetime (bare hours = morning: it's an alarm)."""
    tomorrow = "tomorrow" in t
    t = re.sub(r"\b(tomorrow|morning|in the|o'clock|oclock)\b", " ", t)
    m = re.fullmatch(r"\s*(\w+)(?:[: ](\d{2}))?\s*(am|pm|a m|p m|tonight|evening)?\s*", t)
    if not m:
        return None
    h = _num(m.group(1))
    if h is None or not 0 <= h <= 23:
        return None
    h, mm = int(h), int(m.group(2) or 0)
    ap = (m.group(3) or "").replace(" ", "")
    if ap in ("pm", "tonight", "evening") and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    due = now.replace(hour=h, minute=mm, second=0, microsecond=0)
    if tomorrow:
        due += datetime.timedelta(days=1)
    elif due <= now:
        due += datetime.timedelta(days=1)
    return due


def fast_alarm(t):
    now = datetime.datetime.now()
    m = re.fullmatch(r"(?:set |start )?(?:a |an |the )?timer (?:for )?(.+?)(?: for (?:the )?(.+))?", t) or \
        re.fullmatch(r"(?:set )?(?:a |an )?(.+?) timer(?: for (?:the )?(.+))?", t)
    if m:
        mins = parse_span(m.group(1))
        if mins:
            return {"type": "timer", "at": (now + datetime.timedelta(minutes=mins)).timestamp(),
                    "text": m.group(2) or ""}
        return None
    m = re.fullmatch(r"(?:wake me(?: up)?|set (?:an |the |my )?alarm|alarm)(?: for| at)? (in )?(.+)", t)
    if m:
        rest = m.group(2)
        mins = parse_span(rest)
        if mins and (m.group(1) or re.search(r"minute|hour|second", rest)):
            return {"type": "alarm", "at": (now + datetime.timedelta(minutes=mins)).timestamp(), "text": ""}
        due = parse_clock(rest, now)
        if due:
            return {"type": "alarm", "at": due.timestamp(), "text": ""}
    return None


def fmt_span(mins):
    if mins < 1:
        return "%d s" % round(mins * 60)
    mins = int(round(mins))
    if mins < 60:
        return "%d min" % mins
    return "%d h %02d min" % (mins // 60, mins % 60) if mins % 60 else "%d h" % (mins // 60)


def fast_parse(text):
    t = " ".join(re.sub(r"[^\w\s+']", " ", text.lower().replace(".", "")).split())  # "D.C." -> "dc"
    t = re.sub(r"^(hey |ok |okay |please |can you |could you )+", "", t)
    t = re.sub(r"( please| for me| now)+$", "", t)
    a = fast_alarm(t)
    if a:
        a.update(target="", in_minutes=None, day=None, time=None)
        return a
    for pat, kind, grp in FAST:
        m = re.fullmatch(pat, t)
        if m:
            target = m.group(grp).strip() if grp else ""
            if kind == "navigate" and target in ("my home", "my house", "the house"):
                target = "home"
            if kind == "open" and not actions.find_app(target):
                continue      # "open WhatsApp with Franco" etc.: not an app, try the rest / Qwen
            return {"type": kind, "target": target, "text": "", "in_minutes": None, "day": None, "time": None}
    return None


ACTION_TYPES = ("open", "web", "call", "message", "chat", "find", "navigate", "play", "pause", "resume", "next", "previous")

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"enum": ["reminder", "alarm", "timer", "note", "question", "call", "message", "chat", "find", "web", "navigate",
                          "play", "pause", "resume", "next", "previous", "open", "unknown"]},
        "target": {"type": "string"},
        "text": {"type": "string"},
        "in_minutes": {"type": ["integer", "null"]},
        "day": {"type": ["string", "null"]},
        "time": {"type": ["string", "null"], "pattern": "^[0-2][0-9]:[0-5][0-9]$"},
    },
    "required": ["type", "target", "text", "in_minutes", "day", "time"],
}

PROMPT = """You turn one spoken request into JSON for a phone. Fields:
- type: "reminder" (remind me / don't let me forget), "alarm" (wake me up / set an alarm), "timer" (set a timer / countdown), "note" (note / write down / remember that),
  "call" (call / phone / ring someone or a number), "message" (text / message / WhatsApp someone WITH something to say),
  "chat" (open WhatsApp with someone, nothing to say yet), "find" (find / where is / show a place on the map, "near me"),
  "web" (search for / google / look up something on the internet),
  "navigate" (directions / navigate / take me / how do I get to a place), "play" (play music, an artist, album, song or genre),
  "open" (open / launch / start an app: "open Uber", "launch the camera"),
  "pause" (pause / stop the music), "resume" (resume / continue the music), "next" (next / skip song), "previous" (previous / last song),
  "question" (any other question or request for information: who / what / why / how / explain / tell me), else "unknown"
- target: the person, phone number, place, music, app or search words for call/message/chat/find/navigate/play/open/web, exactly as said ("dad", "Anna", "0701234567", "the nearest pharmacy", "Radiohead"), else ""
- text: what to remind or note, or the message to send, short, in the user's words, without the time part; else ""
- in_minutes: for relative times ("in 20 minutes" = 20, "in two hours" = 120), else null
- day: "today", "tomorrow", a weekday name ("friday") or a date "YYYY-MM-DD", else null
- time: 24-hour "HH:MM" ("6 pm" = "18:00", "half past seven in the morning" = "07:30"), else null
Examples:
"remind me to call mom at 6" -> {"type":"reminder","target":"","text":"call mom","in_minutes":null,"day":null,"time":"18:00"}
"wake me up at half past six tomorrow" -> {"type":"alarm","target":"","text":"","in_minutes":null,"day":"tomorrow","time":"06:30"}
"set a timer for 12 minutes for the pasta" -> {"type":"timer","target":"","text":"pasta","in_minutes":12,"day":null,"time":null}
"call dad" -> {"type":"call","target":"dad","text":"","in_minutes":null,"day":null,"time":null}
"tell Anna on WhatsApp I'm running late" -> {"type":"message","target":"Anna","text":"I'm running late","in_minutes":null,"day":null,"time":null}
"open WhatsApp with Juancho" -> {"type":"chat","target":"Juancho","text":"","in_minutes":null,"day":null,"time":null}
"find coffee near me" -> {"type":"find","target":"coffee","text":"","in_minutes":null,"day":null,"time":null}
"search for cheap flights to Rome" -> {"type":"web","target":"cheap flights to Rome","text":"","in_minutes":null,"day":null,"time":null}
"take me to Gustavsberg" -> {"type":"navigate","target":"Gustavsberg","text":"","in_minutes":null,"day":null,"time":null}
"take me home" -> {"type":"navigate","target":"home","text":"","in_minutes":null,"day":null,"time":null}
"play some jazz" -> {"type":"play","target":"jazz","text":"","in_minutes":null,"day":null,"time":null}
"skip this song" -> {"type":"next","target":"","text":"","in_minutes":null,"day":null,"time":null}
"remind me in 20 minutes to take the pizza out" -> {"type":"reminder","target":"","text":"take the pizza out","in_minutes":20,"day":null,"time":null}
"tomorrow at 9 remind me about the dentist" -> {"type":"reminder","target":"","text":"dentist","in_minutes":null,"day":"tomorrow","time":"09:00"}
"note the wifi password is banana42" -> {"type":"note","target":"","text":"wifi password is banana42","in_minutes":null,"day":null,"time":null}
"who built the eiffel tower" -> {"type":"question","target":"","text":"who built the eiffel tower","in_minutes":null,"day":null,"time":null}
Now it is %s.
Request: "%s" /no_think"""


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_json(path, data):  # same format/atomic write as PhantomShell
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def notify(title, body="", ms=4000):
    subprocess.Popen(["notify-send", "-a", "Voice", "-t", str(ms), title, body],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def buzz(event="button-pressed"):
    subprocess.Popen(["fbcli", "-E", event], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_tone(path, freqs, ms=70, vol=0.35, rate=48000):
    """A short chirp: one segment per frequency, with fades so it doesn't click."""
    import math
    n = int(rate * ms / 1000)
    a = array.array("h")
    for f in freqs:
        for i in range(n):
            env = min(1.0, i / (n * 0.15), (n - i) / (n * 0.15))
            a.append(int(32767 * vol * env * math.sin(2 * math.pi * f * i / rate)))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(a.tobytes())


START_SND = os.path.join(RUN, "voiced-start.wav")
STOP_SND = os.path.join(RUN, "voiced-stop.wav")
make_tone(START_SND, [880, 1320])
make_tone(STOP_SND, [1320, 880])


def chirp(path):
    return subprocess.Popen(["paplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def glyph(on):
    for i in range(1, 9):
        try:
            with open(GLYPH % i, "w") as f:
                f.write("255" if on else "0")
        except OSError:
            return


NUMBERS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}


def clock_phrases(text):
    """'half past ten' -> '10:30' etc.; the small model gets these wrong."""
    def hour(w):
        return NUMBERS.get(w.lower(), int(w) if w.isdigit() else None)

    def sub(pattern, fn):
        nonlocal text
        def repl(m):
            h = hour(m.group(1))
            return fn(h) if h is not None else m.group(0)
        text = re.sub(pattern, repl, text, flags=re.I)

    sub(r"\bhalf past (\w+)", lambda h: "%d:30" % h)
    sub(r"\bquarter past (\w+)", lambda h: "%d:15" % h)
    sub(r"\bquarter to (\w+)", lambda h: "%d:45" % ((h - 1) % 12 or 12))
    return text


def resolve_due(r, now):
    """Turn the model's fields into a timestamp (None if there is no usable time)."""
    if isinstance(r.get("in_minutes"), int) and r["in_minutes"] > 0:
        return now + datetime.timedelta(minutes=r["in_minutes"])
    t = r.get("time")
    day = (r.get("day") or "").strip().lower()
    hh, mm = (int(x) for x in t.split(":")) if t else (9, 0)
    if not t and not day:
        return None
    if day in ("", "today"):
        d = now.date()
    elif day == "tomorrow":
        d = now.date() + datetime.timedelta(days=1)
    elif day in WEEKDAYS:
        ahead = (WEEKDAYS.index(day) - now.weekday()) % 7
        d = now.date() + datetime.timedelta(days=ahead)
    else:
        try:
            d = datetime.date.fromisoformat(day)
        except ValueError:
            d = now.date()
    due = datetime.datetime.combine(d, datetime.time(hh % 24, mm))
    if due <= now and day in ("", "today") and t:
        due += datetime.timedelta(days=1)  # "at 6" when it's already past 6: tomorrow
    elif due <= now and day in WEEKDAYS:
        due += datetime.timedelta(days=7)
    return due


def kill_stale_servers(port):
    """A crash (SIGKILL) can't run cleanup: on start, end any llama-server left on our port."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                cmd = f.read().split(b"\0")
        except OSError:
            continue
        if cmd and cmd[0].endswith(b"llama-server") and str(port).encode() in cmd:
            log("killing stale llama-server", pid)
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass


class Llm:
    """One llama-server on demand; stopped again after `idle` seconds."""

    def __init__(self, name, model, port, idle=LLM_IDLE_S):
        self.name, self.model, self.port, self.idle = name, model, port, idle
        self.proc = None
        self.last = 0
        self.lock = threading.Lock()
        kill_stale_servers(port)
        GLib.timeout_add_seconds(30, self._reap)

    def _reap(self):
        if self.lock.locked():
            return True  # loading or answering: never idle
        if self.proc and time.time() - self.last > self.idle:
            log("stopping %s (idle)" % self.name)
            self.stop()
        return True

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def _ensure(self):
        if self.proc and self.proc.poll() is None:
            return
        log("starting", self.name)
        self.proc = subprocess.Popen(
            ["llama-server", "-m", self.model, "--port", str(self.port), "--host", "127.0.0.1",
             "-c", "2048", "-t", THREADS, "--jinja",
             "--device", "none",  # the Vulkan (Adreno) backend computes garbage
             "--no-repack"],  # keep weights in the mmapped file: reclaimable, not swapped (same speed)
            stdout=open(os.path.join(RUN, "voiced-%s.log" % self.name), "w"),
            stderr=subprocess.STDOUT)
        for _ in range(240):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/health" % self.port, timeout=1)
                return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError("%s did not come up" % self.name)

    def chat(self, content, max_tokens, schema=None, temperature=0.1):
        remote = pc_chat(content, max_tokens, schema, temperature)
        if remote is not None:
            return remote
        with self.lock:
            self.last = time.time()
            self._ensure()
            self.last = time.time()
            body = {
                "messages": [{"role": "user", "content": content}],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            if schema:
                body["response_format"] = {"type": "json_schema", "json_schema": {"schema": schema}}
            req = urllib.request.Request(
                "http://127.0.0.1:%d/v1/chat/completions" % self.port,
                data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                out = json.load(resp)["choices"][0]["message"]["content"]
            self.last = time.time()
            log("%s:" % self.name, repr(out))
            return out

    def parse(self, text):
        now = datetime.datetime.now().strftime("%A %Y-%m-%d %H:%M")
        return json.loads(self.chat(PROMPT % (now, text), 400, SCHEMA))


_pc = {"down_until": 0.0}


def pc_chat(content, max_tokens, schema, temperature):
    """Ask the PC's model; None if it can't answer (then the phone's model does)."""
    if time.time() < _pc["down_until"]:
        return None
    try:
        urllib.request.urlopen(PC_BRAIN + "/health", timeout=1.5).read()
        body = {"messages": [{"role": "user", "content": content}], "temperature": temperature,
                "max_tokens": max_tokens, "chat_template_kwargs": {"enable_thinking": False}}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"schema": schema}}
        req = urllib.request.Request(PC_BRAIN + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=90) as resp:      # first use loads the model (~30 s)
            out = json.load(resp)["choices"][0]["message"]["content"]
        log("pc-brain (%.1fs): %r" % (time.time() - t0, out))
        return out
    except Exception as e:
        log("pc-brain unavailable (%s), using the phone's model" % e)
        _pc["down_until"] = time.time() + PC_RETRY_S
        return None


def pc_transcribe(wav, lang, prompt):
    """Whisper 'small' on the PC (nrwhisper via nrbrain, ~1 s); None -> the phone's whisper."""
    if time.time() < _pc["down_until"]:
        return None
    try:
        boundary = "nrvoice%d" % int(time.time() * 1000)
        parts = []
        for k, v in (("language", lang), ("prompt", prompt), ("response_format", "json"), ("temperature", "0")):
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                          % (boundary, k, v)).encode())
        with open(wav, "rb") as f:
            audio = f.read()
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"v.wav\"\r\n"
                      "Content-Type: audio/wav\r\n\r\n" % boundary).encode() + audio + b"\r\n")
        parts.append(("--%s--\r\n" % boundary).encode())
        req = urllib.request.Request(PC_BRAIN + "/inference", data=b"".join(parts),
                                     headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = json.load(resp).get("text", "")
        log("pc-whisper (%.1fs): %r" % (time.time() - t0, text))
        return text
    except Exception as e:
        log("pc-whisper unavailable (%s), using the phone's whisper" % e)
        _pc["down_until"] = time.time() + PC_RETRY_S
        return None


def strip_markdown(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"[*_#`>]+", "", text)
    return " ".join(text.split())


def pc_speak(text, out):
    """Kokoro on the PC (~1 s per sentence) into out; False -> the phone's piper voice."""
    if time.time() < _pc.get("tts_down_until", 0):   # own back-off: an LLM error must not mute the voice
        return False
    try:
        body = json.dumps({"text": text, "voice": PC_TTS_VOICE, "speed": PC_TTS_SPEED}).encode()
        req = urllib.request.Request(PC_BRAIN + "/tts", data=body, headers={"Content-Type": "application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=20) as resp:
            audio = resp.read()
        with open(out, "wb") as f:
            f.write(audio)
        log("pc-tts (%.1fs): %d bytes" % (time.time() - t0, len(audio)))
        return True
    except Exception as e:
        log("pc-tts unavailable (%s), using piper" % e)
        _pc["tts_down_until"] = time.time() + PC_RETRY_S
        return False


_live = set()   # whisper / piper / paplay now running: STOP kills them


def run_tracked(cmd, timeout, input=None):
    """subprocess.run() that STOP can kill; returns stdout ("" on timeout or kill)."""
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    _live.add(p)
    try:
        return p.communicate(input, timeout=timeout)[0] or ""
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        return ""
    finally:
        _live.discard(p)


def kill_live():
    for p in list(_live):
        try:
            p.kill()
        except OSError:
            pass


def speak(text, alive=lambda: True):
    """Read text aloud: Kokoro on the PC, else piper (blocking until done or STOP)."""
    out = os.path.join(RUN, "voiced-say.wav")
    if pc_speak(text, out):
        if alive():
            run_tracked(["paplay", out], 120)
        return
    if not os.path.exists(TTS_VOICE):
        return
    run_tracked(["piper", "-m", TTS_VOICE, "--length-scale", TTS_SPEED, "-f", out], 60, input=text)
    if alive():
        run_tracked(["paplay", out], 120)


class Voiced:
    def __init__(self, thinking=None, listening=None, app=None):
        self.thinking = thinking
        self.listening = listening
        self.stop_btn = Stop(app, self.cancel) if Stop and app else None
        self.gen = 0   # bumped by STOP: a worker whose generation is stale throws its result away
        self.music_paused = False
        self.llm = Llm("qwen-1.7b", LLM_MODEL, LLM_PORT)
        # questions use the bigger model when it is there; only one model runs at a time (RAM)
        self.big = (Llm("qwen-4b", BIG_MODEL, LLM_PORT + 1, idle=300)
                    if os.path.exists(BIG_MODEL) else self.llm)
        self.ui_fd = self.key_fd = None
        self.hold_timer = 0
        self.rec = None
        self.rec_timer = 0
        self.cue_timer = 0
        self.busy = False
        self.setup_keys()

    # --- key handling -------------------------------------------------------

    def setup_keys(self):
        fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(fd, UI_SET_KEYBIT, KEY_VOLUMEUP)
        fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack("HHHH80sI", 0x19, 0x1, 0x1, 1, b"voiced volume up", 0))
        fcntl.ioctl(fd, UI_DEV_CREATE)
        self.ui_fd = fd
        for d in sorted(os.listdir("/sys/class/input")):
            try:
                with open("/sys/class/input/%s/device/name" % d) as f:
                    name = f.read().strip()
            except OSError:
                continue
            if d.startswith("event") and name == "gpio-keys":
                self.key_fd = os.open("/dev/input/" + d, os.O_RDONLY | os.O_NONBLOCK)
                time.sleep(0.3)  # let libinput pick up the uinput clone first
                fcntl.ioctl(self.key_fd, EVIOCGRAB, 1)
                GLibUnix.fd_add_full(GLib.PRIORITY_HIGH, self.key_fd, GLib.IOCondition.IN, self.on_key_fd)
                log("volume up grabbed:", d)
                return
        raise RuntimeError("gpio-keys not found")

    def emit(self, value):
        os.write(self.ui_fd, EVENT.pack(0, 0, EV_KEY, KEY_VOLUMEUP, value) + EVENT.pack(0, 0, EV_SYN, 0, 0))

    def on_key_fd(self, fd, cond):
        try:
            data = os.read(fd, EVENT.size * 32)
        except BlockingIOError:
            return GLib.SOURCE_CONTINUE
        for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
            _, _, typ, code, val = EVENT.unpack_from(data, i)
            if typ == EV_KEY and code == KEY_VOLUMEUP:
                if val == 1:
                    self.press()
                elif val == 0:
                    self.release()
        return GLib.SOURCE_CONTINUE

    def press(self):
        if self.hold_timer:
            GLib.source_remove(self.hold_timer)
        self.hold_timer = GLib.timeout_add(HOLD_MS, self.long_press)
        if not self.busy and not self.rec:
            self.start_recording()  # from the press, so nothing said early is lost

    def release(self):
        if self.hold_timer:  # short press: normal volume up, drop the recording
            GLib.source_remove(self.hold_timer)
            self.hold_timer = 0
            self.cancel_recording()
            self.emit(1)
            self.emit(0)
        elif self.rec:
            self.stop_recording()

    def long_press(self):
        self.hold_timer = 0
        if self.busy:
            self.cancel_recording()
            notify("Still thinking about the last one…", ms=1500)
            return False
        log("listening")
        glyph(True)
        if self.listening:
            self.listening.ready()
        if self.stop_btn:
            self.stop_btn.show()
        # music would drown the cue / answer, and in HI-FI mode it owns the DAC: Stop frees it
        if music_call(None, "PlaybackStatus") == "Playing":
            music_call("Stop")
            self.music_paused = True
        self.cue_timer = GLib.timeout_add(CUE_MS - HOLD_MS, self.cue)
        return False

    def cue(self):
        self.cue_timer = 0
        if self.rec:
            buzz()
            chirp(START_SND)
            if self.listening:
                self.listening.listen()
        return False

    # --- recording ----------------------------------------------------------

    def start_recording(self):
        self.rec = subprocess.Popen(
            ["parecord", "--device=" + MIC, "--rate=16000", "--channels=1",
             "--format=s16le", "--file-format=wav", WAV])
        self.rec_timer = GLib.timeout_add_seconds(MAX_REC_S, self.stop_recording)

    def cancel_recording(self):
        for t in ("rec_timer", "cue_timer"):
            if getattr(self, t, 0):
                GLib.source_remove(getattr(self, t))
                setattr(self, t, 0)
        rec, self.rec = self.rec, None
        if self.listening:
            self.listening.hide()
        if self.stop_btn and not self.busy:
            self.stop_btn.hide()
        if rec:
            rec.kill()
            rec.wait()
        self.resume_music()

    def cancel(self):
        """STOP tapped: drop whatever is going on (recording, thinking, speaking)."""
        log("cancelled")
        self.gen += 1
        if self.hold_timer:
            GLib.source_remove(self.hold_timer)
            self.hold_timer = 0
        glyph(False)
        self.busy = False
        self.cancel_recording()     # also resumes the music
        kill_live()
        self.think(False)
        if self.stop_btn:
            self.stop_btn.hide()
        buzz()
        notify("Cancelled", "", 1200)

    def stop_recording(self):
        if getattr(self, "cue_timer", 0):
            GLib.source_remove(self.cue_timer)
            self.cue_timer = 0
        if self.rec_timer:
            GLib.source_remove(self.rec_timer)
            self.rec_timer = 0
        rec, self.rec = self.rec, None
        glyph(False)
        if self.listening:
            self.listening.hide()
        if not rec:
            return False
        rec.send_signal(signal.SIGINT)
        try:
            rec.wait(2)
        except subprocess.TimeoutExpired:
            rec.kill()
        buzz()
        chirp(STOP_SND)
        self.think(True)
        self.busy = True
        threading.Thread(target=self.process, daemon=True).start()
        return False

    # --- speech -> text -> action -------------------------------------------

    def normalize(self):
        """Scale the recording to a peak of ~-3 dBFS (the mic is quiet); max gain 30x."""
        with wave.open(WAV) as w:
            params, a = w.getparams(), array.array("h", w.readframes(w.getnframes()))
        peak = max((abs(x) for x in a), default=0)
        if peak < 50:
            return False  # silence
        g = min(23000 / peak, 30.0)
        a = array.array("h", (max(-32768, min(32767, int(x * g))) for x in a))
        with wave.open(WAV, "wb") as w:
            w.setparams(params)
            w.writeframes(a.tobytes())
        log("normalized: peak %d, gain %.1fx" % (peak, g))
        return True

    def transcribe(self):
        if not self.normalize():
            return ""
        remote = pc_transcribe(WAV, STT_LANG, STT_PROMPT)
        if remote is not None:
            return re.sub(r"\[[^\]]*\]|\([^)]*\)", "", " ".join(remote.split())).strip()
        out = run_tracked(
            ["whisper-cli", "-m", STT_MODEL, "-f", WAV, "-l", STT_LANG, "-nt", "-np", "--prompt", STT_PROMPT,
             "-t", STT_THREADS, "-ng"],  # -ng: the Vulkan (Adreno) backend is broken
            120)
        text = " ".join(out.split()).strip()
        return re.sub(r"\[[^\]]*\]|\([^)]*\)", "", text).strip()  # drop [BLANK_AUDIO] etc.

    def process(self):
        g = self.gen
        alive = lambda: g == self.gen  # noqa: E731  False once STOP was tapped
        try:
            t0 = time.time()
            text = self.transcribe()
            if not alive():
                return
            log("heard: %r (%.1fs)" % (text, time.time() - t0))
            if len(text) < 3:
                GLib.idle_add(notify, "Didn't catch that", "Hold volume up while you speak.")
                return
            GLib.idle_add(notify, "Heard", "“%s”" % text, 3000)
            # whichever model is already loaded parses (both don't fit in RAM, and a
            # reload costs more than the bigger model's slower parse)
            r = fast_parse(text)
            if r:
                log("fast path:", r["type"], repr(r["target"]))
            else:
                parser = self.big if self.big.running() else self.llm
                r = parser.parse(clock_phrases(text))
            if not alive():
                log("dropped (cancelled):", r.get("type"))
                return
            if r.get("type") == "question":
                self.answer(text, alive)
                log("done in %.1fs" % (time.time() - t0))
                return
            log("done in %.1fs" % (time.time() - t0))
            GLib.idle_add(lambda: alive() and self.act(r, text))
        except Exception as e:
            log("error:", repr(e))
            if alive():
                GLib.idle_add(notify, "Voice assistant error", str(e)[:200])
        finally:
            if alive():          # after STOP, cancel() already cleaned up (and a new command may run)
                GLib.idle_add(self.think, False)
                self.busy = False
                GLib.idle_add(self.resume_music)
                GLib.idle_add(self.hide_stop)

    def hide_stop(self):
        if self.stop_btn:
            self.stop_btn.hide()
        return False

    def resume_music(self):
        if self.music_paused:
            self.music_paused = False
            music_call("Play")
        return False

    def think(self, on):
        if self.thinking:
            self.thinking.start() if on else self.thinking.stop()
        return False

    def answer(self, question, alive=lambda: True):
        if self.big is not self.llm:
            self.llm.stop()  # free RAM for the big model
            GLib.idle_add(notify, "Thinking…", question, 8000)
        text = strip_markdown(self.big.chat(ANSWER_PROMPT % question, 160, temperature=0.3))
        if not alive():
            return
        GLib.idle_add(self.think, False)
        GLib.idle_add(notify, question[:60], text, 20000)
        buzz("message-new-instant")
        speak(text, alive)

    def act(self, r, heard):
        kind, text = r.get("type"), (r.get("text") or heard).strip()
        now = datetime.datetime.now()
        if kind in ("alarm", "timer"):
            if r.get("at"):                     # fast path computed the time already
                due = datetime.datetime.fromtimestamp(r["at"])
            else:
                due = resolve_due(r, now)
            if not due or due <= now:
                notify("When?", "“%s” — try “wake me up at 7” or “timer for 10 minutes”" % heard)
                return False
            mins = round((due - now).total_seconds() / 60)
            if kind == "timer":
                label = "Timer · %s" % fmt_span(mins) + (" · " + text if r.get("text") else "")
            else:
                label = r.get("text") or "Wake up"
            alarmd.add_alarm(due.timestamp(), label)
            when = due.strftime("%H:%M") if due.date() == now.date() else due.strftime("%a %H:%M")
            notify(("⏲ Timer set · rings " if kind == "timer" else "⏰ Alarm set · ") + when, "in " + fmt_span(mins))
            buzz("message-new-instant")
        elif kind == "reminder":
            due = resolve_due(r, now)
            if not due:
                notify("Reminder needs a time", "“%s”" % heard)
                return False
            items = load_json(REMINDERS)
            items.append({"due": due.timestamp(), "text": text})
            save_json(REMINDERS, items)
            when = due.strftime("%H:%M") if due.date() == now.date() else due.strftime("%a %d %b %H:%M")
            notify("Reminder set · " + when, text)
            buzz("message-new-instant")
        elif kind == "note":
            if iotas_notes.available():
                iotas_notes.add(text)
                notify("Note saved to Iotas", text)
            else:
                items = load_json(NOTES)
                items.append({"time": time.time(), "text": text})
                save_json(NOTES, items)
                notify("Note saved", text)
            buzz("message-new-instant")
        elif kind in ACTION_TYPES:
            target = (r.get("target") or "").strip()
            if kind in ("pause", "next", "previous", "play"):
                self.music_paused = False      # don't auto-resume what was playing before
            threading.Thread(target=self.do_action, args=(kind, target, (r.get("text") or "").strip(), heard),
                             daemon=True).start()
        else:
            notify("Not sure what to do", "“%s” — try “remind me…”, “call…”, “take me to…”, “play…”" % heard)
        return False

    # calls / WhatsApp / maps / music (worker thread: confirms block, bridge calls take seconds)
    def do_action(self, kind, target, text, heard):
        try:
            if kind == "open":
                name = actions.open_app(target)
                GLib.idle_add(notify, "Opening " + name, "", 1500)
            elif kind == "web":
                if not target:
                    raise RuntimeError("Search for what? Didn't catch it.")
                actions.web_search(target)
                GLib.idle_add(notify, "Searching the web", target, 2500)
            elif kind in ("call", "message", "chat"):
                self.person_action(kind, target, text)
            elif kind in ("find", "navigate"):
                if not target:
                    raise RuntimeError("Where to? Didn't catch a place.")
                actions.maps("--search" if kind == "find" else "--navigate", target)
                GLib.idle_add(notify, ("Searching " if kind == "find" else "Directions to ") + target, "", 2500)
            elif kind == "play":
                if target:
                    actions.play_query(target)
                    GLib.idle_add(notify, "Playing " + target, "", 2500)
                else:
                    actions.music("Play")
            else:
                actions.music({"pause": "Pause", "resume": "Play", "next": "Next", "previous": "Previous"}[kind])
        except Exception as e:  # noqa: BLE001
            log("action error:", repr(e))
            GLib.idle_add(notify, "Couldn't do that", str(e)[:200])

    def person_action(self, kind, target, text):
        number = actions.phone_number(target)
        if number:
            name, numbers = number, [number]
        else:
            hit = actions.find_contact(target)
            if not hit:
                raise RuntimeError("No contact matching “%s”" % target)
            name, numbers = hit
        if kind == "call":
            num = numbers[0]
            if actions.confirm("Call %s?" % name, num, yes="Call"):
                actions.dial(num)
        elif kind == "message":
            if not text:
                raise RuntimeError("What should the message say?")
            num = actions.whatsapp_number(name, numbers)
            if actions.confirm("WhatsApp %s?" % name, "“%s”\n%s" % (text, num), yes="Send"):
                actions.whatsapp_send(name, num, text)
                GLib.idle_add(notify, "Sent to " + name, text, 3000)
                buzz("message-sent-instant")
        else:  # chat
            num = actions.whatsapp_number(name, numbers)
            GLib.idle_add(notify, "Opening WhatsApp chat", name, 3000)
            actions.whatsapp_room(name, num)
            actions.open_chatty()


def main():
    app = Gtk.Application(application_id="org.notarealinux.Voiced",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(a):
        a.hold()
        v = a.voiced = Voiced(Thinking(a) if Thinking else None, Listening(a) if Listening else None, a)

        def test():  # kill -USR1 <pid>: 5 s of thinking glitches
            v.think(True)
            GLib.timeout_add_seconds(5, v.think, False)
            return True
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, test)

        def bye():   # SIGTERM/SIGINT (launcher restart, logout): never leave a llama-server behind
            for llm in {id(x): x for x in (v.llm, v.big)}.values():
                llm.stop()
            a.quit()
            return False
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLibUnix.signal_add(GLib.PRIORITY_HIGH, sig, bye)

    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    main()
