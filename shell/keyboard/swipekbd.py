#!/usr/bin/env python3
"""swipekbd: notareal swipe keyboard for Phosh (GTK4 layer-shell + swipe-im helper).

Replaces Stevia as sm.puri.OSK0. Left-handed: backspace sits left of Z, the best
suggestion is the leftmost slot. Resize mode (⤢ in the suggestion bar) moves/scales
the keyboard; settings live in ~/.config/swipekbd/settings.json per orientation.
"""
import json, math, os, sys, time
from ctypes import CDLL
CDLL("libgtk4-layer-shell.so.0")  # must load before libwayland-client

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gtk, Gdk, Gio, GLib, Gsk, Graphene, Pango
from gi.repository import Gtk4LayerShell as LS

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from decoder import Decoder
from dictate import Dictation

try:
    gi.require_version("Lfb", "0.0")
    from gi.repository import Lfb
    Lfb.init("sm.puri.OSK0")
except Exception:
    Lfb = None
KEY_HAPTICS = False

CONF_DIR = os.path.expanduser("~/.config/swipekbd")
CONF = os.path.join(CONF_DIR, "settings.json")
LANGS = ["en", "es", "fr", "sv"]
# colors + font come from the notareal theme (~/theme, `nrtheme <name>` switches live);
# the names are vapor's: LAV = accent, PINK = pink, CYAN = cyan, MINT = mint
try:
    import notareal_theme as theme
except ImportError:
    theme = None
FONT = "CozetteVector"
BG = (28 / 255, 35 / 255, 64 / 255, 0.92)
FG = (0.914, 0.902, 0.984)
LAV = (0.773, 0.627, 1.0)
PINK = (0.969, 0.631, 0.910)
CYAN = (0.435, 0.839, 0.949)
MINT = (0.494, 0.941, 0.824)
ADD_MARK = "＋"   # suggestion prefix: tap to add the typed word to the dictionary
SURFACE = (0.12, 0.15, 0.30)
SHADE = (0.07, 0.08, 0.16)
CORNERS = 1.0      # 0 = square keys (micro)


def load_theme():
    global FONT, BG, FG, LAV, PINK, CYAN, MINT, SURFACE, SHADE, CORNERS
    if not theme:
        return
    FONT = theme.value("font", '"CozetteVector"').split(",")[0].strip().strip('"') or FONT
    BG = theme.rgb("bg") + (0.92,)
    FG, LAV, PINK = theme.rgb("fg"), theme.rgb("accent"), theme.rgb("pink")
    CYAN, MINT, SURFACE, SHADE = theme.rgb("cyan"), theme.rgb("mint"), theme.rgb("surface"), theme.rgb("bg-deep")
    try:
        CORNERS = float(theme.value("corners", "1"))
    except ValueError:
        CORNERS = 1.0


load_theme()

# evdev key codes
KEY_ESC, KEY_BACKSPACE, KEY_TAB, KEY_ENTER = 1, 14, 15, 28
KEY_LEFT, KEY_RIGHT, KEY_UP, KEY_DOWN = 105, 106, 103, 108

MOD_SHIFT, MOD_CTRL, MOD_ALT = 1, 4, 8  # xkb modifier masks the helper understands

# us-layout evdev codes, so latched Ctrl/Alt can be combined with typed characters
EVDEV = {**{c: 16 + i for i, c in enumerate("qwertyuiop")}, **{c: 30 + i for i, c in enumerate("asdfghjkl")},
         **{c: 44 + i for i, c in enumerate("zxcvbnm")}, **{c: 2 + i for i, c in enumerate("1234567890")},
         "-": 12, "=": 13, "[": 26, "]": 27, ";": 39, "'": 40, "`": 41, "\\": 43, ",": 51, ".": 52, "/": 53,
         " ": 57}
SHIFTED = dict(zip('!@#$%^&*()_+{}:"~|<>?', '1234567890-=[];\'`\\,./'))
KEYSYMS = {"Tab": 15, "Escape": 1, "Left": 105, "Up": 103, "Down": 108, "Right": 106, "Home": 102,
           "End": 107, "Page_Up": 104, "Page_Down": 109, "Insert": 110, "Delete": 111, "BackSpace": 14,
           "Return": 28, "Menu": 127, "space": 57, **{f"F{i}": 58 + i for i in range(1, 11)}, "F11": 87, "F12": 88}
SYM_LABEL = {"Tab": "tab", "Escape": "esc", "Left": "◀", "Up": "▲", "Down": "▼", "Right": "▶",
             "Home": "home", "End": "end", "Page_Up": "pgup", "Page_Down": "pgdn", "Menu": "menu",
             "Delete": "del", "Insert": "ins", "BackSpace": "⌫", "Return": "⏎"}
TERM_DEFAULT = ["Tab", "Escape", "<ctrl>", "<alt>", "Left", "Up", "Down", "Right", "<ctrl>r", "Home", "End",
                "<ctrl>w", "<alt>b", "<alt>f", "<ctrl>v", "<ctrl>c", "<ctrl><shift>v", "<ctrl><shift>c", "Menu"]


def char_key(ch):
    """(evdev code, extra mods) for a character on the us layout, or None."""
    if ch in EVDEV:
        return EVDEV[ch], 0
    if ch.isalpha() and ch.lower() in EVDEV:
        return EVDEV[ch.lower()], MOD_SHIFT
    if ch in SHIFTED:
        return EVDEV[SHIFTED[ch]], MOD_SHIFT
    return None


def parse_shortcut(spec):
    """'<ctrl><shift>v' -> (label, action); '<ctrl>' / '<alt>' -> latch toggles."""
    mods, rest = 0, spec
    while rest.startswith("<"):
        name, rest = rest[1:].split(">", 1)
        mods |= {"ctrl": MOD_CTRL, "control": MOD_CTRL, "primary": MOD_CTRL, "alt": MOD_ALT,
                 "shift": MOD_SHIFT}.get(name.lower(), 0)
    if not rest:
        return ("ctrl", "latch-ctrl") if mods == MOD_CTRL else ("alt", "latch-alt")
    if rest in KEYSYMS:
        code, label = KEYSYMS[rest], SYM_LABEL.get(rest, rest)
    elif len(rest) == 1 and char_key(rest):
        code, label = EVDEV.get(rest.lower(), char_key(rest)[0]), rest.upper()
    else:
        return None
    pre = ("^" if mods & MOD_CTRL else "") + ("M-" if mods & MOD_ALT else "") + ("⇧" if mods & MOD_SHIFT else "")
    return pre + label, f"m{mods}:{code}"


def term_shortcuts():
    try:
        specs = list(Gio.Settings.new("mobi.phosh.osk.Terminal").get_strv("shortcuts"))
    except Exception:
        specs = []
    return [x for x in map(parse_shortcut, specs or TERM_DEFAULT) if x]


ACCENTS = {
    "a": "àáâäåæã", "c": "ç", "e": "éèêë€", "i": "íìîï", "n": "ñ", "o": "óòôöøõœ",
    "u": "úùûü", "s": "ß$", "y": "ÿ¥", "l": "£", ".": "…!?,;:'\"-", ",": "¿¡'\"",
}
TOPNUM = dict(zip("qwertyuiop", "1234567890"))


def K(label, x, w=1.0, act=None, kind="char"):
    return {"label": label, "x": x, "w": w, "act": act if act is not None else label, "kind": kind}


def bottom_row(left):
    # 😊: tap = emoji, hold = menu with emoji + the languages
    return [K(left[0], 0, 1.5, left[1], "sys"), K("🎤", 1.5, 1, "mic", "sys"), K("😊", 2.5, 1, "emoji", "sys"),
            K("", 3.5, 3, " ", "space"), K(",", 6.5), K(".", 7.5), K("⏎", 8.5, 1.5, "enter", "enter")]


