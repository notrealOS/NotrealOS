"""circled: Circle to search for the phone.

Hold volume-down (like voiced's hold volume-up) -> the screen freezes under a dimmed overlay;
circle something with a finger (or tap a line of text) -> Tesseract reads it -> a sheet at the
bottom offers what to do with it: call/SMS a number, open a link, mail an address, show an
address in notarealMaps, copy, Google it, or ask/translate with the PC's Qwen (nrbrain).

Takes over pmic_resin (the volume-down key) with EVIOCGRAB and passes short presses on through
a uinput clone, so volume-down still works. Hold volume-down again to close.
Screenshots stay in $XDG_RUNTIME_DIR (RAM) and only the circled text ever leaves the phone
(to the user's own PC, and only on ASK/TRANSLATE). Off while locked and over Bitwarden.
Log: $XDG_RUNTIME_DIR/circled.log.
"""
import fcntl
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.request

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, GLibUnix, Graphene, Gsk, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.expanduser("~/theme"))
sys.path.insert(0, os.path.expanduser("~/voice"))
import notareal_theme as theme  # noqa: E402
import actions  # noqa: E402   voiced's actions: dial, maps, web_search, phone_number, CLEAN_ENV


def pc(key, default=""):
    """Address of our PC server (see docs/pc-link.md); set in ~/.config/notreal/pc.json."""
    try:
        with open(os.path.expanduser("~/.config/notreal/pc.json")) as f:
            return json.load(f).get(key) or default
    except (OSError, ValueError):
        return default

RUN = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "circle")
SHOT = os.path.join(RUN, "shot.ppm")
CROP = os.path.join(RUN, "crop.png")
PC_BRAIN = "http://%s:8097" % pc("host", "localhost")
HOLD_MS = 450                       # same feel as voiced's volume-up
OCR_LANGS = "eng+swe"
PRIVATE_APPS = ("com.bitwarden.desktop", "Bitwarden", "bitwarden")
TAP_SLOP = 18                       # px of finger travel that still counts as a tap
BACK_EDGE = 0.08                    # back swipe starts in this share of the width from the left edge
BACK_SWIPE = 0.25                   # ...and closes after this much rightward travel
TAP_BAND = 0.035                    # tap = OCR a band this share of the screen height above and below the finger

