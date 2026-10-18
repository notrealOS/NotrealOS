#!/usr/bin/env python3
"""notarealLink: a light WebKit viewer for links opened from notarealChat and vaporMail.

Opens fast and small (one WebKit web process), so a link from a chat doesn't wait for a whole
browser. The bar sits at the BOTTOM with the buttons on the LEFT (left-handed thumb):
back, open in the full browser, copy link; the host + a progress line on the right.
One window per app run; more links open as more windows (NON_UNIQUE would cost a WebKit each,
so this is a single app that takes new URLs over D-Bus).
"""
import os
import subprocess
import sys
import urllib.parse

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango, WebKit  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.expanduser("~/theme"))
import notareal_theme  # noqa: E402

APP_ID = "dev.notareal.Link"
DATA = os.path.expanduser("~/.local/share/nrlink")
CACHE = os.path.expanduser("~/.cache/nrlink")
# the "real" browser for OPEN IN BROWSER (chromium if installed, like WhatsApp Calls)
BROWSERS = ("chromium.desktop", "firefox-esr.desktop")
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/124.0 Mobile Safari/537.36")


def full_browser(uri):
    for d in BROWSERS:
        info = Gio.DesktopAppInfo.new(d)
        if info:
            info.launch_uris([uri], None)
            return
    subprocess.Popen(["xdg-open", uri])


class Window(Adw.ApplicationWindow):
    def __init__(self, app, session):
        super().__init__(application=app, title="Link", default_width=540, default_height=1100)
        self.view = WebKit.WebView(network_session=session, vexpand=True)
        s = self.view.get_settings()
        s.set_user_agent(MOBILE_UA)                 # phone layouts, not desktop sites
        s.set_enable_developer_extras(False)
        s.set_enable_back_forward_navigation_gestures(True)
        s.set_media_playback_requires_user_gesture(True)
        self.view.connect("notify::estimated-load-progress", self._progress)
        self.view.connect("notify::uri", self._uri)
        self.view.connect("decide-policy", self._policy)
        self.view.connect("create", self._popup)      # target=_blank: open in this window
        # 10-17: the keyboard never came up on web forms (Plaid). A finger tap focuses the field
        # inside the page but doesn't always give the WebView GTK focus (the bar buttons kept it),
        # and without GTK focus WebKit never enables the input method -> no OSK. Every touch on the
        # page now focuses the view (capture phase, not claimed: WebKit still gets the tap).
        tap = Gtk.GestureClick(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        tap.connect("pressed", lambda *_: self.view.grab_focus())
        self.view.add_controller(tap)
        if os.environ.get("NRLINK_DEBUG"):
            import time
            t0 = time.monotonic()
            self.view.connect("load-changed", lambda v, ev: ev == WebKit.LoadEvent.FINISHED and
                              print(f"nrlink: loaded {v.get_uri()} in {time.monotonic() - t0:.2f}s", flush=True))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.bar_progress = Gtk.ProgressBar(css_classes=["link-progress"])
        box.append(self.view)
        box.append(self.bar_progress)
        bar = Gtk.Box(spacing=4, css_classes=["link-bar"])
        for icon, tip, cb in (("go-previous-symbolic", "Back", self.back),
                              ("web-browser-symbolic", "Open in browser", self.to_browser),
                              ("edit-copy-symbolic", "Copy link", self.copy)):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=["flat", "circular"],
                           can_focus=False)                  # focus stays on the page (keyboard)
            b.connect("clicked", lambda *_, cb=cb: cb())
            bar.append(b)
        self.host = Gtk.Label(hexpand=True, xalign=1, ellipsize=Pango.EllipsizeMode.MIDDLE, css_classes=["link-host"])
        bar.append(self.host)
        close = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"], can_focus=False)
        close.connect("clicked", lambda *_: self.close())
        bar.append(close)
        box.append(bar)
        self.toasts = Adw.ToastOverlay(child=box)
        self.set_content(self.toasts)

    def load(self, uri):
        self.view.load_uri(uri)
        self.present()
        self.view.grab_focus()

    def _progress(self, *_):
        p = self.view.get_estimated_load_progress()
        self.bar_progress.set_fraction(p)
        self.bar_progress.set_visible(p < 1.0)

    def _uri(self, *_):
        u = self.view.get_uri() or ""
        self.host.set_label(urllib.parse.urlsplit(u).hostname or u)

    def _policy(self, view, decision, kind):
        # non-web links (tel:, mailto:, intent://, app stores) go to the system
        if kind == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri() or ""
            scheme = uri.split(":", 1)[0].lower()
            if scheme not in ("http", "https", "about", "data", "blob"):
                decision.ignore()
                try:
                    Gio.AppInfo.launch_default_for_uri(uri, None)
                except GLib.Error:
                    pass
                return True
        return False

    def _popup(self, view, action):
        uri = action.get_request().get_uri()
        if uri:
            view.load_uri(uri)
        return None

    def back(self):
        if self.view.can_go_back():
            self.view.go_back()
        else:
            self.close()

    def to_browser(self):
        u = self.view.get_uri()
        if u:
            full_browser(u)
            self.close()

    def copy(self):
        u = self.view.get_uri()
        if u:
            Gdk.Display.get_default().get_clipboard().set(u)
            self.toasts.add_toast(Adw.Toast(title="link copied", timeout=2))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_OPEN | Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.session = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        notareal_theme.apply(os.path.join(HERE, "style.css"))
        os.makedirs(DATA, exist_ok=True)
        # cookies/logins kept between links (one shared session, cheap)
        self.session = WebKit.NetworkSession.new(DATA, CACHE)
        self.session.get_cookie_manager().set_persistent_storage(
            os.path.join(DATA, "cookies.sqlite"), WebKit.CookiePersistentStorage.SQLITE)
        ctx = WebKit.WebContext.get_default()
        ctx.set_cache_model(WebKit.CacheModel.DOCUMENT_VIEWER)   # smallest memory caches

    def open_uri(self, uri):
        # reuse the window that is already open (one WebKit process), else a new one
        win = self.get_active_window() or (self.get_windows()[0] if self.get_windows() else None)
        if not win:
            win = Window(self, self.session)
        win.load(uri)

    def do_command_line(self, cl):
        args = [a for a in cl.get_arguments()[1:] if not a.startswith("--")]
        if not args:
            self.activate()
            return 0
        for a in args[:1]:
            f = Gio.File.new_for_commandline_arg(a)
            self.open_uri(a if "://" in a else f.get_uri())
        return 0

    def do_activate(self):
        if not self.get_windows():
            self.open_uri("https://duckduckgo.com/")
        else:
            self.get_windows()[0].present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