# emoji pages: (tab icon, emoji); page 0 is filled from the recents in settings.json
EMOJI_PAGES = [
    ("🕘", ""),
    ("😀", "😀 😂 🤣 😊 😍 🥰 😘 😉 😎 🤩 🥳 😏 😅 😆 😇 🙂 🙃 😋 😜 🤪 🤗 🤔 🤭 🤫 😴 🥺 😢 😭"),
    ("😡", "😤 😡 🤬 😱 😨 😰 😳 🤯 😬 🙄 😒 😔 😞 😩 🤤 🤢 🤮 🤧 😷 🤒 🥵 🥶 😵 🫠 💀 👻 🤡 💩"),
    ("👍", "👍 👎 👌 ✌️ 🤞 🤟 🤘 👏 🙌 🙏 💪 👋 🤙 👉 👈 👆 👇 ✋ 🫶 🤝 ✊ 👊 🫡 🤷 🤦 🙋 👀 🧠"),
    ("❤️", "❤️ 🧡 💛 💚 💙 💜 🖤 🤍 💔 💕 💖 💯 🔥 ✨ ⭐ 🌟 💥 💫 🎉 🎊 🎁 🎶 ✅ ❌ ⚡ 💤 ❓ ❗"),
    ("🐶", "🐶 🐱 🐭 🐰 🦊 🐻 🐼 🐨 🐯 🦁 🐮 🐷 🐸 🐵 🐔 🦄 🐝 🦋 🐢 🐍 🐙 🐬 🐳 🌸 🌹 🌻 🌴 🌈"),
    ("🍕", "🍕 🍔 🍟 🌮 🍣 🍜 🍝 🍰 🎂 🍩 🍪 🍫 🍿 🍎 🍌 🍓 🍉 🥑 ☕ 🍺 🍷 🥂 🍾 ⚽ 🎮 🚗 ✈️ 🏠"),
]
EMOJI_PER_PAGE = 28
KAO_TAB = len(EMOJI_PAGES)  # the last tab: kaomoji, tapping it again flips to the next set
KAOMOJI = [  # 11 per set: 4 + 4 + 3 wide keys
    ["(◕‿◕)", "(＾▽＾)", "(≧◡≦)", "ヽ(•‿•)ノ", "(ﾉ◕ヮ◕)ﾉ*:･ﾟ✧", "(✿◠‿◠)", "٩(◕‿◕)۶", "(｡◕‿◕｡)",
     "＼(^o^)／", "(⌒‿⌒)", "(￣▽￣)"],
    ["(♥ω♥)", "(´∀｀)♡", "(づ｡◕‿‿◕｡)づ", "(っ˘з(˘⌣˘ )", "(*˘︶˘*).｡*♡", "♡(｡- ω -)", "(｡♥‿♥｡)",
     "( ˘ ³˘)♥", "(ɔˆ ³(ˆ⌣ˆc)", "♡＾▽＾♡", "(◍•ᴗ•◍)❤"],
    ["(╥﹏╥)", "(ಥ﹏ಥ)", "(｡•́︿•̀｡)", "(T_T)", "(´；ω；`)", "(っ˘̩╭╮˘̩)っ", "(个_个)", "( ≧Д≦)",
     "o(TヘTo)", "(；へ：)", "(ᗒᗣᗕ)՞"],
    ["(╯°□°)╯︵ ┻━┻", "┬─┬ノ( º _ ºノ)", "(ノಠ益ಠ)ノ彡┻━┻", "(ಠ_ಠ)", "(¬_¬)", "(＃`Д´)", "(╬ Ò﹏Ó)",
     "ヽ(`⌒´メ)ノ", "(ง'̀-'́)ง", "(눈_눈)", "ಠ╭╮ಠ"],
    ["¯\\_(ツ)_/¯", "( ͡° ͜ʖ ͡°)", "(⊙_⊙)", "(°ロ°)", "┐(￣ヘ￣)┌", "(・_・;)", "(￣～￣;)", "(・・ ) ?",
     "( •_•)>⌐■-■", "(⌐■_■)", "(ʘ‿ʘ)"],
    ["ʕ•ᴥ•ʔ", "ʕっ•ᴥ•ʔっ", "(=^･ω･^=)", "ฅ^•ﻌ•^ฅ", "(ᵔᴥᵔ)", "U・ᴥ・U", "(・θ・)", "ヾ(・ω・)ノ",
     "(－_－) zzZ", "( ˘▽˘)っ♨", "(°)#))<<"],
]


def emoji_tabs(kao_set=0):
    n = len(EMOJI_PAGES) + 1
    tabs = [K(icon, 1.5 + i * 7 / n, 7 / n, f"epage{i}", "sys") for i, (icon, _) in enumerate(EMOJI_PAGES)]
    return tabs + [K(f"^_^ {kao_set + 1}/{len(KAOMOJI)}" if kao_set >= 0 else "^_^",
                     1.5 + (n - 1) * 7 / n, 7 / n, f"epage{KAO_TAB}", "sys")]


def kaomoji_rows(kao_set):
    k = KAOMOJI[kao_set]
    return [
        [K(c, i * 2.5, 2.5) for i, c in enumerate(k[:4])],
        [K(c, i * 2.5, 2.5) for i, c in enumerate(k[4:8])],
        [K("⌫", 0, 1.5, "bksp", "sys")] + [K(c, 1.5 + i * 8.5 / 3, 8.5 / 3) for i, c in enumerate(k[8:11])],
        [K("ABC", 0, 1.5, "abc", "sys")] + emoji_tabs(kao_set) + [K("⏎", 8.5, 1.5, "enter", "enter")],
    ]


def emoji_rows(emojis, page):
    e = (emojis + [""] * EMOJI_PER_PAGE)[:EMOJI_PER_PAGE]
    tabs = emoji_tabs(-1)
    return [
        [K(c, i) for i, c in enumerate(e[:10]) if c],
        [K(c, i) for i, c in enumerate(e[10:20]) if c],
        [K("⌫", 0, 1.5, "bksp", "sys")] + [K(c, 1.5 + i * 8.5 / 8, 8.5 / 8) for i, c in enumerate(e[20:]) if c],
        [K("ABC", 0, 1.5, "abc", "sys")] + tabs + [K("⏎", 8.5, 1.5, "enter", "enter")],
    ]


LAYERS = {
    "abc": [
        [K(c, i) for i, c in enumerate("qwertyuiop")],
        [K(c, 0.5 + i) for i, c in enumerate("asdfghjkl")],
        [K("⌫", 0, 1.5, "bksp", "sys")] + [K(c, 1.5 + i) for i, c in enumerate("zxcvbnm")]
        + [K("⇧", 8.5, 1.5, "shift", "sys")],
        bottom_row(("?123", "sym")),
    ],
    "sym": [
        [K(c, i) for i, c in enumerate("1234567890")],
        [K(c, i) for i, c in enumerate("@#$_&-+()/")],
        [K("⌫", 0, 1.5, "bksp", "sys")] + [K(c, 1.5 + i) for i, c in enumerate("*\"':;!?")]
        + [K("=\\<", 8.5, 1.5, "sym2", "sys")],
        bottom_row(("ABC", "abc")),
    ],
    "sym2": [
        [K(c, i) for i, c in enumerate("~`|•√π÷×¶∆")],
        [K(c, i) for i, c in enumerate("£€¥^°={}\\%")],
        [K("⌫", 0, 1.5, "bksp", "sys")] + [K(c, 1.5 + i) for i, c in enumerate("©®™✓[]<")]
        + [K("?123", 8.5, 1.5, "sym", "sys")],
        bottom_row(("ABC", "abc")),
    ],
    "nav": [  # terminal-ish extras, from the long-press of ⏎
        [K(l, i, 1.0, a, "sys") for i, (l, a) in enumerate(
            [("esc", "k1"), ("tab", "k15"), ("^c", "c^c"), ("^d", "c^d"), ("^z", "c^z"),
             ("^l", "c^l"), ("^r", "c^r"), ("^a", "c^a"), ("^e", "c^e"), ("^w", "c^w")])],
        [K(l, 0.5 + i, 1.0, a, "sys") for i, (l, a) in enumerate(
            [("home", "k102"), ("◀", "k105"), ("▲", "k103"), ("▼", "k108"), ("▶", "k106"),
             ("end", "k107"), ("pgup", "k104"), ("pgdn", "k109"), ("del", "k111")])],
        [K("⌫", 0, 1.5, "bksp", "sys")] + [K(c, 1.5 + i) for i, c in enumerate("|&~/-_")]
        + [K("Fn", 7.5, 1, "fn", "sys"), K("ABC", 8.5, 1.5, "abc", "sys")],
        bottom_row(("ABC", "abc")),
    ],
    "fn": [
        [K(f"F{i + 1}", i, 1.0, f"k{58 + i + 1}", "sys") for i in range(10)],
        [K(l, 0.5 + i, 1.0, a, "sys") for i, (l, a) in enumerate(
            [("F11", "k87"), ("F12", "k88"), ("ins", "k110"), ("del", "k111"), ("home", "k102"),
             ("end", "k107"), ("pgup", "k104"), ("pgdn", "k109"), ("menu", "k127")])],
        [K("⌫", 0, 1.5, "bksp", "sys")] + [K(l, 1.5 + i, 1.0, a, "sys") for i, (l, a) in enumerate(
            [("esc", "k1"), ("tab", "k15"), ("◀", "k105"), ("▲", "k103"), ("▼", "k108"), ("▶", "k106"),
             ("⏎", "k28")])] + [K("nav", 8.5, 1.5, "nav", "sys")],
        bottom_row(("ABC", "abc")),
    ],
}

LAYERS["emoji"] = emoji_rows(EMOJI_PAGES[1][1].split(), 1)

DEFAULTS = {"w": 1.0, "x": 0.0, "h": 1.0, "lift": 0}
ONE_DEFAULTS = {"w": 0.78, "x": 0.0, "h": 1.0, "lift": 0}  # one-handed: docked left


def load_conf():
    try:
        with open(CONF) as f:
            c = json.load(f)
    except Exception:
        c = {}
    for o in ("portrait", "landscape"):
        d = c.get(o, {})
        if "full" not in d:  # old flat format: that was the full-width geometry
            d = {"mode": "full", "full": {k: d[k] for k in DEFAULTS if k in d}}
        d["full"] = {**DEFAULTS, **d.get("full", {})}
        d["one"] = {**ONE_DEFAULTS, **d.get("one", {})}
        d.setdefault("mode", "full")
        c[o] = d
    c.setdefault("lang", "en")
    return c


def save_conf(c):
    os.makedirs(CONF_DIR, exist_ok=True)
    tmp = CONF + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=1)
    os.replace(tmp, CONF)