EV_SYN, EV_KEY, KEY_VOLUMEDOWN = 0, 1, 114
EVENT = struct.Struct("qqHHi")
EVIOCGRAB = 0x40044590
UI_SET_EVBIT, UI_SET_KEYBIT = 0x40045564, 0x40045565
UI_DEV_SETUP, UI_DEV_CREATE = 0x405C5503, 0x5501


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def run_bg(argv):
    subprocess.Popen(argv, start_new_session=True, env=actions.CLEAN_ENV,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def buzz():
    subprocess.Popen(["fbcli", "-E", "button-pressed"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def locked():
    try:
        sid = os.environ.get("XDG_SESSION_ID") or "auto"
        out = subprocess.run(["loginctl", "show-session", sid, "-p", "LockedHint", "--value"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        return out == "yes"
    except Exception:
        return False


def private_app_active():
    for app in PRIVATE_APPS:
        r = subprocess.run(["wlrctl", "toplevel", "find", "app_id:" + app, "state:active"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            return True
    return False


# --- OCR + what's in the text --------------------------------------------------------------

def tell_widget(up):
    """Keep the overview widget + joystick hidden while we're up: typing in ASK takes the keyboard
    from the app, which looks like the overview to the widget, and its OVERLAY joystick covers us."""
    try:
        Gio.bus_get_sync(Gio.BusType.SESSION).call(
            "org.notarealinux.OverviewWidget", "/org/notarealinux/OverviewWidget", "org.gtk.Actions",
            "Activate", GLib.Variant("(sava{sv})", ("circle", [GLib.Variant("b", up)], {})),
            None, Gio.DBusCallFlags.NO_AUTO_START, 1000, None, None, None)
    except GLib.Error as e:
        log("widget:", e.message)


def ocr(pixbuf, psm):
    """Tesseract on a pixbuf -> list of (top, bottom, text) lines in the pixbuf's pixels."""
    scale = 2 if pixbuf.get_height() < 400 else 1      # small crops read much better upscaled
    if scale > 1:
        pixbuf = pixbuf.scale_simple(pixbuf.get_width() * scale, pixbuf.get_height() * scale,
                                     GdkPixbuf.InterpType.BILINEAR)
    pixbuf.savev(CROP, "png", [], [])
    out = subprocess.run(["tesseract", CROP, "stdout", "-l", OCR_LANGS, "--psm", str(psm), "tsv"],
                         capture_output=True, text=True, timeout=60).stdout
    lines = {}
    for row in out.splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or f[0] != "5" or not f[11].strip() or float(f[10]) < 30:
            continue
        key = (int(f[2]), int(f[3]), int(f[4]))           # block, paragraph, line
        top, h = int(f[7]) // scale, int(f[9]) // scale
        ln = lines.setdefault(key, [top, top + h, []])
        ln[0], ln[1] = min(ln[0], top), max(ln[1], top + h)
        ln[2].append(f[11])
    return [(t, b, " ".join(words)) for t, b, words in sorted(lines.values(), key=lambda v: v[0])]


URL = re.compile(r"\b(?:https?://\S+|www\.\S+|[\w-]+(?:\.[\w-]+)*\.(?:com|se|org|net|io|nu|app|dev|eu|co|me)\b(?:/\S*)?)",
                 re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<![\w+])(?:\+\d{1,3}[\s-]?)?\(?0?\d{1,4}\)?(?:[\s-]?\d{2,4}){2,4}(?!\w)")
STREET = re.compile(r"\b[A-ZÅÄÖ][\wåäöéü]*(?:gatan|vägen|väg|gränd|torget|torg|backe|backen|allén|stigen|plan|"
                    r"platsen|kajen|street|st\.?|road|rd\.?|avenue|ave\.?|lane|drive|way|boulevard)"
                    r"\s+\d+[A-Za-z]?(?:,?\s+\d{3}\s?\d{2}\s+[A-ZÅÄÖ][\wåäö]+)?")
STREET2 = re.compile(r"\b\d+[A-Za-z]?\s+[A-Z][\w]+(?:\s[A-Z][\w]+)?\s(?:Street|St|Road|Rd|Avenue|Ave|Lane|Drive|Way)\b\.?")
POSTCODE = re.compile(r"\b\d{3}\s?\d{2}\s+[A-ZÅÄÖ][\wåäö]+")


def found_things(text):
    """[(kind, label, value)] for numbers, links, mails and addresses in the text."""
    things, seen = [], set()

    def add(kind, value, shown=None):
        if (kind, value) not in seen:
            seen.add((kind, value))
            things.append((kind, shown or value, value))

    mails = EMAIL.findall(text)
    for m in mails:
        add("mail", m)
    for u in URL.findall(text):
        u = u.rstrip(".,;:!?)»\"'")
        if not any(u in m for m in mails):
            add("url", u)
    for m in PHONE.finditer(text):
        raw = m.group(0).strip()
        digits = re.sub(r"\D", "", raw)
        if 7 <= len(digits) <= 15 and not re.fullmatch(r"(19|20)\d{6}", digits):   # skip dates like 20261018
            n = actions.phone_number(raw)
            if n:
                add("phone", n, raw)
    for rx in (STREET, STREET2, POSTCODE):
        for m in rx.finditer(text):
            p = m.group(0).strip(" ,")
            if not any(k == "place" and p in v for k, _, v in things):   # postcode inside a full address
                add("place", p)
    return things[:8]


def ask_pc(question, text):
    import datetime
    today = datetime.date.today().strftime("%A %Y-%m-%d")
    prompt = ("The user circled this on their phone screen (OCR text, may have small errors):\n\n%s\n\n%s\n"
              "Answer in plain text, short (max ~80 words), no markdown.\n"
              "Today is %s. You CAN add calendar events: if the user asks for one (or to save a date),"
              " say briefly what you will add, then put each event on its own last line exactly like\n"
              'EVENT {"title": "...", "start": "YYYY-MM-DD or YYYY-MM-DDTHH:MM", "end": "same format",'
              ' "all_day": true, "tentative": false, "location": "", "notes": "..."}\n'
              "Dates without a year are the next one to come. The user confirms before it is saved."
              % (text, question, today))
    body = {"messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 400,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(PC_BRAIN + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:      # first use loads the model (~30 s)
        return json.load(resp)["choices"][0]["message"]["content"].strip()


def split_events(answer):
    """Pull the EVENT {...} lines out of a model answer -> (text without them, [event dicts])."""
    events, keep = [], []
    for line in answer.splitlines():
        m = re.match(r"\s*EVENT\s*(\{.*\})\s*$", line)
        if m:
            try:
                ev = json.loads(m.group(1))
                if ev.get("start"):
                    events.append(ev)
                    continue
            except ValueError:
                pass
        keep.append(line)
    return "\n".join(keep).strip(), events


def event_label(ev):
    import datetime

    def d(s):
        try:
            t = datetime.datetime.fromisoformat(s)
        except (TypeError, ValueError):
            return s or ""
        return t.strftime("%-d %b" if len(s) == 10 else "%-d %b %H:%M")
    when = d(ev.get("start"))
    if ev.get("end") and ev["end"] != ev["start"]:
        when += "–" + d(ev["end"])
    title = ev.get("title") or "Event"
    title = title if len(title) <= 24 else title[:22] + "…"
    return "＋ CALENDAR · %s · %s%s" % (title, when, " (tentative)" if ev.get("tentative") else "")


# --- the frozen screen you draw on ---------------------------------------------------------

class Canvas(Gtk.Widget):
    __gtype_name__ = "CircleCanvas"

    def __init__(self, on_select):
        super().__init__()
        self.on_select = on_select
        self.texture = None
        self.points = []
        self.sel = None          # (x, y, w, h) in widget px
        self.start = None
        self.typing = False      # keyboard up: the window shrinks above it, so no screenshot then
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self.begin)
        drag.connect("drag-update", self.update)
        drag.connect("drag-end", self.end)
        self.add_controller(drag)

    def show_shot(self, texture):
        self.texture, self.points, self.sel = texture, [], None
        self.queue_draw()

    def begin(self, g, x, y):
        if self.typing:
            self.start = None
            return
        self.start = (x, y)
        self.points = [(x, y)]
        self.sel = None
        self.queue_draw()

    def update(self, g, dx, dy):
        if self.start:
            self.points.append((self.start[0] + dx, self.start[1] + dy))
            self.queue_draw()

    def end(self, g, dx, dy):
        if not self.start:
            return
        w, h = self.get_width(), self.get_height()
        xs, ys = [p[0] for p in self.points], [p[1] for p in self.points]
        if max(xs) - min(xs) < TAP_SLOP and max(ys) - min(ys) < TAP_SLOP:
            x, y = self.start
            band = h * TAP_BAND
            self.sel = (0, max(0, y - band), w, min(h, y + band) - max(0, y - band))
            self.points = []
            tap_y = y - self.sel[1]
        else:
            pad = 10
            x0, y0 = max(0, min(xs) - pad), max(0, min(ys) - pad)
            x1, y1 = min(w, max(xs) + pad), min(h, max(ys) + pad)
            self.sel = (x0, y0, x1 - x0, y1 - y0)
            tap_y = None
        self.start = None
        self.queue_draw()
        self.on_select(self.sel, tap_y)

    def do_snapshot(self, s):
        w, h = self.get_width(), self.get_height()
        full = Graphene.Rect().init(0, 0, w, h)
        if self.typing:
            s.append_color(theme.color("bg-deep", 0.92), full)
            return
        if self.texture:
            s.append_texture(self.texture, full)
        dim = theme.color("bg-deep", 0.55)
        if self.sel:
            x, y, sw, sh = self.sel
            for r in ((0, 0, w, y), (0, y + sh, w, h - y - sh), (0, y, x, sh), (x + sw, y, w - x - sw, sh)):
                if r[2] > 0 and r[3] > 0:
                    s.append_color(dim, Graphene.Rect().init(*r))
            box = Gsk.RoundedRect()
            box.init_from_rect(Graphene.Rect().init(x, y, sw, sh), 12)
            s.append_border(box, [3] * 4, [theme.color("accent")] * 4)
        else:
            s.append_color(theme.color("bg-deep", 0.30), full)
        if len(self.points) > 1:
            pb = Gsk.PathBuilder.new()
            pb.move_to(*self.points[0])
            for p in self.points[1:]:
                pb.line_to(*p)
            stroke = Gsk.Stroke.new(7)
            stroke.set_line_cap(Gsk.LineCap.ROUND)
            stroke.set_line_join(Gsk.LineJoin.ROUND)
            s.append_stroke(pb.to_path(), stroke, theme.color("accent", 0.85))


# --- the overlay window --------------------------------------------------------------------

class Circle:
    def __init__(self, app):
        self.app = app
        self.pixbuf = None
        self.widget_off = 0      # pending "circle closed" to the overview widget
        self.text = ""
        self.gen = 0             # bumps on every new selection; late OCR/ASK results are dropped
        win = self.win = Gtk.Window(application=app, title="Circle")
        win.add_css_class("circle-win")
        LayerShell.init_for_window(win)
        LayerShell.set_namespace(win, "circle")
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)   # EXCLUSIVE only while typing
        LayerShell.set_exclusive_zone(win, -1)
        for e in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM, LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(win, e, True)

        over = Gtk.Overlay()
        self.canvas = Canvas(self.selected)
        over.set_child(self.canvas)

        # top hint bar: close on the left (left-handed)
        top = Gtk.Box(spacing=8, valign=Gtk.Align.START, halign=Gtk.Align.START, margin_top=44, margin_start=12)
        top.add_css_class("circle-top")
        close = Gtk.Button(icon_name="window-close-symbolic")
        close.add_css_class("circle-close")
        close.connect("clicked", lambda *_: self.hide())
        top.append(close)
        self.hint = Gtk.Label(label="CIRCLE ANYTHING · TAP A LINE")
        self.hint.add_css_class("circle-hint")
        top.append(self.hint)
        over.add_overlay(top)

        # bottom sheet
        sheet = self.sheet = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.END,
                                     margin_start=8, margin_end=8, margin_bottom=8, visible=False)
        sheet.add_css_class("circle-sheet")
        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("circle-status")
        sheet.append(self.status)
        self.tv = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=6, bottom_margin=6,
                               left_margin=8, right_margin=8)
        self.tv.add_css_class("circle-text")
        self.tv.get_buffer().connect("changed", self.text_edited)
        sc = Gtk.ScrolledWindow(child=self.tv, propagate_natural_height=True, max_content_height=170,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        sheet.append(sc)
        self.chips = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6, row_spacing=6,
                                 max_children_per_line=3, homogeneous=False, halign=Gtk.Align.START)
        sheet.append(self.chips)
        ask = Gtk.Box(spacing=6)
        self.ask_btn = Gtk.Button(label="ASK")
        self.ask_btn.add_css_class("circle-ask")
        self.ask_btn.connect("clicked", lambda *_: self.ask())
        ask.append(self.ask_btn)
        self.entry = Gtk.Entry(placeholder_text="ask about it… (empty = explain)", hexpand=True)
        self.entry.connect("activate", lambda *_: self.ask())
        tap = Gtk.GestureClick()
        tap.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        tap.connect("pressed", lambda *_: self.typing(True))
        self.entry.add_controller(tap)
        ask.append(self.entry)
        sheet.append(ask)
        self.answer = Gtk.Label(xalign=0, wrap=True, selectable=True, visible=False)
        self.answer.add_css_class("circle-answer")
        self.answer_sc = Gtk.ScrolledWindow(child=self.answer, propagate_natural_height=True,
                                            max_content_height=220, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                            visible=False)
        sheet.append(self.answer_sc)
        self.acts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.START)
        sheet.append(self.acts)     # ASK's suggested actions (calendar events), tap = do it
        over.add_overlay(sheet)

        win.set_child(over)
        # universal back: a left-to-right swipe from the left edge closes, in case ✕ is out of reach
        back = Gtk.GestureDrag()
        back.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        back.connect("drag-begin", self.back_begin)
        back.connect("drag-update", self.back_update)
        win.add_controller(back)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda c, k, *_: (self.hide(), True)[1] if k == Gdk.KEY_Escape else False)
        win.add_controller(keys)

    def typing(self, on):
        """Phoc has layer-shell v3 (no ON_DEMAND keyboard) and the swipe OSK lives on TOP: while
        typing, drop to TOP with an exclusive keyboard and respect the OSK's zone, so the sheet sits
        right above the keyboard. Back to a full-screen OVERLAY without keyboard afterwards."""
        if on == self.canvas.typing:
            return
        self.canvas.typing = on
        LayerShell.set_layer(self.win, LayerShell.Layer.TOP if on else LayerShell.Layer.OVERLAY)
        LayerShell.set_exclusive_zone(self.win, 0 if on else -1)
        LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.EXCLUSIVE if on
                                     else LayerShell.KeyboardMode.NONE)
        self.canvas.queue_draw()
        if on:
            GLib.timeout_add(120, lambda: (self.entry.grab_focus(), self.entry.set_position(-1), False)[2])
        else:
            self.win.set_focus(None)

    def back_begin(self, g, x, y):
        if x > BACK_EDGE * max(1, self.win.get_width()):
            g.set_state(Gtk.EventSequenceState.DENIED)   # not from the edge: circling/typing as usual

    def back_update(self, g, dx, dy):
        if dx > BACK_SWIPE * max(1, self.win.get_width()) and abs(dy) < dx * 0.7:
            g.set_state(Gtk.EventSequenceState.CLAIMED)
            g.reset()
            log("back swipe")
            self.hide()

    @property
    def shown(self):
        return self.win.get_visible()

    def show(self):
        try:
            os.makedirs(RUN, exist_ok=True)
            subprocess.run(["grim", "-t", "ppm", SHOT], check=True, timeout=5)
            self.pixbuf = GdkPixbuf.Pixbuf.new_from_file(SHOT)
            os.unlink(SHOT)
        except Exception as e:
            log("screenshot failed:", e)
            return
        self.gen += 1
        # own copy of the pixels: new_for_pixbuf segfaulted in GDK's threaded conversion (10-18)
        pb = self.pixbuf
        fmt = Gdk.MemoryFormat.R8G8B8A8 if pb.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
        self.canvas.show_shot(Gdk.MemoryTexture.new(pb.get_width(), pb.get_height(), fmt,
                                                    GLib.Bytes.new(pb.get_pixels()), pb.get_rowstride()))
        self.sheet.set_visible(False)
        self.hint.set_label("CIRCLE ANYTHING · TAP A LINE")
        if self.widget_off:
            GLib.source_remove(self.widget_off)
            self.widget_off = 0
        tell_widget(True)
        self.win.present()
        log("shown")

    def hide(self):
        self.typing(False)
        self.gen += 1
        self.win.set_visible(False)
        if not self.widget_off:   # let the app get its focus back first, or the widget flashes up
            self.widget_off = GLib.timeout_add(600, lambda: (setattr(self, "widget_off", 0), tell_widget(False), False)[2])
        self.pixbuf = None
        self.canvas.show_shot(None)
        for p in (CROP,):
            try:
                os.unlink(p)
            except OSError:
                pass

    # selection -> OCR
    def selected(self, sel, tap_y):
        if not self.pixbuf:
            return
        sx = self.pixbuf.get_width() / max(1, self.canvas.get_width())
        sy = self.pixbuf.get_height() / max(1, self.canvas.get_height())
        x, y = int(sel[0] * sx), int(sel[1] * sy)
        w = max(8, min(int(sel[2] * sx), self.pixbuf.get_width() - x))
        h = max(8, min(int(sel[3] * sy), self.pixbuf.get_height() - y))
        crop = self.pixbuf.new_subpixbuf(x, y, w, h).copy()
        self.gen += 1
        gen = self.gen
        self.hint.set_label("READING…")
        self.set_text("")
        self.status.set_label("READING…")
        self.answer_sc.set_visible(False)
        self.clear_acts()
        self.sheet.set_visible(True)
        self.clear_chips()
        ty = None if tap_y is None else tap_y * sy

        def work():
            t0 = time.time()
            try:
                lines = ocr(crop, 6 if tap_y is not None or h < 500 else 3)
                if ty is not None and lines:   # tap: just the line under the finger
                    lines = [min(lines, key=lambda ln: 0 if ln[0] <= ty <= ln[1] else
                                 min(abs(ln[0] - ty), abs(ln[1] - ty)))]
                text = "\n".join(ln[2] for ln in lines)
                err = None
            except FileNotFoundError:
                text, err = "", "Tesseract isn't installed: sudo apk add tesseract-ocr tesseract-ocr-data-eng tesseract-ocr-data-swe"
            except Exception as e:
                text, err = "", "OCR failed: %s" % e
            log("ocr %.1fs %dx%d -> %d chars" % (time.time() - t0, w, h, len(text)))
            GLib.idle_add(self.read_done, gen, text, err)

        threading.Thread(target=work, daemon=True).start()

    def read_done(self, gen, text, err):
        if gen != self.gen:
            return False
        self.hint.set_label("CIRCLE AGAIN TO CHANGE")
        if err:
            self.status.set_label(err)
        elif not text.strip():
            self.status.set_label("NO TEXT FOUND · TRY A BIGGER CIRCLE")
        else:
            self.status.set_label("TEXT · EDIT IF NEEDED")
        self.set_text(text)
        return False

    def set_text(self, text):
        self.tv.get_buffer().set_text(text)

    def text_edited(self, buf):
        self.text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        self.build_chips()

    # actions
    def clear_chips(self):
        while (c := self.chips.get_first_child()):
            self.chips.remove(c)

    def chip(self, label, cls, fn):
        b = Gtk.Button(label=label)
        b.add_css_class("circle-chip")
        b.add_css_class(cls)
        b.connect("clicked", lambda *_: fn())
        self.chips.append(b)

    def build_chips(self):
        self.clear_chips()
        t = self.text
        if not t:
            return
        short = lambda s: s if len(s) <= 22 else s[:20] + "…"  # noqa: E731
        for kind, shown, value in found_things(t):
            if kind == "phone":
                self.chip("CALL " + short(shown), "phone", lambda v=value: self.go(actions.dial, v))
                self.chip("SMS", "phone", lambda v=value: self.go(run_bg, ["python3", os.path.expanduser("~/chat/chat.py"),
                                                                           "--sms", v]))
            elif kind == "url":
                url = value if re.match(r"https?://", value, re.I) else "https://" + value
                self.chip("OPEN " + short(shown), "url", lambda v=url: self.go(run_bg, ["xdg-open", v]))
            elif kind == "mail":
                self.chip("MAIL " + short(shown), "mail", lambda v=value: self.go(run_bg, ["gio", "open", "mailto:" + v]))
            elif kind == "place":
                self.chip("MAP " + short(shown), "place", lambda v=value: self.go(actions.maps, "--search", v))
        self.chip("COPY", "plain", self.copy)
        self.chip("SEARCH", "plain", lambda: self.go(actions.web_search, " ".join(t.split())[:300]))
        self.chip("TRANSLATE", "plain",
                  lambda: self.ask("Translate it to English (if it already is English, to Swedish). "
                                   "Give only the translation."))
        if not any(k == "place" for k, _, _ in found_things(t)) and len(t) < 80:
            self.chip("MAP", "place", lambda: self.go(actions.maps, "--search", " ".join(t.split())))

    def go(self, fn, *args):
        log("action", fn.__name__, args[:1])
        self.hide()
        fn(*args)

    def clear_acts(self):
        while (c := self.acts.get_first_child()):
            self.acts.remove(c)

    def add_event(self, btn, ev):
        btn.set_sensitive(False)
        btn.set_label("ADDING…")
        log("add event", ev.get("title"), ev.get("start"), ev.get("end"))

        def work():
            try:
                r = subprocess.run(["python3", os.path.join(HERE, "addevent.py")], input=json.dumps(ev),
                                   capture_output=True, text=True, timeout=60)
                ok, msg = r.returncode == 0, (r.stdout.strip() or r.stderr.strip().splitlines()[-1:] or ["?"])
                msg = msg if isinstance(msg, str) else msg[0]
            except Exception as e:
                ok, msg = False, str(e)
            GLib.idle_add(done, ok, msg)

        def done(ok, msg):
            log("add event:", ok, msg)
            btn.set_label(("✓ ADDED TO " + msg.upper()) if ok else "COULDN'T ADD: " + msg[:40])
            if ok:
                buzz()
            return False

        threading.Thread(target=work, daemon=True).start()

    def copy(self):
        try:
            p = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE, start_new_session=True, env=actions.CLEAN_ENV)
            p.communicate(self.text.encode(), timeout=3)
        except Exception:
            self.win.get_clipboard().set(self.text)
        self.status.set_label("COPIED")
        buzz()

    def ask(self, question=None):
        t = self.text
        if not t:
            return
        q = question or self.entry.get_text().strip() or "What is this? Explain briefly."
        gen = self.gen
        self.clear_acts()
        self.answer.set_label("THINKING… (first question can take ~30 s)")
        self.answer.set_visible(True)
        self.answer_sc.set_visible(True)
        self.ask_btn.set_sensitive(False)
        self.typing(False)

        def work():
            try:
                a = ask_pc(q, t)
            except Exception as e:
                log("ask failed:", e)
                a = "The PC brain didn't answer (is the PC on?)."
            GLib.idle_add(done, a)

        def done(a):
            self.ask_btn.set_sensitive(True)
            if gen == self.gen:
                a, events = split_events(a)
                self.answer.set_label(a or "Here is what I found:")
                self.clear_acts()
                for ev in events:
                    b = Gtk.Button(label=event_label(ev))
                    b.add_css_class("circle-chip")
                    b.add_css_class("plain")
                    b.connect("clicked", lambda btn, e=ev: self.add_event(btn, e))
                    self.acts.append(b)
            return False

        threading.Thread(target=work, daemon=True).start()


