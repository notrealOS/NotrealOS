"""Phone actions for voiced: contacts, calls, WhatsApp (via the mautrix bridge), maps, music.

Qwen only fills in {type, target, text}; everything here is plain Python, so a misheard name
can never call or message anyone without the confirm notification being tapped.
"""
import difflib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import uuid

from gi.repository import Gio, GLib

HOME = os.path.expanduser("~")
CONTACTS = os.path.join(HOME, ".cache", "newchat", "contacts.json")      # [[name, [[e164, shown], ...]], ...]
WA_CHOICES = os.path.join(HOME, ".local", "share", "newchat", "choices.json")  # name -> WhatsApp number
ALIASES = os.path.join(HOME, ".config", "voiced", "aliases.json")        # "mum" -> "Jane Doe"
MAPS = os.path.join(HOME, "maps", "notarealmaps")
MUSIC = "org.mpris.MediaPlayer2.notarealPlayer"
DEFAULT_CC = "+46"
CLEAN_ENV = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}


def _read(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _norm(t):
    return " ".join(re.sub(r"[^\w ]", " ", (t or "").lower()).split())


# --- contacts --------------------------------------------------------------------------

def phone_number(text):
    """'08-123 456' / 'plus 46 70...' -> +46... if the target is a number, else None."""
    t = (text or "").lower().replace("plus", "+")
    digits = re.sub(r"[^\d+]", "", t)
    if len(re.sub(r"\D", "", digits)) < 5 or re.search(r"[a-z]{3,}", t.replace("plus", "")):
        return None
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+"):
        digits = DEFAULT_CC + digits.lstrip("0")
    return digits


def find_contact(spoken):
    """Spoken name -> (name, [numbers]) or None. Exact matches of every kind first (whole phrase,
    nickname word, name words), fuzzy matching only last: "my friend Juancho" must not become
    "Frida", while "aron pucket" (whisper spelling) should still find Aaron Puckett."""
    hit = _find_contact(spoken, fuzzy=False)
    if hit:
        return hit
    words = [w for w in _norm(spoken).split() if w not in ("my", "the", "to", "on", "with")]
    aliases = {_norm(k) for k in _read(ALIASES, {})}
    for w in words:
        if w in aliases:
            return _find_contact(w, fuzzy=False)
    people = _read(CONTACTS, [])
    sq = re.sub(r"[^a-z0-9]", "", " ".join(words))   # "juan cho" -> Juancho
    for n, nums in people:
        if re.sub(r"[^a-z0-9]", "", n.lower()) == sq:
            return (n, [x[0] for x in nums])
    named = [w for w in words if any(w in _norm(n).split() for n, _ in people)]
    if named:  # the contact whose name has the most of these words
        best = max(people, key=lambda p: sum(w in _norm(p[0]).split() for w in named))
        return (best[0], [x[0] for x in best[1]])
    return _find_contact(spoken, fuzzy=True)


def _find_contact(spoken, fuzzy=True):
    """Best contact for a spoken name -> (name, [numbers]) or None.

    Order: alias ("dad"), exact full name, unique first/last name, then fuzzy (whisper
    often gets Swedish names slightly wrong)."""
    people = _read(CONTACTS, [])
    if not people:
        return None
    by_name = {n: [x[0] for x in nums] for n, nums in people}
    want = _norm(spoken)
    for w in (want, re.sub(r"^(my|the) ", "", want)):
        alias = {_norm(k): v for k, v in _read(ALIASES, {}).items()}.get(w)
        if alias:
            if alias.startswith("+"):
                return (w.title(), [alias])
            if alias in by_name:
                return (alias, by_name[alias])
    norm_names = {_norm(n): n for n in by_name if _norm(n)}
    if want in norm_names:
        n = norm_names[want]
        return (n, by_name[n])
    parts = [n for k, n in norm_names.items() if want in k.split()]
    if len(parts) == 1:
        return (parts[0], by_name[parts[0]])
    if not fuzzy:
        return (parts[0], by_name[parts[0]]) if parts else None
    # fuzzy on full names and on first names
    close = difflib.get_close_matches(want, list(norm_names), n=1, cutoff=0.72)
    if close:
        n = norm_names[close[0]]
        return (n, by_name[n])
    firsts = {}
    for k, n in norm_names.items():
        firsts.setdefault(k.split()[0], []).append(n)
    close = difflib.get_close_matches(want, list(firsts), n=1, cutoff=0.8)
    if close and len(firsts[close[0]]) == 1:
        n = firsts[close[0]][0]
        return (n, by_name[n])
    if parts:  # several people share that first name: take the first, the confirm shows who
        return (parts[0], by_name[parts[0]])
    return None


def whatsapp_number(name, numbers):
    return _read(WA_CHOICES, {}).get(name) or (numbers[0] if numbers else None)


# --- confirm ---------------------------------------------------------------------------

def confirm(title, body, yes="Yes", timeout_s=25):
    """Blocking (run from a worker thread): a notification with Yes / Cancel buttons."""
    try:
        out = subprocess.run(["notify-send", "-a", "Voice", "-t", str(timeout_s * 1000),
                              "-A", "yes=" + yes, "-A", "no=Cancel", title, body],
                             capture_output=True, text=True, timeout=timeout_s + 5).stdout.strip()
    except subprocess.TimeoutExpired:
        return False
    return out == "yes"


# --- calls -----------------------------------------------------------------------------

def dial(number):
    subprocess.Popen(["gnome-calls", "--dial", number], start_new_session=True, env=CLEAN_ENV,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- WhatsApp (mautrix bridge, same as ~/newchat) ---------------------------------------

def _bridge():
    sys.path.insert(0, os.path.join(HOME, "newchat"))
    import newchat  # noqa: E402  (imports GTK/EDS too, so only when needed)
    return newchat.Bridge()


def whatsapp_room(name, number):
    """Room id of the WhatsApp chat with this number (created + named if needed)."""
    return _bridge().start(number, name)


def whatsapp_send(name, number, text):
    b = _bridge()
    rid = b.start(number, name)
    b.api("PUT", "/rooms/%s/send/m.room.message/%s" % (urllib.parse.quote(rid), uuid.uuid4().hex),
          {"msgtype": "m.text", "body": text})
    return rid


def open_chatty():
    subprocess.Popen(["python3", os.path.join(HOME, "chat", "chat.py")], start_new_session=True, env=CLEAN_ENV,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- maps ------------------------------------------------------------------------------

def maps(flag, query):
    subprocess.Popen([MAPS, flag, query], start_new_session=True, env=CLEAN_ENV,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- web -------------------------------------------------------------------------------

def web_search(query):
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
    subprocess.Popen(["chromium", "--ozone-platform=wayland", "--enable-wayland-ime",   # main browser since 10-16
                      "--wayland-text-input-version=3", "--disable-features=TouchDragAndDrop", url], start_new_session=True, env=CLEAN_ENV,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- music -----------------------------------------------------------------------------

def _music_up():
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        r = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                          "NameHasOwner", GLib.Variant("(s)", (MUSIC,)), None, Gio.DBusCallFlags.NONE, 500, None)
        return r.unpack()[0]
    except GLib.Error:
        return False


def music(method, uri=None):
    """Player method over MPRIS; starts notarealPlayer first if it isn't running."""
    if not _music_up():
        subprocess.Popen(["gtk-launch", "dev.notareal.Player"], start_new_session=True, env=CLEAN_ENV,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.time() + 15
        while time.time() < end and not _music_up():
            time.sleep(0.5)
        time.sleep(1.5)  # let it log in to Navidrome
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    args = GLib.Variant("(s)", (uri,)) if uri else None
    bus.call_sync(MUSIC, "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", method, args, None,
                  Gio.DBusCallFlags.NONE, 3000, None)


def play_query(q):
    music("OpenUri", "spin:play?q=" + urllib.parse.quote(q))


# --- apps ------------------------------------------------------------------------------

# spoken words -> desktop id, for apps whose name you wouldn't say
APP_WORDS = {
    "music": "dev.notareal.Player", "player": "dev.notareal.Player", "spin": "dev.notareal.Player",
    "maps": "dev.notareal.Maps", "map": "dev.notareal.Maps", "navigation": "dev.notareal.Maps",
    "mail": "dev.notareal.VaporMail", "email": "dev.notareal.VaporMail", "e mail": "dev.notareal.VaporMail",
    "camera": "dev.notareal.Camera", "whatsapp": "dev.notareal.Chat", "messages": "dev.notareal.Chat",
    "chats": "dev.notareal.Chat", "youtube": "de.schmidhuberj.tubefeeder", "reddit": "dev.notareal.Reddit",
    "phone": "dev.notareal.Calls", "calls": "dev.notareal.Calls", "dialer": "dev.notareal.Calls",
    "terminal": "org.gnome.Console", "console": "org.gnome.Console", "settings": "org.gnome.Settings",
    "browser": "chromium", "internet": "chromium", "firefox": "firefox-esr", "notes": "org.gnome.World.Iotas",
    "new chat": "dev.notareal.NewChat", "google voice": "dev.notareal.Voice",
}


def _squash(t):
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def find_app(spoken):
    want = _norm(re.sub(r"\b(the|app|application)\b", " ", spoken or ""))
    apps = [a for a in Gio.AppInfo.get_all() if a.should_show()]
    by_id = {a.get_id().removesuffix(".desktop"): a for a in apps}
    if want in APP_WORDS and APP_WORDS[want] in by_id:
        return by_id[APP_WORDS[want]]
    sq = _squash(want)
    names = {}
    for a in apps:
        names.setdefault(_squash(a.get_display_name()), a)
    for a in apps:  # "open chat" -> notreal.chat; keywords too ("open mail", "open music")
        names.setdefault(_squash(a.get_display_name()).removeprefix("notreal"), a)
        for k in (a.get_keywords() if isinstance(a, Gio.DesktopAppInfo) else []):
            names.setdefault(_squash(k), a)
    if sq in names:
        return names[sq]
    starts = [a for k, a in names.items() if k.startswith(sq) or sq.startswith(k)]
    if len(starts) == 1:
        return starts[0]
    close = difflib.get_close_matches(sq, list(names), n=1, cutoff=0.7)
    return names[close[0]] if close else None


def open_app(spoken):
    a = find_app(spoken)
    if not a:
        raise RuntimeError("No app called “%s”" % spoken)
    subprocess.Popen(["gtk-launch", a.get_id().removesuffix(".desktop")], start_new_session=True, env=CLEAN_ENV,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return a.get_display_name()
