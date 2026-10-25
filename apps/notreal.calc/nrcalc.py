#!/usr/bin/env python3
"""notreal.calc: a phone calculator in the shared notareal theme (10-19).

Left-handed: the operator column and = sit on the LEFT, the fx (scientific) toggle and the
history clear button in the left of the header. The answer is previewed live while typing;
= moves it up into the history strip (tap an old line to reuse its answer, long-press the
display to copy). Typed percent works like a phone: 200 + 10% = 220, 50 × 10% = 5.
Trig is in degrees unless the DEG key is switched to RAD.
"""

import json
import math
import os
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

import notareal_theme  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP_ID = "dev.notareal.Calc"
DATA = os.path.expanduser("~/.local/share/nrcalc")
HIST = os.path.join(DATA, "history.json")
HIST_MAX = 50

FUNCS = {"sin": "sin(", "cos": "cos(", "tan": "tan(", "ln": "ln(", "log": "log(", "√": "√("}
CONSTS = {"π": math.pi, "e": math.e}


# --- evaluation ------------------------------------------------------------------------------

class CalcError(Exception):
    pass


def tokenize(s):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c.isspace():
            i += 1
        elif c.isdigit() or c == ".":
            j = i
            while j < len(s) and (s[j].isdigit() or s[j] == "."):
                j += 1
            if s[i:j].count(".") > 1:
                raise CalcError("number")
            out.append(("num", float(s[i:j]) if s[i:j] != "." else 0.0))
            i = j
        elif s.startswith(("sin", "cos", "tan", "log"), i):
            out.append(("fn", s[i:i + 3]))
            i += 3
        elif s.startswith("ln", i):
            out.append(("fn", "ln"))
            i += 2
        elif c == "√":
            out.append(("fn", "√"))
            i += 1
        elif c in CONSTS:
            out.append(("num", CONSTS[c]))
            i += 1
        elif c in "+-−×*÷/^%!()":
            out.append(("op", {"−": "-", "*": "×", "/": "÷"}.get(c, c)))
            i += 1
        else:
            raise CalcError("char")
    return out


class Parser:
    def __init__(self, tokens, degrees):
        self.t, self.i, self.deg = tokens, 0, degrees

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def parse(self):
        v, _pct = self.expr()
        if self.i != len(self.t):
            raise CalcError("trailing")
        return v

    def expr(self):
        v, pct = self.term()
        while self.peek() in (("op", "+"), ("op", "-")):
            op = self.take()[1]
            r, rpct = self.term()
            if rpct:                     # 200 + 10% -> 200 + 20
                r = v * r
            v, pct = (v + r if op == "+" else v - r), False
        return v, pct

    def starts_primary(self):
        k, x = self.peek()
        return k in ("num", "fn") or (k, x) == ("op", "(")

    def term(self):
        v, pct = self.unary()
        while True:
            k, x = self.peek()
            if (k, x) in (("op", "×"), ("op", "÷")):
                self.take()
                r, _ = self.unary()
                if x == "÷":
                    if r == 0:
                        raise CalcError("div0")
                    v = v / r
                else:
                    v = v * r
            elif self.starts_primary():   # implicit: 2π, 3(4), 2sin(30)
                r, _ = self.unary()
                v = v * r
            else:
                return v, pct
            pct = False

    def unary(self):
        if self.peek() == ("op", "-"):
            self.take()
            v, pct = self.unary()
            return -v, pct
        if self.peek() == ("op", "+"):
            self.take()
        return self.power()

    def power(self):
        v, pct = self.postfix()
        if self.peek() == ("op", "^"):
            self.take()
            e, _ = self.unary()
            try:
                v = math.pow(v, e)
            except (OverflowError, ValueError):
                raise CalcError("pow")
            pct = False
        return v, pct

    def postfix(self):
        v = self.primary()
        pct = False
        while self.peek() in (("op", "%"), ("op", "!")):
            if self.take()[1] == "%":
                v, pct = v / 100, True
            else:
                if v < 0 or v != int(v) or v > 170:
                    raise CalcError("fact")
                v, pct = float(math.factorial(int(v))), False
        return v, pct

    def primary(self):
        k, x = self.take()
        if k == "num":
            return x
        if (k, x) == ("op", "("):
            v, _ = self.expr()
            if self.take() != ("op", ")"):
                raise CalcError("paren")
            return v
        if k == "fn":
            a, _ = self.power() if self.peek() != ("op", "(") else (self.primary(), False)
            return self.apply(x, a)
        raise CalcError("syntax")

    def apply(self, fn, a):
        try:
            if fn in ("sin", "cos", "tan"):
                r = math.radians(a) if self.deg else a
                if fn == "tan" and self.deg and abs(math.fmod(a, 180)) == 90:
                    raise CalcError("tan")
                v = getattr(math, fn)(r)
                return 0.0 if abs(v) < 1e-12 else v
            if fn == "ln":
                return math.log(a)
            if fn == "log":
                return math.log10(a)
            return math.sqrt(a)
        except ValueError:
            raise CalcError("domain")