# --- volume-down key -----------------------------------------------------------------------

class Keys:
    def __init__(self, on_hold):
        self.on_hold = on_hold
        self.hold_timer = 0
        fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(fd, UI_SET_KEYBIT, KEY_VOLUMEDOWN)
        fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack("HHHH80sI", 0x19, 0x1, 0x1, 1, b"circled volume down", 0))
        fcntl.ioctl(fd, UI_DEV_CREATE)
        self.ui_fd = fd
        for d in sorted(os.listdir("/sys/class/input")):
            try:
                with open("/sys/class/input/%s/device/name" % d) as f:
                    name = f.read().strip()
            except OSError:
                continue
            if d.startswith("event") and name == "pmic_resin":
                self.key_fd = os.open("/dev/input/" + d, os.O_RDONLY | os.O_NONBLOCK)
                time.sleep(0.3)  # let libinput pick up the uinput clone first
                fcntl.ioctl(self.key_fd, EVIOCGRAB, 1)
                GLibUnix.fd_add_full(GLib.PRIORITY_HIGH, self.key_fd, GLib.IOCondition.IN, self.on_key_fd)
                log("volume down grabbed:", d)
                return
        raise RuntimeError("pmic_resin not found")

    def emit(self, value):
        os.write(self.ui_fd, EVENT.pack(0, 0, EV_KEY, KEY_VOLUMEDOWN, value) + EVENT.pack(0, 0, EV_SYN, 0, 0))

    def on_key_fd(self, fd, cond):
        try:
            data = os.read(fd, EVENT.size * 32)
        except BlockingIOError:
            return GLib.SOURCE_CONTINUE
        for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
            _, _, typ, code, val = EVENT.unpack_from(data, i)
            if typ == EV_KEY and code == KEY_VOLUMEDOWN:
                if val == 1:
                    if self.hold_timer:
                        GLib.source_remove(self.hold_timer)
                    self.hold_timer = GLib.timeout_add(HOLD_MS, self.long_press)
                elif val == 0 and self.hold_timer:   # short press: normal volume down
                    GLib.source_remove(self.hold_timer)
                    self.hold_timer = 0
                    self.emit(1)
                    self.emit(0)
        return GLib.SOURCE_CONTINUE

    def long_press(self):
        self.hold_timer = 0
        self.on_hold()
        return False


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.notareal.Circle")
        self.circle = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        theme.apply(os.path.join(HERE, "style.css"))
        self.circle = Circle(self)
        self.keys = Keys(self.toggle)
        self.hold()
        os.makedirs(RUN, exist_ok=True)
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, 10, self.toggle)   # SIGUSR1 = same as holding volume-down
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, 12, lambda: (self.circle.shown and self.circle.typing(True), True)[1])   # SIGUSR2 = tap ASK (test)

    def do_activate(self):
        pass

    def toggle(self, *_):
        if self.circle.shown:
            self.circle.hide()
        elif locked():
            log("locked: ignored")
        elif private_app_active():
            log("private app active: ignored")
            subprocess.Popen(["notify-send", "-a", "Circle", "-t", "2000", "Circle is off in Bitwarden"])
        else:
            buzz()
            self.circle.show()
        return GLib.SOURCE_CONTINUE


if __name__ == "__main__":
    App().run([sys.argv[0]])
