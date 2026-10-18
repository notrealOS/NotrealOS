"""Shared notareal theme for our GTK4 apps (and the code-drawn shell parts).

    import notareal_theme
    notareal_theme.apply(os.path.join(HERE, "style.css"))   # after the display exists

Themes live in ~/theme/themes/<name>.css (+ optional <name>.override.css); the active one is
named in ~/theme/active. Switch with  ~/theme/nrtheme <name>  - every running app restyles live.
Layers: theme (600) < app style.css (650) < theme override (700) < user gtk.css (800).

Code-drawn colors:
    notareal_theme.hex("accent"), .color("accent", 0.5) -> Gdk.RGBA, .rgb("accent") -> (r, g, b) 0..1
    notareal_theme.resolve("#c5a0ff") -> the active theme's color of that NAME (vapor hex = name)
    notareal_theme.on_change(callback)  -> called after a theme switch/edit (redraw there)
    notareal_theme.value("wallpaper")   -> non-color settings, e.g. "micro"
"""
import os
import re

DIR = os.path.expanduser("~/theme")
THEMES = os.path.join(DIR, "themes")
ACTIVE = os.path.join(DIR, "active")
THEME_PRIORITY = 600   # Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
APP_PRIORITY = 650
OVERRIDE_PRIORITY = 700

# vapor's hex values (as written all over the older code) -> palette names
VAPOR = {"#1c2340": "bg", "#161c36": "bg-deep", "#232a4d": "surface", "#1f2647": "bg", "#262f55": "surface",
         "#e9e6fb": "fg", "#eef0ff": "fg-bright", "#dfe3ff": "fg", "#e8dcff": "fg", "#f3e6ff": "fg",
         "#ffffff": "white", "#c5a0ff": "accent", "#d9c2ff": "accent-light", "#b38cf5": "violet",
         "#b48cff": "violet", "#8d6ff0": "violet", "#8fe0f2": "primary", "#6fd6f2": "cyan", "#5fe3cf": "mint",
         "#f7a1e8": "pink", "#ff7eb6": "hot-pink", "#ff8fb3": "hot-pink", "#ff9fcb": "hot-pink",
         "#7ef0d2": "mint", "#fffb96": "yellow", "#ffb86b": "orange", "#5aa9ff": "blue",
         "#3a2a78": "glow", "#33286e": "glow", "#2a2460": "bg-deep", "#232457": "bg", "#785aaa": "glow",
         "#3a3f6e": "surface", "#141830": "bg-deep"}

_state = {"name": None, "css": "", "palette": {}, "values": {}}
_providers = None      # (theme, override) - one pair per process
_callbacks = []
_keep = []


def active():
    try:
        name = os.environ.get("NR_THEME") or open(ACTIVE).read().strip()   # NR_THEME: previews/tests
    except OSError:
        name = ""
    return name if os.path.exists(os.path.join(THEMES, name + ".css")) else "vapor"


def _read():
    name = active()
    try:
        css = open(os.path.join(THEMES, name + ".css")).read()
    except OSError:
        css = ""
    _state.update(name=name, css=css,
                  palette={m[1]: m[2].lower() for m in re.finditer(r"--nr-([\w-]+):\s*(#[0-9a-fA-F]{3,8})\b", css)},
                  values={m[1]: m[2].strip() for m in re.finditer(r"--nr-([\w-]+):\s*([^;#]+?)\s*;", css)})


def palette():
    if _state["name"] is None or _providers is None:
        _read()      # no live monitor in this process: read fresh each time
    return dict(_state["palette"])


def hex(name, fallback="#ff00ff"):
    return palette().get(name, fallback)


def value(name, fallback=""):
    if _state["name"] is None or _providers is None:
        _read()
    return _state["values"].get(name, fallback)


def rgb(name):
    h = hex(name).lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def color(name, alpha=1.0):
    from gi.repository import Gdk
    c = Gdk.RGBA()
    c.parse(hex(name))
    c.alpha = alpha
    return c


def resolve(spec):
    """'#c5a0ff' (a vapor color) -> the active theme's color with the same name; others unchanged."""
    if isinstance(spec, str) and spec.lower() in VAPOR:
        return palette().get(VAPOR[spec.lower()], spec)
    return spec


def on_change(callback):
    _callbacks.append(callback)


def _reload(*_):
    _read()
    theme_p, over_p = _providers
    theme_p.load_from_string(_state["css"])
    over = os.path.join(THEMES, _state["name"] + ".override.css")
    over_p.load_from_string(open(over).read() if os.path.exists(over) else "")
    for cb in list(_callbacks):
        try:
            cb()
        except Exception as e:  # a broken redraw must not break the theme switch
            print("notareal_theme callback:", e)
    return False


def _watch(path, directory, then):
    from gi.repository import Gio, GLib
    f = Gio.File.new_for_path(path)
    mon = f.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None) if directory else \
        f.monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
    pending = {"id": 0}

    def changed(*_):
        if pending["id"]:
            GLib.source_remove(pending["id"])

        def run():
            pending["id"] = 0
            then()
            return False
        pending["id"] = GLib.timeout_add(250, run)   # debounce editor save bursts
    mon.connect("changed", changed)
    _keep.append(mon)


def apply(app_css=None, display=None):
    """Theme + optional app stylesheet + theme overrides on the display; all live-reload."""
    global _providers
    from gi.repository import Gdk, Gtk
    display = display or Gdk.Display.get_default()
    if _providers is None:
        _providers = (Gtk.CssProvider(), Gtk.CssProvider())
        Gtk.StyleContext.add_provider_for_display(display, _providers[0], THEME_PRIORITY)
        Gtk.StyleContext.add_provider_for_display(display, _providers[1], OVERRIDE_PRIORITY)
        _reload()
        _watch(DIR, True, _reload)       # ~/theme/active switched
        _watch(THEMES, True, _reload)    # a theme file edited
    if app_css:
        provider = Gtk.CssProvider()

        def load():
            if os.path.exists(app_css):
                provider.load_from_path(app_css)
        load()
        Gtk.StyleContext.add_provider_for_display(display, provider, APP_PRIORITY)
        _watch(app_css, False, load)
        _keep.append(provider)


def add_css(css_text, display=None):
    """An app's CSS given as a string (layer-shell parts keep theirs in code)."""
    from gi.repository import Gdk, Gtk
    apply(display=display)
    provider = Gtk.CssProvider()
    provider.load_from_string(css_text)
    Gtk.StyleContext.add_provider_for_display(display or Gdk.Display.get_default(), provider, APP_PRIORITY)
    _keep.append(provider)
    return provider