def evaluate(text, degrees=True, close=False):
    if close:                            # live preview: finish open brackets
        text = text + ")" * max(0, text.count("(") - text.count(")"))
        text = text.rstrip("+-−×÷^")
    toks = tokenize(text)
    if not toks:
        raise CalcError("empty")
    v = Parser(toks, degrees).parse()
    if math.isnan(v) or math.isinf(v):
        raise CalcError("range")
    return v


def fmt(v):
    if v == 0:
        return "0"
    if abs(v) >= 1e15 or abs(v) < 1e-9:
        m, e = ("%.9e" % v).split("e")
        return "%s×10^%d" % (m.rstrip("0").rstrip("."), int(e))
    s = "%.12g" % v if abs(v) < 1e12 else "%.0f" % v
    if "e" in s:
        s = ("%.12f" % v).rstrip("0").rstrip(".")
    neg = s.startswith("-")
    s = s.lstrip("-")
    whole, _, frac = s.partition(".")
    whole = "{:,}".format(int(whole)).replace(",", " ")
    return ("−" if neg else "") + whole + ("." + frac if frac else "")


def plain(v):
    """A result as typeable input (no grouping, ASCII minus, no ×10^)."""
    s = repr(round(v, 12)) if abs(v) < 1e15 else "%.12g" % v
    if "e" in s:
        m, e = s.split("e")
        s = "%s×10^%d" % (m, int(e))
    s = s[:-2] if s.endswith(".0") else s
    return s.replace("-", "−")


# --- UI --------------------------------------------------------------------------------------