class Touch:
    def __init__(self, x, y, key):
        self.x0, self.y0, self.x, self.y = x, y, x, y
        self.key = key
        self.pts = [(x, y)]
        self.mode = "tap"
        self.t0 = time.monotonic()
        self.timer = 0
        self.alts = None
        self.alt_sel = 0
        self.cursor_acc = 0.0
        self.grab = None


class Keyboard(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.notareal.SwipeKbd", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.conf = load_conf()
        self.decoder = Decoder(self.conf["lang"])
        self.layer = "abc"
        self.shift = 0  # 0 off, 1 one-shot, 2 caps lock
        self.shift_tap = 0.0
        self.active = False
        self.forced = None  # SetVisible override: None follow IM, False hidden
        self.purpose = 0
        self.hint = 0
        self.purpose_seen = None
        self.before = ""  # surrounding text before the cursor (if the app reports it)
        self.word = ""  # word being tap-typed
        self.last_swipe = None  # (committed word, had leading space)
        self.has_surrounding = False
        self.suggest = []
        self.suggest_kind = None  # "swipe" | "complete"
        self.touches = {}
        self.resize = False
        self.space_time = 0.0
        self.edit_time = 0.0  # last commit/backspace we sent (see the "S" handler)
        self.helper = None
        self.keys = []
        self.dbus_conn = None
        self.visible = False
        self.mods = {}  # latched modifiers: mask -> 1 one-shot / 2 locked
        self.term = term_shortcuts()
        self.bar_scroll = 0.0
        self.dictation = Dictation(self.dictated, self.dictation_state)
        self.dict_pulse = 0

    # ---------- setup ----------
    def do_activate(self):
        css = Gtk.CssProvider()
        css.load_from_data(b"window, .kbd { background: transparent; }", -1)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, 800)
        if theme:   # live theme switch: re-read the palette, redraw
            theme.apply()
            theme.on_change(lambda: (load_theme(), self.area.queue_draw()))
        win = self.win = Gtk.Window(application=self)
        win.add_css_class("kbd")
        LS.init_for_window(win)
        LS.set_layer(win, LS.Layer.TOP)  # TOP like Stevia: phoc then stacks us above the Phosh home bar (overlay covered it)
        LS.set_namespace(win, "osk")
        for e in (LS.Edge.BOTTOM, LS.Edge.LEFT, LS.Edge.RIGHT):
            LS.set_anchor(win, e, True)
        LS.set_keyboard_mode(win, LS.KeyboardMode.NONE)
        # exclusive zone is set by hand in update_height (capped, see there)
        self._layouts = {}
        self.area = Canvas(self)
        ctl = Gtk.EventControllerLegacy()
        ctl.connect("event", self.on_event)
        self.area.add_controller(ctl)
        win.set_child(self.area)
        self.update_height()
        self.hold()
        self.start_helper()
        if os.environ.get("SWIPEKBD_NO_DBUS"):  # nested test compositor: leave the real OSK alone
            return
        Gio.bus_own_name(Gio.BusType.SESSION, "sm.puri.OSK0",
                         Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT | Gio.BusNameOwnerFlags.REPLACE,
                         self.on_bus, None, self.on_name_lost)

    # ---------- sm.puri.OSK0 D-Bus ----------
    XML = """<node><interface name="sm.puri.OSK0">
      <method name="SetVisible"><arg type="b" name="visible" direction="in"/></method>
      <method name="Dismiss"/>
      <method name="Restore"/>
      <property name="Visible" type="b" access="read"/></interface></node>"""

    def on_bus(self, conn, name):
        self.dbus_conn = conn
        info = Gio.DBusNodeInfo.new_for_xml(self.XML).interfaces[0]
        conn.register_object("/sm/puri/OSK0", info, self.dbus_call, self.dbus_get, None)

    def on_name_lost(self, conn, name):
        if self.dbus_conn is not None:  # someone replaced us
            print("swipekbd: lost sm.puri.OSK0, exiting", file=sys.stderr)
            self.quit()

    def dbus_call(self, conn, sender, path, iface, method, params, inv):
        if method == "SetVisible":
            self.forced = bool(params.unpack()[0])
            self.apply_visible()
        elif method == "Restore":
            # the shade is closed again (overview widget follows Phosh's log): come back
            if getattr(self, "dismissed", False):
                self.dismissed = False
                self.forced = None
                self.apply_visible()
        elif method == "Dismiss":
            # 10-16: the overview widget calls this when a touch starts in the top bar. Our
            # exclusive zone (half the screen) kept Phosh's shade from unfolding: it "opened"
            # but never showed. Hide until the text field speaks again (tap = cursor event).
            if self.visible:
                self.dismissed = True
                self.forced = False
                self.end_resize() if hasattr(self, "end_resize") else None
                self.apply_visible()
        inv.return_value(None)

    def dbus_get(self, conn, sender, path, iface, prop):
        return GLib.Variant("b", self.visible)

    def apply_visible(self):
        want = self.forced if self.forced is not None else self.active
        if want == self.visible:
            return
        self.visible = want
        if want:
            self.win.present()
        else:
            self.end_resize()
            self.win.set_visible(False)
        if self.dbus_conn:
            self.dbus_conn.emit_signal(None, "/sm/puri/OSK0", "org.freedesktop.DBus.Properties",
                                       "PropertiesChanged", GLib.Variant(
                                           "(sa{sv}as)", ("sm.puri.OSK0", {"Visible": GLib.Variant("b", want)}, [])))

    # ---------- helper process ----------
    def start_helper(self):
        try:
            self.helper = Gio.Subprocess.new([os.path.join(HERE, "swipe-im")],
                                             Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE)
        except GLib.Error as e:
            print("swipekbd: helper:", e.message, file=sys.stderr)
            GLib.timeout_add(300, self._retry_helper)
            return
        self.h_in = self.helper.get_stdin_pipe()
        self.h_out = Gio.DataInputStream.new(self.helper.get_stdout_pipe())
        self.h_out.read_line_async(GLib.PRIORITY_DEFAULT, None, self.on_line)
        self.helper.wait_async(None, self.on_helper_exit)

    def _retry_helper(self):
        self.start_helper()
        return False

    def on_helper_exit(self, proc, res):
        proc.wait_finish(res)
        print("swipekbd: helper exited", proc.get_exit_status(), file=sys.stderr)
        self.active = False
        self.apply_visible()
        GLib.timeout_add(300, self._retry_helper)

    def send(self, line):
        if self.helper:
            try:
                self.h_in.write_all((line + "\n").encode(), None)
            except GLib.Error:
                pass

    def on_line(self, stream, res):
        try:
            line, _ = stream.read_line_finish_utf8(res)
        except GLib.Error:
            return
        if line is None:
            return
        self.handle_event(line)
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.on_line)

    def handle_event(self, line):
        c = line[:1]
        if c in "AX":  # focus in/out of a text field (debugging swipes lost over Circle, 10-18)
            print(time.strftime("%H:%M:%S"), "im", c, file=sys.stderr, flush=True)
        if getattr(self, "dismissed", False) and c == "A":
            # a NEW text field got focus: show again. Not on S/T: terminals (Console) send those
            # constantly and brought the keyboard straight back over the shade (10-17)
            self.dismissed = False
            self.forced = None
            GLib.idle_add(lambda: (self.apply_visible(), False)[1])
        if c == "A":
            self.active = True
            self.forced = None
            self.word = ""
            self.last_swipe = None
            self.before = ""
            self.has_surrounding = False
            self.purpose_seen = None
            self.set_suggest([], None)
            self.apply_visible()
        elif c == "X":
            self.dictation.cancel()
            self.mods.clear()
            self.active = False
            self.forced = None
            self.touches.clear()
            self.apply_visible()
        elif c == "T":
            h, p = line[2:].split()
            fresh = self.purpose_seen != int(p)  # apps resend T after every commit: only react to a new field
            self.hint, self.purpose = int(h), int(p)
            self.purpose_seen = self.purpose
            want = "sym" if self.purpose in (2, 3, 4, 9) else None
            if not fresh:
                pass
            elif want and self.layer == "abc":
                self.set_layer(want)
            elif not want and self.layer in ("sym", "nav") and not self.word:
                self.set_layer("abc")
        elif c == "S":
            _, cur, anc, text = (line.split(" ", 3) + [""])[:4]
            text = text.replace("\\n", "\n").replace("\\\\", "\\")
            self.has_surrounding = True
            b = text.encode()[: int(cur)].decode(errors="ignore")
            # apps often echo the text from BEFORE our last commit/backspace (it lands a moment
            # later); trusting that stale copy re-armed shift mid-word ("HEllo"). Keep ours.
            if time.monotonic() - self.edit_time < 1.0 and not b.endswith(self.before[-20:]):
                return
            self.before = b
            self.auto_shift()
        elif c == "U":
            print("swipekbd: input method unavailable (Stevia still running?)", file=sys.stderr)

    # ---------- text helpers ----------
    def no_dict(self):
        return self.purpose in (2, 3, 4, 5, 6, 8, 9, 13) or self.hint & (64 | 128)

    def commit(self, text):
        self.send("C " + text.replace("\\", "\\\\").replace("\n", "\\n"))
        self.before = (self.before + text)[-200:]
        self.edit_time = time.monotonic()

    def replace(self, old, new):
        """Swap the `old` text right before the cursor for `new`. Backspace keys (virtual
        keyboard) and commits (input method) travel separately, so the app could get the new
        word before the backspaces and erase it; delete_surrounding + commit is one atomic step.
        Apps that never report surrounding text get backspaces and a slightly later commit."""
        if self.has_surrounding:
            self.send("R %d %s" % (len(old.encode()), new.replace("\\", "\\\\").replace("\n", "\\n")))
            self.before = (self.before[: -len(old)] if old and self.before.endswith(old) else self.before) + new
            self.before = self.before[-200:]
            self.edit_time = time.monotonic()
        else:
            self.backspace(len(old))
            GLib.timeout_add(80, lambda: (self.commit(new), False)[1])

    def key(self, code, mods=0):
        self.send(f"K {code} {mods}")

    def latched(self):
        m = 0
        for mask in self.mods:
            m |= mask
        return m

    def mod_key(self, code, mods=0):
        """Send a key with any latched Ctrl/Alt, then drop the one-shot latches."""
        self.key(code, mods | self.latched())
        self.mods = {k: v for k, v in self.mods.items() if v == 2}
        self.word = ""
        self.last_swipe = None
        self.area.queue_draw()

    def toggle_mod(self, mask):
        st = self.mods.get(mask, 0)
        if st == 2:
            self.mods.pop(mask)
        else:
            self.mods[mask] = st + 1
        self.area.queue_draw()

    def term_mode(self):
        return self.purpose == 13

    def backspace(self, n=1):
        for _ in range(n):
            self.key(KEY_BACKSPACE)
        self.before = self.before[:-n] if n <= len(self.before) else ""
        self.edit_time = time.monotonic()

    def auto_shift(self):
        if self.shift == 2 or not (self.hint & 4 or self.purpose == 0) or self.no_dict():
            return
        b = self.before.rstrip(" ")
        new = 1 if (not b or b[-1] in ".!?\n" and self.before.endswith((" ", "\n"))) else 0
        if new and (self.word or self.last_swipe):  # never re-arm shift in the middle of a word
            return
        if new != self.shift:
            self.shift = new
            self.area.queue_draw()

    def cased(self, w):
        if self.shift == 2:
            return w.upper()
        if self.shift == 1:
            return w[:1].upper() + w[1:]
        return w

    def tap_suggest(self):
        """Completions for the tapped word; an unknown word (2+ letters) is offered as
        "＋word" in the leftmost slot: tapping it adds it to the user dictionary."""
        comps = self.decoder.complete(self.word, 3)
        w = self.word
        if len(w) >= 2 and w.isalpha() and not self.decoder.is_known(w):
            comps = [ADD_MARK + w] + [c for c in comps if c.lower() != w.lower()][:2]
        self.set_suggest(comps, "complete")

    def set_suggest(self, items, kind):
        self.suggest, self.suggest_kind = items, kind
        self.area.queue_draw()

    def buzz(self, ev="button-pressed"):
        if not KEY_HAPTICS:
            return  # 10-17 user: no haptics on key taps
        if Lfb:
            try:
                Lfb.Event.new(ev).trigger_feedback_async(None, None, None)
            except Exception:
                pass

    # ---------- actions ----------
    def type_char(self, ch):
        if self.mods:
            if self.layer == "abc" and ch.isalpha() and self.shift:
                ch = ch.upper()
            ck = char_key(ch)
            if ck:
                if self.shift == 1:
                    self.shift = 0
                return self.mod_key(ck[0], ck[1])
        if self.layer == "abc" and ch.isalpha():
            ch = ch.upper() if self.shift else ch
            if self.shift == 1:
                self.shift = 0
            self.word += ch
            self.last_swipe = None
            self.commit(ch)
            if not self.no_dict():
                self.tap_suggest()
            return
        # punctuation / symbol: glue to the previous word, end the current one
        if self.last_swipe and ch in ".,!?;:" and self.before.endswith(" "):
            self.backspace()
        self.commit(ch)
        self.word = ""
        self.last_swipe = None
        self.set_suggest([], None)
        if ch in ".!?":
            self.auto_shift()

    def action(self, act):
        if self.mods and act in ("bksp", "enter", " "):
            return self.mod_key({"bksp": KEY_BACKSPACE, "enter": KEY_ENTER, " ": 57}[act])
        if act == "latch-ctrl":
            return self.toggle_mod(MOD_CTRL)
        if act == "latch-alt":
            return self.toggle_mod(MOD_ALT)
        if act.startswith("m") and ":" in act:  # shortcut bar: m<mods>:<code>
            m, code = act[1:].split(":")
            return self.mod_key(int(code), int(m))
        if act == "bksp":
            if self.last_swipe:
                w, sp = self.last_swipe
                self.backspace(len(w) + sp)
                self.last_swipe = None
                self.set_suggest([], None)
            else:
                self.backspace()
                self.word = self.word[:-1]
                if self.word and not self.no_dict():
                    self.tap_suggest()
                else:
                    self.set_suggest([], "complete")
            self.auto_shift()
        elif act == " ":
            now = time.monotonic()
            if now - self.space_time < 0.4 and self.before.endswith(" ") and len(self.before) > 1 \
                    and self.before[-2].isalnum() and not self.no_dict():
                self.backspace()
                self.commit(". ")
                self.space_time = 0
            elif self.term_mode():  # terminals (kgx/VTE) drop a committed lone space: press the key
                self.key(57)
                self.before = (self.before + " ")[-200:]
            else:
                self.commit(" ")
                self.space_time = now
            self.word = ""
            self.last_swipe = None
            self.set_suggest([], None)
            self.auto_shift()
        elif act == "enter":
            self.key(KEY_ENTER)
            self.before += "\n"
            self.word = ""
            self.last_swipe = None
            self.set_suggest([], None)
            self.auto_shift()
        elif act == "shift":
            now = time.monotonic()
            if self.shift == 0:
                self.shift = 2 if now - self.shift_tap < 0.35 else 1
            elif self.shift == 1 and now - self.shift_tap < 0.35:
                self.shift = 2
            else:
                self.shift = 0
            self.shift_tap = now
        elif act == "mic":
            self.dictation.toggle(self.conf["lang"])
        elif act == "emoji":
            self.emoji_page(0 if self.conf.get("recent_emoji") else 1)
        elif act.startswith("epage"):
            self.emoji_page(int(act[5:]))
        elif act in LAYERS:
            self.set_layer(act)
        elif act == "lang":
            self.set_lang(LANGS[(LANGS.index(self.conf["lang"]) + 1) % len(LANGS)])
        elif act[:1] == "k" and act[1:].isdigit():  # raw key code (nav layer); plain "k" is a letter
            self.mod_key(int(act[1:]))
        elif act.startswith("c^"):
            code = {"c": 46, "d": 32, "z": 44, "l": 38, "r": 19, "a": 30, "e": 18, "w": 17}[act[2]]
            self.key(code, 4)
        else:
            self.type_char(act)
            if self.layer == "emoji" and getattr(self, "kao_set", -1) < 0:  # kaomoji stay out of recents
                rec = [act] + [e for e in self.conf.get("recent_emoji", []) if e != act]
                self.conf["recent_emoji"] = rec[:EMOJI_PER_PAGE]
                save_conf(self.conf)
        self.area.queue_draw()

    def dictation_state(self, state):
        if state == "recording" and not self.dict_pulse:
            self.dict_pulse = GLib.timeout_add(250, self.dict_tick)
        self.buzz()
        self.area.queue_draw()

    def dict_tick(self):
        self.area.queue_draw()
        if self.dictation.state == "idle":
            self.dict_pulse = 0
            return False
        return True

    def dictated(self, text):
        """Type what whisper heard, spaced and cased to fit the text before the cursor."""
        if not self.active:
            return
        b = self.before.rstrip(" ")
        mid = b and b[-1] not in ".!?\n"
        if mid and text[:1].isupper() and not text.startswith(("I ", "I'")) and not text[1:2].isupper():
            text = text[0].lower() + text[1:]
        if mid and text[-1:] == "." and text.count(".") == 1:
            text = text[:-1]  # whisper ends every snippet with a period; keep mid-sentence text open
        if self.before and not self.before.endswith((" ", "\n")):
            text = " " + text
        self.commit(text + " ")
        self.word = ""
        self.last_swipe = None
        self.set_suggest([], None)
        self.auto_shift()
        self.area.queue_draw()

    def set_lang(self, lang):
        self.conf["lang"] = lang
        save_conf(self.conf)
        self.decoder.load(lang)
        self.flash = (lang.upper(), time.monotonic())
        GLib.timeout_add(900, lambda: self.area.queue_draw() and False)

    def emoji_page(self, page):
        if page == KAO_TAB:  # kaomoji: tapping the tab again shows the next set
            if self.layer == "emoji" and getattr(self, "kao_set", -1) >= 0:
                self.kao_set = (self.kao_set + 1) % len(KAOMOJI)
            else:
                self.kao_set = 0
            LAYERS["emoji"] = kaomoji_rows(self.kao_set)
            self.set_layer("emoji")
            return
        self.kao_set = -1
        emojis = self.conf.get("recent_emoji", []) if page == 0 else EMOJI_PAGES[page][1].split()
        LAYERS["emoji"] = emoji_rows(emojis, page)
        self.set_layer("emoji")

    def set_layer(self, name):
        self.layer = name
        self.relayout()

    def pick_suggestion(self, i):
        if i >= len(self.suggest):
            return
        w = self.suggest[i]
        if self.suggest_kind == "swipe" and self.last_swipe:
            old, sp = self.last_swipe
            w = self.match_case(old, w)
            self.replace(old, w)
            self.last_swipe = (w, sp)
            rest = [s for s in self.swipe_all if s != w][:3]
            self.set_suggest([old] + [s for s in rest if s != old][:2], "swipe")
        elif self.suggest_kind == "complete" and self.word and w.startswith(ADD_MARK):
            w = w[len(ADD_MARK):]
            self.decoder.add_word(w)   # as typed: mostly names; completion ignores case
            self.replace(self.word, w + " ")
            self.word = ""
            self.set_suggest([], None)
            self.flash = ("added to dictionary", time.monotonic())
            GLib.timeout_add(850, lambda: (self.area.queue_draw(), False)[1])
        elif self.suggest_kind == "complete" and self.word:
            w = self.match_case(self.word, w)
            self.replace(self.word, w + " ")
            self.word = ""
            self.set_suggest([], None)
        self.buzz()
        self.auto_shift()

    @staticmethod
    def match_case(old, new):
        if old.isupper() and len(old) > 1:
            return new.upper()
        if old[:1].isupper():
            return new[:1].upper() + new[1:]
        return new

    def do_swipe(self, t):
        g = self.geom
        pts = [((x - g["kx"]) / g["kw"], (y - g["ky"]) / g["kw"]) for x, y in t.pts]
        res = self.decoder.decode(pts, row=g["kh"] / g["kw"], k=5)
        if not res:
            return
        words = [w for w, _ in res]
        w = self.cased(words[0])
        if self.shift == 1:
            self.shift = 0
        sp = bool(self.before) and not self.before.endswith((" ", "\n", "(", "¿", "¡", "\"", "/"))
        if self.word:  # glide after tapping letters: start a new word
            sp = True
        self.commit((" " if sp else "") + w)
        self.word = ""
        self.last_swipe = (w, sp)
        self.swipe_all = words
        self.set_suggest(words[1:4], "swipe")
        self.buzz()

    # ---------- geometry ----------
    def orient(self):
        mon = self.monitor_geom()
        return "landscape" if mon.width > mon.height else "portrait"

    def watch_monitor(self, mon):
        if getattr(self, "_watched", None) is mon:
            return
        self._watched = mon
        mon.connect("notify::geometry", self.on_monitor_changed)

    def on_monitor_changed(self, mon, _p):
        g = mon.get_geometry()
        if os.environ.get("SWIPEKBD_DEBUG"):
            print(f"swipekbd: monitor changed -> {g.width}x{g.height}", file=sys.stderr, flush=True)
        self.update_height()
        self.relayout()

    def monitor_geom(self):
        surf = self.win.get_surface()
        disp = Gdk.Display.get_default()
        mon = disp.get_monitor_at_surface(surf) if surf else None
        if mon is None:
            mon = disp.get_monitors().get_item(0)
        self.watch_monitor(mon)
        return mon.get_geometry()

    def conf_o(self):
        """Geometry of the active mode (full / one) for the current orientation."""
        d = self.conf[self.orient()]
        return d[d["mode"]]

    def one_handed(self):
        return self.conf[self.orient()]["mode"] == "one"

    def set_mode(self, mode, x=None):
        self.conf[self.orient()]["mode"] = mode
        if x is not None:
            self.conf_o()["x"] = x
        save_conf(self.conf)
        self.update_height()
        self.relayout()

    def update_height(self):
        c = self.conf_o()
        land = self.orient() == "landscape"
        self.row_h = (36 if land else 50) * c["h"]
        self.bar_h = 30 if land else 36
        # Never reserve more than ~half the screen: phoc aborts (wlr_xdg_toplevel_set_size
        # assertion) when our exclusive zone leaves app windows a negative height.
        cap = self.monitor_geom().height * 0.55
        if self.bar_h + 4 * self.row_h + 6 > cap:
            self.row_h = (cap - self.bar_h - 6) / 4
        c["lift"] = int(max(0, min(c["lift"], cap - self.bar_h - 4 * self.row_h - 6)))
        total = int(self.bar_h + 4 * self.row_h + 6 + c["lift"])
        self.area.set_size_request(-1, total)
        self.win.set_default_size(-1, total)
        # Reserve at most the screen's SHORT side minus a margin. phoc rearranges app windows
        # the instant the output changes size (rotation, nested window resize) - before we can
        # shrink - and aborts on a negative window height (wlr_xdg_toplevel_set_size assert).
        # With this cap, usable height stays positive in any orientation. We may draw taller.
        m = self.monitor_geom()
        zone = int(min(total, min(m.width, m.height) - 100))
        LS.set_exclusive_zone(self.win, max(0, zone))

    def relayout(self):
        W = self.area.get_width() or self.monitor_geom().width
        if getattr(self, "_orient", None) != self.orient():
            self._orient = self.orient()
            self.update_height()
        H = self.area.get_height()
        c = self.conf_o()
        if self.orient() == "landscape":
            c = {**c, "w": min(c["w"], 0.62)} if c["w"] >= 0.99 else c
        kbw = W * max(0.45, min(1.0, c["w"]))
        free = W - kbw
        kx = free * max(0.0, min(1.0, c["x"]))
        ky = self.bar_h + 2
        kh = self.row_h
        kw = kbw / 10
        self.geom = dict(W=W, H=H, kx=kx, ky=ky, kw=kw, kh=kh, kbw=kbw, free=free,
                         bottom=ky + 4 * kh)
        self.keys = []
        for r, row in enumerate(LAYERS[self.layer]):
            for k in row:
                self.keys.append({**k, "rect": (kx + k["x"] * kw, ky + r * kh, k["w"] * kw, kh), "row": r})
        self.area.queue_draw()

    def key_at(self, x, y):
        best, bd = None, 1e9
        for k in self.keys:
            rx, ry, rw, rh = k["rect"]
            if rx <= x < rx + rw and ry <= y < ry + rh:
                return k
            cx, cy = rx + rw / 2, ry + rh / 2
            d = math.hypot((x - cx) / rw, (y - cy) / rh)
            if d < bd:
                best, bd = k, d
        g = self.geom
        if g["kx"] - 4 <= x <= g["kx"] + g["kbw"] + 4 and y >= g["ky"] - 4:
            return best
        return None

    def bar_slots(self):
        """Suggestion slots, then icons: one-hand toggle + resize (full width only), hide."""
        g = self.geom
        x0, w = g["kx"], g["kbw"]
        icon = self.bar_h * 1.1
        names = ["hide"] if self.one_handed() else ["one", "rz", "hide"]
        sw = (w - len(names) * icon) / 3
        slots = [(x0 + i * sw, sw) for i in range(3)]
        icons = [(n, x0 + 3 * sw + i * icon, icon) for i, n in enumerate(names)]
        return slots, icons

    def term_items(self):
        """Shortcut chips laid out in the bar's slot area: (label, act, x, w), scrolled."""
        slots, _ = self.bar_slots()
        x0 = slots[0][0]
        end = slots[-1][0] + slots[-1][1]
        port = self.orient() == "portrait"
        cw = 46 if port else 40
        items, x = [], x0 - self.bar_scroll
        for label, act in self.term:
            w = max(cw, 9 * len(label) + 16)
            items.append((label, act, x, w))
            x += w
        self.bar_max = max(0.0, x + self.bar_scroll - end)
        return items, x0, end

    def side_buttons(self):
        """One-handed strip: a mouse-wheel scroll bar on its left edge, then the handles
        move, height, width grip, full width (top to bottom)."""
        g = self.geom
        if not self.one_handed() or self.resize:
            return []
        lw, rx = g["kx"], g["kx"] + g["kbw"]
        rw = g["W"] - rx
        fx, fw = (rx, rw) if rw >= lw else (0, lw)
        if fw < 30:
            return []
        top, bot = g["ky"], g["bottom"]
        scroll = []
        if fw >= 56:  # room for the scroll bar left of the handles
            sw = min(30, fw * 0.32)
            scroll = [("scroll", (fx + 3, top + 2, sw, bot - top - 4))]
            fx, fw = fx + sw + 6, fw - sw - 6
        s = min(fw * 0.8, 44, (bot - top - 16) / 4.5)
        bx = fx + (fw - s) / 2
        gh = bot - top - 3 * s - 16  # the width grip takes what is left: easy to catch with a thumb
        return scroll + [("move", (bx, top + 2, s, s)), ("height", (bx, top + s + 6, s, s)),
                         ("scale", (bx, top + 2 * s + 10, s, gh)), ("full", (bx, bot - s - 2, s, s))]

    # ---------- input ----------
    def on_event(self, ctl, ev):
        ev = ev or ctl.get_current_event()  # PyGObject may hand us None
        if ev is None:
            return False
        t = ev.get_event_type()
        T = Gdk.EventType
        if t in (T.TOUCH_BEGIN, T.BUTTON_PRESS):
            ok, x, y = ev.get_position()
            self.press(ev.get_event_sequence() or "mouse", x, y)
        elif t in (T.TOUCH_UPDATE, T.MOTION_NOTIFY):
            seq = ev.get_event_sequence() or "mouse"
            if seq in self.touches:
                ok, x, y = ev.get_position()
                self.motion(seq, x, y)
        elif t in (T.TOUCH_END, T.BUTTON_RELEASE):
            seq = ev.get_event_sequence() or "mouse"
            if seq in self.touches:
                ok, x, y = ev.get_position()
                self.release(seq, x, y)
        elif t == T.TOUCH_CANCEL:
            self.touches.pop(ev.get_event_sequence(), None)
            self.area.queue_draw()
        return True

    def press(self, seq, x, y):
        g = self.geom
        if self.resize:
            return self.resize_press(seq, x, y)
        # other taps in flight become real taps right away (fast two-thumb typing)
        for s, o in list(self.touches.items()):
            if o.mode == "tap":
                self.finish_tap(o)
                self.touches.pop(s)
        if y < g["ky"] - 2:
            slots, icons = self.bar_slots()
            if self.term_mode() and slots[0][0] <= x < slots[-1][0] + slots[-1][1]:
                o = self.touches[seq] = Touch(x, y, None)
                o.mode = "bar"
                o.grab = self.bar_scroll
                return
            for i, (sx, sw) in enumerate(slots):
                if sx <= x < sx + sw:
                    self.pick_suggestion(i)
            for name, ix, iw in icons:
                if ix <= x < ix + iw:
                    if name == "one":
                        self.set_mode("one")
                        self.buzz()
                    elif name == "rz":
                        self.start_resize()
                    else:
                        self.forced = False
                        self.apply_visible()
            return
        for name, (bx, by, bw, bh) in self.side_buttons():
            if name == "scroll":  # exact on the handle side, generous elsewhere
                if bx - 12 <= x < bx + bw + 2 and by - 8 <= y < by + bh + 8:
                    o = self.touches[seq] = Touch(x, y, None)
                    o.mode = "scroll"
                    o.grab = ("scroll",)
                    self.area.queue_draw()
                    return
                continue
            # generous hit box: the whole strip row around the handle
            if bx - 12 <= x < bx + bw + 12 and by - 8 <= y < by + bh + 8:
                self.buzz()
                if name == "full":
                    self.set_mode("full")
                    return
                c = self.conf_o()
                o = self.touches[seq] = Touch(x, y, None)
                o.mode = "handle"
                o.grab = (name, c["w"], c["h"], c["x"], c["lift"], self.area.get_height() - y)
                o.g0 = dict(g)
                o.side = 1 if bx > g["kx"] else -1
                self.area.queue_draw()
                return
        k = self.key_at(x, y)
        if not k:
            return
        o = self.touches[seq] = Touch(x, y, k)
        self.buzz()
        if k["act"] == "bksp":
            o.mode = "repeat"
            self.action("bksp")
            o.timer = GLib.timeout_add(420, self.repeat_start, seq)
        elif k["kind"] == "char" or k["act"] in ("enter", "emoji"):
            o.timer = GLib.timeout_add(380, self.long_press, seq)
        self.area.queue_draw()

    def repeat_start(self, seq):
        o = self.touches.get(seq)
        if o and o.mode == "repeat":
            o.timer = GLib.timeout_add(55, self.repeat_tick, seq)
        return False

    def repeat_tick(self, seq):
        o = self.touches.get(seq)
        if not o or o.mode != "repeat":
            return False
        self.action("bksp")
        return True

    def long_press(self, seq):
        o = self.touches.get(seq)
        if not o or o.mode != "tap":
            return False
        o.timer = 0
        k = o.key
        if k["act"] == "enter":
            o.mode = "done"
            self.set_layer("nav")
            self.buzz("button-pressed")
            return False
        if k["act"] == "emoji":  # menu: emoji page or a language, current language preselected
            o.mode = "alts"
            o.alts = ["😊"] + [l.upper() for l in LANGS]
            o.alt_sel = 1 + LANGS.index(self.conf["lang"])
            self.buzz()
            self.area.queue_draw()
            return False
        base = k["act"].lower()
        alts = ([TOPNUM[base]] if self.layer == "abc" and base in TOPNUM else []) + list(ACCENTS.get(base, ""))
        if not alts:
            return False
        if self.layer == "abc" and self.shift:
            alts = [a.upper() for a in alts]
        o.mode = "alts"
        o.alts = alts
        o.alt_sel = 0
        self.buzz()
        self.area.queue_draw()
        return False

    def motion(self, seq, x, y):
        o = self.touches[seq]
        if self.resize:
            return self.resize_motion(o, x, y)
        if o.mode == "handle":
            return self.handle_drag(o, x, y)
        if o.mode == "scroll":
            return self.wheel(y - o.y, o, y)
        if o.mode == "bar":
            if abs(x - o.x0) > 10:
                o.cursor_acc = 1  # scrolled: not a tap
            self.term_items()
            self.bar_scroll = max(0.0, min(self.bar_max, o.grab - (x - o.x0)))
            self.area.queue_draw()
            return
        g = self.geom
        o.x, o.y = x, y
        o.pts.append((x, y))
        if o.mode == "alts":
            ax0 = self.alts_x0(o)
            o.alt_sel = max(0, min(len(o.alts) - 1, int((x - ax0) // g["kw"])))
        elif o.mode == "tap":
            dist = math.hypot(x - o.x0, y - o.y0)
            if o.key["kind"] == "space" and abs(x - o.x0) > 0.4 * g["kw"]:
                o.mode = "cursor"
                o.cursor_acc = x - o.x0
            elif dist > 0.45 * g["kw"] and o.key["kind"] == "char" and self.layer == "abc" \
                    and o.key["act"].isalpha() and not self.no_dict() and not self.mods:
                o.mode = "swipe"
            if o.mode != "tap" and o.timer:
                GLib.source_remove(o.timer)
                o.timer = 0
        elif o.mode == "cursor":
            o.cursor_acc += x - o.pts[-2][0]
            step = 0.33 * g["kw"]
            while abs(o.cursor_acc) >= step:
                right = o.cursor_acc > 0
                self.key(KEY_RIGHT if right else KEY_LEFT)
                o.cursor_acc -= step if right else -step
                self.word = ""
                self.last_swipe = None
        self.area.queue_draw()

    def finish_tap(self, o):
        if o.timer:
            GLib.source_remove(o.timer)
            o.timer = 0
        self.action(o.key["act"])

    def release(self, seq, x, y):
        o = self.touches.pop(seq)
        if self.resize:
            return self.resize_release(o)
        if o.mode == "handle":
            save_conf(self.conf)
            self.area.queue_draw()
            return
        if o.mode == "scroll":
            self.send("w")  # axis_stop: lets apps coast (kinetic scrolling)
            self.area.queue_draw()
            return
        if o.mode == "bar":
            if not o.cursor_acc:
                items, x0, end = self.term_items()
                for label, act, ix, iw in items:
                    if ix <= x < ix + iw and x0 <= x < end:
                        self.buzz()
                        self.action(act)
            self.area.queue_draw()
            return
        if o.timer:
            GLib.source_remove(o.timer)
            o.timer = 0
        if o.mode == "tap":
            self.action(o.key["act"])
        elif o.mode == "alts" and o.key["act"] == "emoji":
            pick = o.alts[o.alt_sel]
            if pick == "😊":
                self.action("emoji")
            elif pick.lower() != self.conf["lang"]:
                self.set_lang(pick.lower())
        elif o.mode == "alts":
            self.type_char(o.alts[o.alt_sel])
        elif o.mode == "swipe":
            o.pts.append((x, y))
            self.do_swipe(o)
        self.area.queue_draw()

    def wheel(self, dy, o, y):
        """Scroll bar: drag down = scroll the app down, like a mouse wheel / scrollbar.
        The virtual pointer is parked in the middle of the app area above the keyboard."""
        o.y = y
        if not dy:
            return
        m = self.monitor_geom()
        px = m.x + m.width // 2
        py = m.y + max(1, (m.height - self.area.get_height()) // 2)
        self.send("W %d %d %d %d %.2f" % (px, py, m.x + m.width, m.y + m.height, dy * 2.0))
        self.area.queue_draw()

    def handle_drag(self, o, x, y):
        """Live scale / move from the one-handed handles."""
        name, w0, h0, x0, lift0, b0 = o.grab
        g0 = o.g0
        c = self.conf_o()
        dx = x - o.x0
        up = (self.area.get_height() - y) - b0  # measured from the bottom: stable while we grow
        if name == "scale":  # width only: drag away from the keyboard to widen
            c["w"] = round(max(0.45, min(1.0, w0 + dx * o.side / g0["W"])), 3)
        elif name == "height":  # half-speed vertical drag for fine-tuning the row height
            c["h"] = round(max(0.65, min(1.6, h0 + 0.5 * up / (4 * g0["kh"]) * h0)), 3)
        else:
            free = g0["W"] - g0["kbw"]
            if free > 1:
                c["x"] = max(0.0, min(1.0, (g0["kx"] + dx) / free))
            c["lift"] = int(max(0, min(200, lift0 + up)))
        self.update_height()
        self.relayout()

    # ---------- resize mode ----------
    def start_resize(self):
        self.resize = True
        self.buzz()
        self.area.queue_draw()

    def end_resize(self):
        if self.resize:
            self.resize = False
            save_conf(self.conf)
            self.area.queue_draw()

    def resize_buttons(self):
        g = self.geom
        names = [("one-L", "1H ◀"), ("full", "FULL"), ("one-R", "1H ▶"), ("done", "DONE")]
        bw = g["kbw"] / len(names)
        y = g["ky"] + 3 * g["kh"] + 4
        return [(n, l, (g["kx"] + i * bw + 4, y, bw - 8, g["kh"] - 8)) for i, (n, l) in enumerate(names)]

    def resize_press(self, seq, x, y):
        g = self.geom
        o = self.touches[seq] = Touch(x, y, None)
        for n, l, (bx, by, bw, bh) in self.resize_buttons():
            if bx <= x < bx + bw and by <= y < by + bh:
                o.grab = ("btn", n)
                return
        c = self.conf_o()
        edge = 0.6 * g["kw"]
        if y < g["ky"] + 0.5 * g["kh"]:
            o.grab = ("top", c["h"], self.area.get_height() - y, 4 * self.row_h)
        elif abs(x - g["kx"]) < edge:
            o.grab = ("left",)
        elif abs(x - (g["kx"] + g["kbw"])) < edge:
            o.grab = ("right",)
        else:
            o.grab = ("move", c["x"], c["lift"])
        o.g0 = dict(g)

    def resize_motion(self, o, x, y):
        if not o.grab or o.grab[0] == "btn":
            return
        c = self.conf_o()
        g0 = o.g0
        W = g0["W"]
        kind = o.grab[0]
        dx = x - o.x0
        if kind == "top":
            # distance from the bottom of the surface to the finger sets the height
            dist = self.area.get_height() - y
            h0, d0, rows0 = o.grab[1:]
            c["h"] = round(max(0.65, min(1.6, h0 * (rows0 + dist - d0) / rows0)), 3)
            self.update_height()
        elif kind in ("left", "right"):
            left, right = g0["kx"], g0["kx"] + g0["kbw"]
            if kind == "left":
                left = max(0, min(right - 0.45 * W, left + dx))
            else:
                right = min(W, max(left + 0.45 * W, right + dx))
            c["w"] = (right - left) / W
            free = W - (right - left)
            c["x"] = left / free if free > 1 else 0.0
        elif kind == "move":
            free = g0["free"]
            if free > 1:
                c["x"] = max(0.0, min(1.0, (g0["kx"] + dx) / free))
            dy = o.y0 - y
            c["lift"] = int(max(0, min(200, o.grab[2] + dy)))
            self.update_height()
        self.relayout()

    def resize_release(self, o):
        if o.grab and o.grab[0] == "btn":
            n = o.grab[1]
            if n == "done":
                self.end_resize()
            elif n == "full":
                self.set_mode("full")
            else:
                self.set_mode("one", 0.0 if n == "one-L" else 1.0)
            self.buzz()
        save_conf(self.conf)

    # ---------- drawing (GSK snapshot; no pycairo on this system) ----------
    def rgba(self, c, a=1.0):
        r = Gdk.RGBA()
        r.red, r.green, r.blue, r.alpha = c[0], c[1], c[2], a
        return r

    def rect(self, snap, c, a, x, y, w, h):
        snap.append_color(self.rgba(c, a), Graphene.Rect().init(x, y, w, h))

    def rpath(self, x, y, w, h, r):
        b = Gsk.PathBuilder.new()
        rr = Gsk.RoundedRect()
        rr.init_from_rect(Graphene.Rect().init(x, y, w, h), r * CORNERS)
        b.add_rounded_rect(rr)
        return b.to_path()

    def rfill(self, snap, c, a, x, y, w, h, r):
        snap.append_fill(self.rpath(x, y, w, h, r), Gsk.FillRule.WINDING, self.rgba(c, a))

    def rstroke(self, snap, c, a, x, y, w, h, r, width=1.0, dash=None):
        st = Gsk.Stroke.new(width)
        if dash:
            st.set_dash(dash)
        snap.append_stroke(self.rpath(x, y, w, h, r), st, self.rgba(c, a))

    def text(self, snap, s, x, y, w, h, size, color, alpha=1.0, bold=False):
        ck = (s, size, bold, int(w))
        hit = self._layouts.get(ck)
        if hit is None:
            layout = self.area.create_pango_layout(s)
            fd = Pango.FontDescription.from_string(f"{FONT}{' Bold' if bold else ''} {size}")
            layout.set_font_description(fd)
            lw, lh = layout.get_pixel_size()
            if lw > w - 4 and lw > 0:
                fd.set_size(int(fd.get_size() * (w - 4) / lw))
                layout.set_font_description(fd)
                lw, lh = layout.get_pixel_size()
            if len(self._layouts) > 600:
                self._layouts.clear()
            hit = self._layouts[ck] = (layout, lw, lh)
        layout, lw, lh = hit
        snap.save()
        snap.translate(Graphene.Point().init(x + (w - lw) / 2, y + (h - lh) / 2))
        snap.append_layout(layout, self.rgba(color, alpha))
        snap.restore()

    def draw(self, snap, W, H):
        g = self.geom if self.keys else None
        if not g or abs(g["W"] - W) > 1:
            self.relayout()
            g = self.geom
        g["H"] = H
        self.rect(snap, BG[:3], BG[3], 0, 0, W, H)
        # soft lavender rise toward the bottom (Stevia gtk.css look)
        bands = 6
        for i in range(bands):
            self.rect(snap, (120 / 255, 90 / 255, 170 / 255), 0.16 * (i + 1) / bands / 2,
                      0, H * i / bands, W, H / bands)
        pressed = {id(o.key) for o in self.touches.values() if o.key is not None and o.mode in ("tap", "repeat", "alts")}
        port = self.orient() == "portrait"
        # suggestion bar: best guess leftmost (left-handed)
        slots, icons = self.bar_slots()
        fs = 11 if port else 9
        for i, (sx, sw) in enumerate(slots if not self.term_mode() else []):
            if i < len(self.suggest):
                self.text(snap, self.suggest[i], sx, 0, sw, self.bar_h, fs, CYAN if i == 0 else FG,
                          1.0 if i == 0 else 0.75)
            if i:
                self.rect(snap, LAV, 0.18, sx, self.bar_h * 0.28, 1, self.bar_h * 0.44)
        if self.term_mode():
            self.draw_term_bar(snap)
        flash = getattr(self, "flash", None)
        if flash and time.monotonic() - flash[1] < 0.8:
            self.text(snap, flash[0], slots[0][0], 0, slots[0][1], self.bar_h, fs, MINT)
        if self.dictation.state != "idle":
            msg = "LISTENING · TAP ● TO STOP" if self.dictation.state == "recording" else "TYPING…"
            self.text(snap, msg, slots[0][0], 0, sum(slots[-1]) - slots[0][0], self.bar_h, fs,
                      PINK if self.dictation.state == "recording" else CYAN)
        for name, ix, iw in icons:
            if name == "one":
                self.draw_one_icon(snap, ix + iw / 2, self.bar_h / 2, self.bar_h * 0.5)
            elif name == "rz":
                self.text(snap, "⤢", ix, 0, iw, self.bar_h, fs, LAV, 1 if self.resize else 0.6)
            else:
                self.text(snap, "▾", ix, 0, iw, self.bar_h, fs, LAV, 0.6)
        caps = self.shift and self.layer == "abc"
        for k in self.keys:
            x, y, w, h = k["rect"]
            if id(k) in pressed:
                self.rfill(snap, LAV, 0.16, x + 2, y + 3, w - 4, h - 6, 6)
            lab = k["label"]
            col, alpha, size = FG, 1.0, 13 if port else 11
            if k["kind"] == "sys" and k["act"] == "bksp":
                col, alpha, size = PINK, 1.0, size + 3
            elif k["kind"] == "sys":
                col, alpha, size = LAV, 0.7, size - 2
                if k["act"] == "shift":
                    lab = "⇪" if self.shift == 2 else "⇧"
                    if self.shift:
                        col, alpha = CYAN, 1.0
                elif k["act"] == "lang":
                    lab = self.conf["lang"].upper()
                elif k["act"] == "mic" and self.dictation.state == "recording":
                    lab, col, size = "●", PINK, size + 4
                    alpha = 0.55 + 0.45 * abs(math.sin(time.monotonic() * 3))
                elif k["act"] == "mic" and self.dictation.state == "working":
                    lab, col, alpha = "…", CYAN, 1.0
            elif k["kind"] == "enter":
                col = CYAN
            elif k["kind"] == "space":
                self.rect(snap, PINK, 0.35, x + w * 0.1, y + h * 0.72, w * 0.8, 1)
            elif caps and len(lab) == 1:
                lab = lab.upper()
            if lab:
                self.text(snap, lab, x, y, w, h, size, col, alpha)
            if k["act"] == "emoji":  # the current language, small, like the number hints
                self.text(snap, self.conf["lang"].upper(), x + w * 0.5, y + 2, w * 0.5, h * 0.35, 7, LAV, 0.6)
            if self.layer == "abc" and k["kind"] == "char" and k["act"] in TOPNUM:
                self.text(snap, TOPNUM[k["act"]], x + w * 0.55, y + 2, w * 0.45, h * 0.35, 7, LAV, 0.45)
        dragging = {o.grab[0] for o in self.touches.values() if o.mode in ("handle", "scroll")}
        for name, r in self.side_buttons():
            self.draw_handle(snap, name, r, name in dragging)
        for o in self.touches.values():
            if o.mode == "swipe" and len(o.pts) > 1:
                self.draw_trail(snap, o.pts[-80:])
            elif o.mode == "alts":
                self.draw_alts(snap, o)
            elif o.mode == "cursor":
                x, y, w, h = o.key["rect"]
                self.text(snap, "◀ ▶", x, y, w, h, 11, CYAN)
        if self.resize:
            self.draw_resize(snap)

    def draw_trail(self, snap, pts):
        """Neon trail, pink tail -> cyan head, drawn in a few segments of growing width."""
        n = len(pts)
        parts = 6
        for p in range(parts):
            a, b = n * p // parts, min(n, n * (p + 1) // parts + 1)
            if b - a < 2:
                continue
            t = (p + 1) / parts
            pb = Gsk.PathBuilder.new()
            pb.move_to(*pts[a])
            for q in pts[a + 1:b]:
                pb.line_to(*q)
            st = Gsk.Stroke.new(2 + 5 * t)
            st.set_line_cap(Gsk.LineCap.ROUND)
            st.set_line_join(Gsk.LineJoin.ROUND)
            c = [PINK[j] + (CYAN[j] - PINK[j]) * t for j in range(3)]
            snap.append_stroke(pb.to_path(), st, self.rgba(c, 0.3 + 0.6 * t))

    def line(self, snap, pts, c, a, width=1.5):
        pb = Gsk.PathBuilder.new()
        pb.move_to(*pts[0])
        for q in pts[1:]:
            pb.line_to(*q)
        st = Gsk.Stroke.new(width)
        st.set_line_cap(Gsk.LineCap.ROUND)
        st.set_line_join(Gsk.LineJoin.ROUND)
        snap.append_stroke(pb.to_path(), st, self.rgba(c, a))

    def draw_term_bar(self, snap):
        items, x0, end = self.term_items()
        snap.push_clip(Graphene.Rect().init(x0, 0, end - x0, self.bar_h))
        fs = 10 if self.orient() == "portrait" else 9
        for label, act, ix, iw in items:
            if ix + iw < x0 or ix > end:
                continue
            mask = {"latch-ctrl": MOD_CTRL, "latch-alt": MOD_ALT}.get(act)
            st = self.mods.get(mask, 0) if mask else 0
            col = PINK if st == 2 else CYAN if st == 1 else (LAV if mask else FG)
            self.rfill(snap, col, 0.22 if st else 0.07, ix + 2, 4, iw - 4, self.bar_h - 8, 6)
            self.text(snap, label, ix + 2, 4, iw - 4, self.bar_h - 8, fs, col, 1.0 if st else 0.85)
        if self.bar_max > 0:  # fade hints that there is more to scroll
            if self.bar_scroll > 1:
                self.rect(snap, LAV, 0.35, x0, 6, 2, self.bar_h - 12)
            if self.bar_scroll < self.bar_max - 1:
                self.rect(snap, LAV, 0.35, end - 2, 6, 2, self.bar_h - 12)
        snap.pop()

    def draw_one_icon(self, snap, cx, cy, s):
        """Bar toggle: a keyboard outline with its left part filled = one-handed left."""
        x, y, w, h = cx - s * 0.6, cy - s * 0.4, s * 1.2, s * 0.8
        self.rstroke(snap, LAV, 0.6, x, y, w, h, 2, 1.2)
        self.rfill(snap, LAV, 0.6, x + 2, y + 2, w * 0.55, h - 4, 1.5)

    def draw_handle(self, snap, name, r, active):
        bx, by, s, hh = r
        col, a = (PINK, 1.0) if active else (LAV, 0.75)
        self.rfill(snap, col, 0.18 if active else 0.08, bx, by, s, hh, s * 0.3)
        self.rstroke(snap, col, 0.5 * a, bx, by, s, hh, s * 0.3, 1.2)
        cx, cy, q = bx + s / 2, by + hh / 2, s * 0.26
        if name == "scroll":  # slim track with up/down chevrons and a centre grip
            h = min(s * 0.3, 7)
            for ty, d in ((by + s * 0.7, -1), (by + hh - s * 0.7, 1)):
                self.line(snap, [(cx - h, ty - d * h * 0.5), (cx, ty + d * h * 0.5), (cx + h, ty - d * h * 0.5)], col, a)
            for gy in (-0.08, 0, 0.08):
                self.line(snap, [(cx - s * 0.22, cy + gy * hh), (cx + s * 0.22, cy + gy * hh)], col, a * 0.6, 1.2)
        elif name == "scale":  # grip lines + horizontal two-headed arrow
            for gx in (-0.18, 0, 0.18):
                self.line(snap, [(cx + gx * s, by + hh * 0.18), (cx + gx * s, cy - q * 0.8)], col, a * 0.6, 1.2)
                self.line(snap, [(cx + gx * s, cy + q * 0.8), (cx + gx * s, by + hh * 0.82)], col, a * 0.6, 1.2)
            h = q * 0.4
            self.line(snap, [(cx - q, cy), (cx + q, cy)], col, a)
            self.line(snap, [(cx - q + h, cy - h), (cx - q, cy), (cx - q + h, cy + h)], col, a)
            self.line(snap, [(cx + q - h, cy - h), (cx + q, cy), (cx + q - h, cy + h)], col, a)
        elif name == "height":  # vertical two-headed arrow between two rows
            h = q * 0.4
            self.line(snap, [(cx, cy - q), (cx, cy + q)], col, a)
            self.line(snap, [(cx - h, cy - q + h), (cx, cy - q), (cx + h, cy - q + h)], col, a)
            self.line(snap, [(cx - h, cy + q - h), (cx, cy + q), (cx + h, cy + q - h)], col, a)
            self.line(snap, [(cx - q, cy - q * 0.15), (cx - q * 0.45, cy - q * 0.15)], col, a * 0.6, 1)
            self.line(snap, [(cx + q * 0.45, cy + q * 0.15), (cx + q, cy + q * 0.15)], col, a * 0.6, 1)
        elif name == "move":  # four-way cross
            self.line(snap, [(cx - q, cy), (cx + q, cy)], col, a)
            self.line(snap, [(cx, cy - q), (cx, cy + q)], col, a)
            h = q * 0.35
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                tx, ty = cx + dx * q, cy + dy * q
                self.line(snap, [(tx - dx * h - dy * h, ty - dy * h - dx * h), (tx, ty),
                                 (tx - dx * h + dy * h, ty - dy * h + dx * h)], col, a)
        else:  # back to full width: a fully filled keyboard outline
            self.rstroke(snap, col, a, cx - q * 1.2, cy - q * 0.8, q * 2.4, q * 1.6, 2, 1.2)
            self.rfill(snap, col, a * 0.8, cx - q * 1.2 + 2, cy - q * 0.8 + 2, q * 2.4 - 4, q * 1.6 - 4, 1.5)

    def alts_x0(self, o):
        g = self.geom
        x, y, w, h = o.key["rect"]
        n = len(o.alts)
        x0 = x + w / 2 - g["kw"] / 2
        return max(g["kx"], min(g["kx"] + g["kbw"] - n * g["kw"], x0))

    def draw_alts(self, snap, o):
        g = self.geom
        x, y, w, h = o.key["rect"]
        x0 = self.alts_x0(o)
        n = len(o.alts)
        top = max(2, y - h)
        self.rfill(snap, SURFACE, 0.97, x0 - 2, top, n * g["kw"] + 4, h - 2, 8)
        self.rstroke(snap, LAV, 0.6, x0 - 2, top, n * g["kw"] + 4, h - 2, 8)
        for i, a in enumerate(o.alts):
            ax = x0 + i * g["kw"]
            if i == o.alt_sel:
                self.rfill(snap, CYAN, 0.25, ax + 2, top + 3, g["kw"] - 4, h - 8, 6)
            self.text(snap, a, ax, top, g["kw"], h - 2, 13, FG)

    def draw_resize(self, snap):
        g = self.geom
        self.rect(snap, SHADE, 0.72, 0, 0, g["W"], g["H"])
        x, y, w, h = g["kx"], g["ky"], g["kbw"], g["bottom"] - g["ky"]
        self.rstroke(snap, CYAN, 0.9, x + 1, y + 1, w - 2, h - 2, 4, 2, [6, 4])
        self.rfill(snap, PINK, 0.95, x + w / 2 - 30, y - 3, 60, 6, 3)
        for hx in (x, x + w):
            self.rfill(snap, PINK, 0.95, hx - 3, y + h / 2 - 30, 6, 60, 3)
        self.text(snap, "drag: move · edges: width · top bar: height", x, y + g["kh"] * 0.6, w, g["kh"], 9, FG, 0.9)
        c = self.conf_o()
        self.text(snap, f"{self.orient()}  w {c['w']:.2f}  h {c['h']:.2f}  lift {c['lift']}",
                  x, y + g["kh"] * 1.5, w, g["kh"], 9, LAV, 0.8)
        for n, l, (bx, by, bw, bh) in self.resize_buttons():
            col = MINT if n == "done" else LAV
            self.rstroke(snap, col, 0.9, bx, by, bw, bh, 8, 1.5)
            self.text(snap, l, bx, by, bw, bh, 10, MINT if n == "done" else FG)


class Canvas(Gtk.Widget):
    def __init__(self, kbd):
        super().__init__()
        self.kbd = kbd

    def do_snapshot(self, snap):
        self.kbd.draw(snap, self.get_width(), self.get_height())

    def do_size_allocate(self, w, h, baseline):
        self.kbd.relayout()



if __name__ == "__main__":
    Keyboard().run([])