def key(text, css, cb, *extra):
    b = Gtk.Button(label=text, hexpand=True, vexpand=True)
    b.add_css_class("ck")
    for c in (css,) + extra:
        if c:
            b.add_css_class(c)
    b.connect("clicked", lambda *_: cb())
    return b


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Calculator", default_width=360, default_height=760)
        self.add_css_class("nrcalc")
        self.text = ""
        self.just_done = False           # last key was =: a digit starts over, an operator continues
        self.degrees = True
        self.history = self.load_history()

        hb = Adw.HeaderBar(show_end_title_buttons=False)
        hb.add_css_class("vapor-bar")
        t = Gtk.Label(label="NOTREAL.CALC")
        t.add_css_class("app-title")
        hb.set_title_widget(t)
        self.fx_btn = Gtk.ToggleButton(label="fx", tooltip_text="Scientific keys")
        self.fx_btn.add_css_class("flat")
        self.fx_btn.add_css_class("fx-toggle")
        hb.pack_start(self.fx_btn)
        clear_h = Gtk.Button(icon_name="edit-clear-all-symbolic", tooltip_text="Clear history")
        clear_h.add_css_class("flat")
        clear_h.connect("clicked", lambda *_: self.clear_history())
        hb.pack_start(clear_h)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # history strip: old lines, newest at the bottom, tap = reuse the answer
        self.hist_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.END)
        self.hist_scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                              child=self.hist_box)
        self.hist_scroll.add_css_class("hist")
        root.append(self.hist_scroll)

        disp = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        disp.add_css_class("display")
        self.expr_lbl = Gtk.Label(xalign=1.0, wrap=True, wrap_mode=2, selectable=False)
        self.expr_lbl.add_css_class("expr")
        self.res_lbl = Gtk.Label(xalign=1.0, ellipsize=1)
        self.res_lbl.add_css_class("res")
        disp.append(self.expr_lbl)
        disp.append(self.res_lbl)
        lp = Gtk.GestureLongPress()
        lp.connect("pressed", lambda *_: self.copy())
        disp.add_controller(lp)
        swipe = Gtk.GestureSwipe()           # swipe the display sideways = backspace
        swipe.connect("swipe", lambda g, vx, vy: self.back() if abs(vx) > abs(vy) and abs(vx) > 300 else None)
        disp.add_controller(swipe)
        root.append(disp)

        self.fx_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        fx = Gtk.Grid(column_homogeneous=True, row_homogeneous=True, column_spacing=6, row_spacing=6)
        fx.add_css_class("fx-pad")
        self.deg_btn = key("DEG", "fx", self.toggle_deg)
        fx_keys = [("sin", lambda: self.put("sin(")), ("cos", lambda: self.put("cos(")),
                   ("tan", lambda: self.put("tan(")), ("π", lambda: self.put("π")), ("e", lambda: self.put("e")),
                   ("ln", lambda: self.put("ln(")), ("log", lambda: self.put("log(")),
                   ("√", lambda: self.put("√(")), ("xʸ", lambda: self.put("^")), ("x²", lambda: self.put("^2"))]
        fx.attach(self.deg_btn, 0, 0, 1, 1)
        fx.attach(key("x!", "fx", lambda: self.put("!")), 0, 1, 1, 1)
        for n, (label, cb) in enumerate(fx_keys):
            fx.attach(key(label, "fx", cb), 1 + n % 5, n // 5, 1, 1)
        self.fx_rev.set_child(fx)
        self.fx_btn.bind_property("active", self.fx_rev, "reveal-child", 0)
        root.append(self.fx_rev)

        pad = Gtk.Grid(column_homogeneous=True, row_homogeneous=True, column_spacing=8, row_spacing=8)
        pad.add_css_class("pad")
        ops = [("÷", "÷"), ("×", "×"), ("−", "−"), ("+", "+")]
        for r, (label, op) in enumerate(ops):
            pad.attach(key(label, "op", lambda op=op: self.put(op)), 0, r, 1, 1)
        pad.attach(key("=", "eq", self.equals, "suggested-action"), 0, 4, 1, 1)
        pad.attach(key("C", "fn", self.clear), 1, 0, 1, 1)
        pad.attach(key("( )", "fn", self.paren), 2, 0, 1, 1)
        pad.attach(key("%", "fn", lambda: self.put("%")), 3, 0, 1, 1)
        for n, d in enumerate("789456123"):
            pad.attach(key(d, "num", lambda d=d: self.put(d)), 1 + n % 3, 1 + n // 3, 1, 1)
        pad.attach(key("0", "num", lambda: self.put("0")), 1, 4, 1, 1)
        pad.attach(key(".", "num", lambda: self.put(".")), 2, 4, 1, 1)
        bk = Gtk.Button(icon_name="edit-clear-symbolic", hexpand=True, vexpand=True, tooltip_text="Delete")
        bk.add_css_class("ck")
        bk.add_css_class("fn")
        bk.connect("clicked", lambda *_: self.back())
        hold = Gtk.GestureLongPress()        # hold ⌫ = clear
        hold.connect("pressed", lambda *_: self.clear())
        bk.add_controller(hold)
        pad.attach(bk, 3, 4, 1, 1)
        root.append(pad)

        self.toasts = Adw.ToastOverlay(child=root)
        tv = Adw.ToolbarView()
        tv.add_top_bar(hb)
        tv.set_content(self.toasts)
        self.set_content(tv)

        kc = Gtk.EventControllerKey()
        kc.connect("key-pressed", self.on_key)
        self.add_controller(kc)

        notareal_theme.on_change(self.on_theme)
        self.on_theme()
        self.render_history()
        self.update()

    # --- theme ---
    def on_theme(self, *_):
        (self.add_css_class if notareal_theme.value("corners", "1") == "0" else self.remove_css_class)("square")

    # --- input ---
    def put(self, s):
        if self.just_done:
            self.just_done = False
            if s[0].isdigit() or s[0] in ".(πe" or s in FUNCS.values():
                self.text = ""
        t = self.text
        if s in "÷×−+^" and len(s) == 1:
            if not t:
                if s != "−":
                    return               # nothing to operate on (a leading minus is fine)
            elif t[-1] in "÷×−+^":
                if not (s == "−" and t[-1] in "÷×^"):   # 5 × −2 is fine; else replace the op
                    t = t[:-1]
                    if not t and s != "−":
                        return
        if s == ".":
            i = len(t)
            while i and (t[i - 1].isdigit() or t[i - 1] == "."):
                i -= 1
            if "." in t[i:]:
                return               # this number already has its point
            if not t or not t[-1].isdigit():
                s = "0."
        self.text = t + s
        self.update()

    def paren(self):
        t = self.text
        if self.just_done:
            self.just_done = False
        opened = t.count("(") - t.count(")")
        if opened > 0 and t and (t[-1].isdigit() or t[-1] in ")%!πe."):
            self.put(")")
        else:
            self.put("(")

    def back(self):
        self.just_done = False
        t = self.text
        for f in ("sin(", "cos(", "tan(", "log(", "ln(", "√("):
            if t.endswith(f):
                self.text = t[:-len(f)]
                break
        else:
            self.text = t[:-1]
        self.update()

    def clear(self):
        self.text = ""
        self.just_done = False
        self.update()

    def toggle_deg(self):
        self.degrees = not self.degrees
        self.deg_btn.set_label("DEG" if self.degrees else "RAD")
        self.update()

    def equals(self):
        if not self.text:
            return
        try:
            v = evaluate(self.text, self.degrees, close=True)
        except (CalcError, OverflowError, RecursionError):
            self.res_lbl.set_label("error")
            self.res_lbl.add_css_class("err")
            return
        self.history.append({"expr": self.text, "res": plain(v)})
        del self.history[:-HIST_MAX]
        self.save_history()
        self.render_history()
        self.text = plain(v)
        self.just_done = True
        self.update(done=True)

    def on_key(self, _c, keyval, _code, state):
        name = Gdk.keyval_name(keyval) or ""
        ch = chr(Gdk.keyval_to_unicode(keyval)) if Gdk.keyval_to_unicode(keyval) else ""
        if name in ("Return", "KP_Enter") or ch == "=":
            self.equals()
        elif name == "BackSpace":
            self.back()
        elif name in ("Escape", "Delete"):
            self.clear()
        elif ch and (ch.isdigit() or ch in ".()%^!"):
            self.put(ch)
        elif ch in ("+", "-", "*", "/", "x"):
            self.put({"-": "−", "*": "×", "x": "×", "/": "÷"}.get(ch, ch))
        elif ch == ",":
            self.put(".")
        else:
            return False
        return True

    # --- display ---
    def update(self, done=False):
        t = self.text
        self.expr_lbl.set_label(t or "0")
        n = len(t)
        for c in ("long", "longer"):
            self.expr_lbl.remove_css_class(c)
        if n > 22:
            self.expr_lbl.add_css_class("longer")
        elif n > 12:
            self.expr_lbl.add_css_class("long")
        self.res_lbl.remove_css_class("err")
        if done or not t:
            self.res_lbl.set_label("")
            return
        try:
            v = evaluate(t, self.degrees, close=True)
            # a lone number already shows itself
            self.res_lbl.set_label("" if t.replace("−", "-").lstrip("-").replace(".", "").isdigit()
                                   else "= " + fmt(v))
        except (CalcError, OverflowError, RecursionError, ValueError):
            self.res_lbl.set_label("")

    def copy(self):
        val = self.text
        try:
            val = fmt(evaluate(self.text, self.degrees, close=True)).replace(" ", "").replace("−", "-")
        except (CalcError, OverflowError, RecursionError, ValueError):
            pass
        if not val:
            return
        Gdk.Display.get_default().get_clipboard().set(val)
        self.toasts.add_toast(Adw.Toast(title="copied " + val, timeout=2))

    # --- history ---
    def load_history(self):
        try:
            with open(HIST) as f:
                h = json.load(f)
            return [x for x in h if isinstance(x, dict) and "expr" in x and "res" in x][-HIST_MAX:]
        except (OSError, ValueError):
            return []

    def save_history(self):
        os.makedirs(DATA, exist_ok=True)
        tmp = HIST + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.history, f)
        os.replace(tmp, HIST)

    def clear_history(self):
        self.history = []
        self.save_history()
        self.render_history()

    def render_history(self):
        while (c := self.hist_box.get_first_child()):
            self.hist_box.remove(c)
        for h in self.history:
            b = Gtk.Button()
            b.add_css_class("flat")
            b.add_css_class("hist-row")
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            e = Gtk.Label(label=h["expr"], xalign=1.0, ellipsize=1)
            e.add_css_class("hist-expr")
            r = Gtk.Label(label="= " + h["res"], xalign=1.0, ellipsize=1)
            r.add_css_class("hist-res")
            col.append(e)
            col.append(r)
            b.set_child(col)
            b.connect("clicked", lambda *_, res=h["res"]: self.reuse(res))
            self.hist_box.append(b)
        GLib.idle_add(self.scroll_bottom)

    def scroll_bottom(self):
        adj = self.hist_scroll.get_vadjustment()
        adj.set_value(adj.get_upper())
        return False

    def reuse(self, res):
        if self.just_done or not self.text:
            self.text = ""
            self.just_done = False
        elif self.text[-1].isdigit() or self.text[-1] in ".)%!πe":
            self.text += "×"
        self.put(res)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = self.get_active_window()
        if not win:
            notareal_theme.apply(os.path.join(HERE, "style.css"))
            win = Window(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
