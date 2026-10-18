#!/usr/bin/env python3
"""notarealGallery: the phone's photos + videos, backed up to and imported from Google Photos.

Grid (3 columns, newest first) with tabs ALL / CAMERA / SCREENS / VIDEOS / GOOGLE.
Viewer: swipe left/right = next/previous, swipe down = back, pinch or double-tap = zoom,
tap = hide/show the bars. Controls sit on the LEFT (left-handed user).
Backup runs in galleryd (autostart); IMPORT opens Google's Photos Picker.
"""
import collections
import concurrent.futures
import datetime
import json
import os
import signal
import subprocess
import sys
import threading

# 10-12: a hard freeze followed the iris HW video decoder; keep this app on software decoding
os.environ["GST_PLUGIN_FEATURE_RANK"] = ",".join(
    f"v4l2{c}dec:NONE" for c in ("vp8", "vp9", "h264", "h265", "mpeg2"))

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Graphene, Gsk, Gtk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.expanduser("~/theme"))
import notareal_theme  # noqa: E402
import gphotos  # noqa: E402
import media  # noqa: E402

APP_ID = "dev.notareal.Gallery"
CELL = 176                 # logical px per grid cell (540 / 3, minus spacing)
FULL_PX = 2400             # longest side of a photo in the viewer (enough for 2.5x zoom)
TEX_CACHE = 150            # thumbnail textures kept in memory
SWIPE_NAV = 0.22           # fraction of the width that flips to the next photo
SWIPE_CLOSE = 0.20         # fraction of the height (downwards) that closes the viewer
UNDO_S = 5
TABS = (("all", "ALL"), ("camera", "CAMERA"), ("screens", "SCREENS"), ("video", "VIDEOS"), ("google", "GOOGLE"))
STATUS = os.path.join(gphotos.DATA, "status.json")
PIDFILE = os.path.join(gphotos.DATA, "galleryd.pid")
SETUP_DOC = os.path.join(HERE, "GOOGLE-SETUP.md")


class Item(GObject.Object):
    def __init__(self, path, album, mtime):
        super().__init__()
        self.path, self.album, self.mtime = path, album, mtime
        self.video = media.is_video(path)


def fmt_date(t):
    d = datetime.datetime.fromtimestamp(t)
    today = datetime.date.today()
    if d.date() == today:
        day = "TODAY"
    elif (today - d.date()).days == 1:
        day = "YESTERDAY"
    else:
        day = d.strftime("%a %d %b %Y").upper()
    return f"{day} · {d:%H:%M}"


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# --- thumbnails ---------------------------------------------------------------

class Thumbs:
    """Background thumbnail maker + a small LRU of textures."""
    def __init__(self):
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.tex = collections.OrderedDict()
        self.waiting = {}            # path -> [callbacks]

    def get(self, path, cb):
        t = self.tex.get(path)
        if t is not None:
            self.tex.move_to_end(path)
            return t
        if path in self.waiting:
            self.waiting[path].append(cb)
            return None
        self.waiting[path] = [cb]
        self.pool.submit(self._work, path)
        return None

    def _work(self, path):
        out = media.make_thumb(path)
        GLib.idle_add(self._done, path, out)

    def _done(self, path, out):
        cbs = self.waiting.pop(path, [])
        tex = None
        if out:
            try:
                tex = Gdk.Texture.new_from_filename(out)
            except GLib.Error:
                tex = None
        if tex is not None:
            self.tex[path] = tex
            while len(self.tex) > TEX_CACHE:
                self.tex.popitem(last=False)
        for cb in cbs:
            cb(path, tex)
        return False

    def forget(self, path):
        self.tex.pop(path, None)


# --- zoomable photo -----------------------------------------------------------

class ZoomView(Gtk.Widget):
    """Draws one texture fitted to the widget; pinch/double-tap zoom, pan, swipe callbacks."""
    def __init__(self, on_nav, on_close, on_tap):
        super().__init__()
        self.on_nav, self.on_close, self.on_tap = on_nav, on_close, on_tap
        self.tex = None
        self.scale, self.off = 1.0, [0.0, 0.0]
        self.swipe = [0.0, 0.0]          # finger offset while swiping at scale 1
        self.zooming = False
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.set_hexpand(True)
        self.set_vexpand(True)

        z = Gtk.GestureZoom()
        z.connect("begin", self._zoom_begin)
        z.connect("scale-changed", self._zoom_changed)
        z.connect("end", self._zoom_end)
        self.add_controller(z)
        d = Gtk.GestureDrag()
        d.connect("drag-begin", self._drag_begin)
        d.connect("drag-update", self._drag_update)
        d.connect("drag-end", self._drag_end)
        self.add_controller(d)
        c = Gtk.GestureClick()
        c.connect("released", self._click)
        self.add_controller(c)
        self.tap_id = None

    def set_texture(self, tex, keep_zoom=False):
        self.tex = tex
        if not keep_zoom:
            self.scale, self.off = 1.0, [0.0, 0.0]
        self.swipe = [0.0, 0.0]
        self.queue_draw()

    def _fit(self):
        w, h = self.get_width(), self.get_height()
        if not self.tex or w <= 0 or h <= 0:
            return 0, 0
        tw, th = self.tex.get_width(), self.tex.get_height()
        k = min(w / tw, h / th)
        return tw * k, th * k

    def _clamp(self):
        w, h = self.get_width(), self.get_height()
        fw, fh = self._fit()
        for i, (full, view) in enumerate(((fw * self.scale, w), (fh * self.scale, h))):
            m = max(0.0, (full - view) / 2)
            self.off[i] = max(-m, min(m, self.off[i]))

    def _zoom_to(self, s, fx, fy):
        """Scale to s keeping the screen point (fx, fy) still."""
        s = max(1.0, min(6.0, s))
        cx, cy = self.get_width() / 2, self.get_height() / 2
        r = s / self.scale
        self.off = [(fx - cx) - (fx - cx - self.off[0]) * r, (fy - cy) - (fy - cy - self.off[1]) * r]
        self.scale = s
        if s == 1.0:
            self.off = [0.0, 0.0]
        self._clamp()
        self.queue_draw()

    def _zoom_begin(self, g, _seq):
        self.zooming = True
        self.z0 = self.scale
        ok, x, y = g.get_bounding_box_center()
        self.zc = (x, y) if ok else (self.get_width() / 2, self.get_height() / 2)
        self.swipe = [0.0, 0.0]

    def _zoom_changed(self, _g, s):
        self._zoom_to(self.z0 * s, *self.zc)

    def _zoom_end(self, *_):
        self.zooming = False
        if self.scale < 1.05:
            self._zoom_to(1.0, 0, 0)

    def _drag_begin(self, _g, _x, _y):
        self.d0 = list(self.off)

    def _drag_update(self, _g, dx, dy):
        if self.zooming:
            return
        if self.scale > 1.01:
            self.off = [self.d0[0] + dx, self.d0[1] + dy]
            self._clamp()
        else:
            self.swipe = [dx, max(0.0, dy) if abs(dy) > abs(dx) else 0.0]
        self.queue_draw()

    def _drag_end(self, _g, dx, dy):
        if self.zooming or self.scale > 1.01:
            return
        self.swipe = [0.0, 0.0]
        self.queue_draw()
        w, h = self.get_width(), self.get_height()
        if abs(dx) > w * SWIPE_NAV and abs(dx) > abs(dy):
            self.on_nav(-1 if dx > 0 else 1)
        elif dy > h * SWIPE_CLOSE and dy > abs(dx):
            self.on_close()

    def _click(self, _g, n, x, y):
        if n == 2:
            if self.tap_id:
                GLib.source_remove(self.tap_id)
                self.tap_id = None
            self._zoom_to(1.0 if self.scale > 1.01 else 2.5, x, y)
        elif n == 1:
            if self.tap_id:
                GLib.source_remove(self.tap_id)
            self.tap_id = GLib.timeout_add(260, self._single_tap)

    def _single_tap(self):
        self.tap_id = None
        self.on_tap()
        return False

    def do_snapshot(self, snap):
        if not self.tex:
            return
        w, h = self.get_width(), self.get_height()
        fw, fh = self._fit()
        sw, sh = fw * self.scale, fh * self.scale
        x = w / 2 + self.off[0] + self.swipe[0] - sw / 2
        y = h / 2 + self.off[1] + self.swipe[1] - sh / 2
        if self.swipe[1] > 0:       # shrink a little while pulling down to close
            k = max(0.7, 1 - self.swipe[1] / (h * 1.5))
            x, y, sw, sh = x + sw * (1 - k) / 2, y + sh * (1 - k) / 2, sw * k, sh * k
        snap.append_scaled_texture(self.tex, Gsk.ScalingFilter.TRILINEAR,
                                   Graphene.Rect().init(x, y, sw, sh))


# --- window ---------------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Gallery")
        self.set_default_size(540, 1100)
        self.thumbs = Thumbs()
        self.items = []
        self.tab = "all"
        self.state = gphotos.load_state()
        self.pending_delete = {}        # path -> GLib source id
        self.view_list = []             # items in the viewer's order (current tab)
        self.view_i = 0
        self.full_gen = 0
        self.import_cancel = False

        self.toasts = Adw.ToastOverlay()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=160)
        self.toasts.set_child(self.stack)
        self.set_content(self.toasts)
        self.stack.add_named(self._grid_page(), "grid")
        self.stack.add_named(self._viewer_page(), "viewer")

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

        self.rescan()
        self._watch_folders()
        GLib.timeout_add_seconds(3, self._poll_status)
        self._poll_status()

    # --- grid page
    def _grid_page(self):
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        hb.add_css_class("vapor-bar")
        gear = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings")
        gear.connect("clicked", lambda *_: self.open_settings())
        hb.pack_start(gear)
        title = Gtk.Label(label="NOTREAL.GALLERY")
        title.add_css_class("app-title")
        hb.set_title_widget(title)
        tv.add_top_bar(hb)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        chips_scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER,
                                          hscrollbar_policy=Gtk.PolicyType.EXTERNAL)
        self.chips = Gtk.Box(spacing=4, margin_start=10, margin_end=10, margin_top=6, margin_bottom=6)
        self.chips.add_css_class("filters")
        chips_scroll.set_child(self.chips)
        body.append(chips_scroll)
        self.chip_btns = {}
        group = None
        for key, text in TABS:
            b = Gtk.ToggleButton(label=text)
            b.add_css_class("filter")
            if group:
                b.set_group(group)
            group = group or b
            b.set_active(key == self.tab)
            b.connect("toggled", self._tab_toggled, key)
            self.chips.append(b)
            self.chip_btns[key] = b

        self.store = Gio.ListStore(item_type=Item)
        self.filter = Gtk.CustomFilter.new(self._visible)
        self.filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._cell_setup)
        factory.connect("bind", self._cell_bind)
        factory.connect("unbind", self._cell_unbind)
        self.grid = Gtk.GridView(model=Gtk.NoSelection(model=self.filtered), factory=factory,
                                 min_columns=3, max_columns=3, single_click_activate=True)
        self.grid.add_css_class("photo-grid")
        self.grid.connect("activate", lambda _g, pos: self.open_viewer(pos))
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.grid)
        self.empty = Gtk.Label(label="NO PHOTOS HERE", vexpand=True)
        self.empty.add_css_class("empty")
        self.grid_stack = Gtk.Stack()
        self.grid_stack.add_named(scroll, "grid")
        self.grid_stack.add_named(self.empty, "empty")
        body.append(self.grid_stack)

        # bottom bar: IMPORT + backup button on the LEFT, status text after them
        bar = Gtk.Box(spacing=6, margin_start=10, margin_end=10, margin_top=6, margin_bottom=8)
        bar.add_css_class("bottom-bar")
        self.import_btn = Gtk.Button(label="IMPORT")
        self.import_btn.add_css_class("pill-btn")
        self.import_btn.set_tooltip_text("Pick photos from Google Photos")
        self.import_btn.connect("clicked", lambda *_: self.start_import())
        bar.append(self.import_btn)
        self.backup_btn = Gtk.Button(icon_name="weather-overcast-symbolic", tooltip_text="Back up now")
        self.backup_btn.add_css_class("flat")
        self.backup_btn.connect("clicked", lambda *_: self.backup_now())
        bar.append(self.backup_btn)
        self.status = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        self.status.add_css_class("status")
        bar.append(self.status)
        tv.add_bottom_bar(bar)
        tv.set_content(body)
        return tv

    def _cell_setup(self, _f, li):
        ov = Gtk.Overlay()
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        pic.set_size_request(CELL, CELL)
        pic.add_css_class("cell")
        ov.set_child(pic)
        badge = Gtk.Label(halign=Gtk.Align.START, valign=Gtk.Align.END, margin_start=6, margin_bottom=4)
        badge.add_css_class("badge")
        ov.add_overlay(badge)
        cloud = Gtk.Label(label="☁", halign=Gtk.Align.END, valign=Gtk.Align.END, margin_end=6, margin_bottom=4)
        cloud.add_css_class("cloud")
        ov.add_overlay(cloud)
        li.set_child(ov)
        ov.pic, ov.badge, ov.cloud, ov.path = pic, badge, cloud, None

    def _cell_bind(self, _f, li):
        it, ov = li.get_item(), li.get_child()
        ov.path = it.path
        ov.badge.set_label("▶" if it.video else "")
        ov.badge.set_visible(it.video)
        ov.cloud.set_visible(gphotos.is_backed_up(self.state, it.path))
        tex = self.thumbs.get(it.path, lambda p, t, ov=ov: ov.path == p and ov.pic.set_paintable(t))
        ov.pic.set_paintable(tex)

    def _cell_unbind(self, _f, li):
        ov = li.get_child()
        ov.path = None
        ov.pic.set_paintable(None)

    def _visible(self, it):
        if it.path in self.pending_delete:
            return False
        if self.tab == "all":
            return True
        if self.tab == "video":
            return it.video
        return it.album == self.tab

    def _tab_toggled(self, b, key):
        if b.get_active() and key != self.tab:
            self.tab = key
            self.filter.changed(Gtk.FilterChange.DIFFERENT)
            self._update_empty()
            self.grid.scroll_to(0, Gtk.ListScrollFlags.NONE, None) if self.filtered.get_n_items() else None

    def _update_empty(self):
        n = self.filtered.get_n_items()
        self.grid_stack.set_visible_child_name("grid" if n else "empty")
        if not n:
            self.empty.set_label("NOTHING IMPORTED YET\nTAP IMPORT" if self.tab == "google" else "NO PHOTOS HERE")
        counts = collections.Counter()
        for it in self.items:
            if it.path in self.pending_delete:
                continue
            counts["all"] += 1
            counts[it.album] += 1
            counts["video"] += it.video
        for key, text in TABS:
            c = counts.get(key, 0)
            self.chip_btns[key].set_label(f"{text} {c}" if c else text)

    def rescan(self):
        def work():
            found = media.scan()
            GLib.idle_add(self._apply_scan, found)
        threading.Thread(target=work, daemon=True).start()

    def _apply_scan(self, found):
        old = {it.path: it for it in self.items}
        self.items = []
        for p, a, m in found:
            it = old.get(p)
            if it is None or it.mtime != m:
                if it is not None:
                    self.thumbs.forget(p)
                it = Item(p, a, m)
            self.items.append(it)
        self.store.splice(0, self.store.get_n_items(), self.items)
        self._update_empty()
        return False

    def _watch_folders(self):
        self.monitors = []
        self.rescan_id = None
        for folder, _a, _r in media.SOURCES:
            os.makedirs(folder, exist_ok=True) if folder.endswith("Google") else None
            if os.path.isdir(folder):
                m = Gio.File.new_for_path(folder).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
                m.connect("changed", self._folder_changed)
                self.monitors.append(m)

    def _folder_changed(self, *_):
        if self.rescan_id:
            GLib.source_remove(self.rescan_id)
        self.rescan_id = GLib.timeout_add(800, self._rescan_now)

    def _rescan_now(self):
        self.rescan_id = None
        self.rescan()
        return False

    # --- backup status
    def _poll_status(self):
        if not self.get_mapped():
            return True
        self.state = gphotos.load_state()
        try:
            st = json.load(open(STATUS))
        except (OSError, ValueError):
            st = {}
        s = media.settings()
        cands = [p for p, _a, _m in media.backup_candidates(s)]
        done = sum(1 for p in cands if gphotos.is_backed_up(self.state, p))
        if not gphotos.has_client() or not gphotos.signed_in():
            text = "☁ sign in to back up" if gphotos.has_client() else "☁ Google not set up · ⚙"
        elif not s.get("backup"):
            text = "☁ backup off"
        elif st.get("state") == "uploading":
            text = "☁ " + st.get("text", "")
        elif done == len(cands):
            text = f"☁ all {done} backed up"
        elif st.get("state") in ("waiting", "error"):
            text = "☁ " + st.get("text", "")
        else:
            text = f"☁ {done}/{len(cands)} backed up"
        if not getattr(self, "import_busy", False):
            self.status.set_label(text)
        return True

    def _daemon_pid(self):
        try:
            pid = int(open(PIDFILE).read())
            if "galleryd" in open(f"/proc/{pid}/cmdline").read():
                return pid
        except (OSError, ValueError):
            pass
        return None

    def backup_now(self):
        if not gphotos.has_client():
            return self.show_setup()
        if not gphotos.signed_in():
            return self.sign_in(then=self.backup_now)
        s = media.settings()
        if not s.get("backup"):
            s["backup"] = True
            media.save_settings(s)
        pid = self._daemon_pid()
        if pid is None:
            GLib.spawn_async([os.path.join(HERE, "galleryd")], flags=GLib.SpawnFlags.DEFAULT)
            self.toast("backup service started")
            return
        os.kill(pid, signal.SIGUSR1)
        self.toast("backing up now (also on mobile data)")

    # --- Google
    def toast(self, text, button=None, cb=None, timeout=3):
        t = Adw.Toast(title=text, timeout=timeout)
        if button:
            t.set_button_label(button)
            t.connect("button-clicked", lambda *_: cb())
        self.toasts.add_toast(t)
        return t

    def show_setup(self):
        d = Adw.AlertDialog(heading="Connect Google Photos",
                            body="One-time setup (about 2 minutes) in the Google Cloud console:\n\n"
                                 "1. APIs & Services → Library: enable “Photos Library API” and "
                                 "“Google Photos Picker API”.\n"
                                 "2. OAuth consent screen: add your Gmail as a test user.\n"
                                 "3. Credentials → Create OAuth client ID → Desktop app (not Web) → "
                                 "Download JSON.\n"
                                 "4. Download it to the phone's Downloads folder; Gallery picks it up.\n\n"
                                 "Then tap IMPORT or the cloud button again.\n"
                                 "(Same steps in ~/gallery/GOOGLE-SETUP.md)")
        d.add_response("ok", "OK")
        d.add_response("console", "OPEN CONSOLE")
        d.set_response_appearance("console", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r == "console":
                gphotos.open_in_browser("https://console.cloud.google.com/apis/credentials")
        d.connect("response", resp)
        d.present(self)

    def sign_in(self, then=None):
        self.status.set_label("☁ finish signing in in the browser…")

        def work():
            try:
                gphotos.sign_in(gphotos.open_in_browser)
                GLib.idle_add(done, None)
            except Exception as e:
                GLib.idle_add(done, str(e))

        def done(err):
            self.present()
            if err:
                self.toast("sign-in failed: " + err, timeout=6)
            else:
                self.toast("signed in to Google Photos")
                pid = self._daemon_pid()
                if pid:
                    os.kill(pid, signal.SIGUSR1)
                if then:
                    then()
            self._poll_status()
            return False
        threading.Thread(target=work, daemon=True).start()

    def start_import(self):
        if getattr(self, "import_busy", False):
            self.import_cancel = True
            self.toast("import cancelled")
            return
        if not gphotos.has_client():
            return self.show_setup()
        if not gphotos.signed_in():
            return self.sign_in(then=self.start_import)
        self.import_busy, self.import_cancel = True, False
        self.import_btn.set_label("CANCEL")

        def progress(text):
            GLib.idle_add(self.status.set_label, "⇩ " + text)

        def work():
            try:
                saved = gphotos.run_picker(gphotos.open_in_browser, progress, lambda: self.import_cancel)
                GLib.idle_add(done, saved, None)
            except Exception as e:
                GLib.idle_add(done, [], str(e))

        def done(saved, err):
            self.import_busy = False
            self.import_btn.set_label("IMPORT")
            self.present()
            if err:
                self.toast("import failed: " + err, timeout=6)
            elif saved:
                self.toast(f"imported {len(saved)}")
                self.chip_btns["google"].set_active(True)
                self.rescan()
            self._poll_status()
            return False
        threading.Thread(target=work, daemon=True).start()

    def open_settings(self):
        s = media.settings()
        dlg = Adw.PreferencesDialog(title="Settings")
        page = Adw.PreferencesPage()
        dlg.add(page)

        acct = Adw.PreferencesGroup(title="Google Photos")
        page.add(acct)
        row = Adw.ActionRow()
        if not gphotos.has_client():
            row.set_title("Not set up")
            row.set_subtitle("needs your own OAuth client (2 min)")
            b = Gtk.Button(label="HOW", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_: (dlg.close(), self.show_setup()))
        elif gphotos.signed_in():
            row.set_title("Signed in")
            row.set_subtitle(f"backups go to the “{gphotos.ALBUM_TITLE}” album")
            b = Gtk.Button(label="SIGN OUT", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_: (gphotos.sign_out(), dlg.close(), self._poll_status()))
        else:
            row.set_title("Not signed in")
            b = Gtk.Button(label="SIGN IN", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_: (dlg.close(), self.sign_in()))
        b.add_css_class("pill-btn")
        row.add_prefix(b)          # left-handed: the button on the left
        acct.add(row)

        grp = Adw.PreferencesGroup(title="Backup")
        page.add(grp)

        def switch(key, title, sub):
            r = Adw.SwitchRow(title=title, subtitle=sub, active=bool(s.get(key)))

            def changed(row, _p):
                s[key] = row.get_active()
                media.save_settings(s)
                self._poll_status()
            r.connect("notify::active", changed)
            grp.add(r)
        switch("backup", "Back up camera photos + videos", "new shots upload by themselves")
        switch("screenshots", "Include screenshots + screen recordings", None)
        switch("wifi_only", "Wi-Fi only", "the cloud button forces a backup on mobile data")
        dlg.present(self)

    # --- viewer page
    def _viewer_page(self):
        ov = Gtk.Overlay()
        ov.add_css_class("viewer")
        self.vstack = Gtk.Stack(transition_type=Gtk.StackTransitionType.NONE)
        self.zoom = ZoomView(self.nav, self.close_viewer, self.toggle_chrome)
        self.vstack.add_named(self.zoom, "photo")
        self.video = Gtk.Video(autoplay=True, hexpand=True, vexpand=True)
        vd = Gtk.GestureDrag()
        vd.connect("drag-end", self._video_drag_end)
        self.video.add_controller(vd)
        self.vstack.add_named(self.video, "video")
        ov.set_child(self.vstack)

        top = Gtk.Box(spacing=4, valign=Gtk.Align.START, margin_start=6, margin_end=6, margin_top=6)
        top.add_css_class("viewer-bar")
        back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back")
        back.add_css_class("flat")
        back.connect("clicked", lambda *_: self.close_viewer())
        top.append(back)
        self.v_title = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        self.v_title.add_css_class("viewer-title")
        top.append(self.v_title)
        self.v_count = Gtk.Label()
        self.v_count.add_css_class("viewer-count")
        top.append(self.v_count)
        ov.add_overlay(top)

        bottom = Gtk.Box(spacing=4, valign=Gtk.Align.END, margin_start=6, margin_end=6, margin_bottom=10)
        bottom.add_css_class("viewer-bar")
        for icon, tip, cb in (("emblem-shared-symbolic", "Share", self.share_current),
                              ("document-edit-symbolic", "Edit", self.edit_current),
                              ("user-trash-symbolic", "Delete", self.delete_current),
                              ("dialog-information-symbolic", "Info", self.info_current),
                              ("document-open-symbolic", "Open in Loupe", self.open_external)):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.add_css_class("flat")
            b.connect("clicked", lambda *_, cb=cb: cb())
            bottom.append(b)
        self.v_cloud = Gtk.Label(hexpand=True, xalign=1)
        self.v_cloud.add_css_class("viewer-count")
        bottom.append(self.v_cloud)
        ov.add_overlay(bottom)
        self.chrome = (top, bottom)
        return ov

    def toggle_chrome(self):
        vis = not self.chrome[0].get_visible()
        for w in self.chrome:
            w.set_visible(vis)

    def open_viewer(self, pos):
        self.view_list = [self.filtered.get_item(i) for i in range(self.filtered.get_n_items())]
        if not self.view_list:
            return
        self.view_i = max(0, min(pos, len(self.view_list) - 1))
        for w in self.chrome:
            w.set_visible(True)
        self.stack.set_visible_child_name("viewer")
        self.show_current()

    def close_viewer(self):
        self.video.set_media_stream(None)
        self.full_gen += 1
        self.stack.set_visible_child_name("grid")
        if self.view_list:
            it = self.view_list[self.view_i]
            for i in range(self.filtered.get_n_items()):
                if self.filtered.get_item(i) is it:
                    self.grid.scroll_to(i, Gtk.ListScrollFlags.NONE, None)
                    break

    def nav(self, step):
        j = self.view_i + step
        if 0 <= j < len(self.view_list):
            self.view_i = j
            self.show_current()

    def _video_drag_end(self, _g, dx, dy):
        w, h = self.video.get_width(), self.video.get_height()
        if abs(dx) > w * SWIPE_NAV and abs(dx) > abs(dy):
            self.nav(-1 if dx > 0 else 1)
        elif dy > h * SWIPE_CLOSE and dy > abs(dx):
            self.close_viewer()

    def show_current(self):
        it = self.view_list[self.view_i]
        self.v_title.set_label(fmt_date(it.mtime))
        self.v_count.set_label(f"{self.view_i + 1}/{len(self.view_list)}")
        backed = gphotos.is_backed_up(self.state, it.path)
        self.v_cloud.set_label("☁ backed up" if backed else ("☁ imported" if it.album == "google" else ""))
        self.full_gen += 1
        gen = self.full_gen
        if it.video:
            self.zoom.set_texture(None)
            self.vstack.set_visible_child_name("video")
            self.video.set_file(Gio.File.new_for_path(it.path))
            return
        self.video.set_media_stream(None)
        self.vstack.set_visible_child_name("photo")
        self.zoom.set_texture(self.thumbs.tex.get(it.path))   # instant blurry preview

        def work():
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(it.path, FULL_PX, FULL_PX, True)
                pb = pb.apply_embedded_orientation() or pb
            except GLib.Error:
                pb = None
            GLib.idle_add(done, pb)

        def done(pb):
            if gen == self.full_gen and pb is not None:
                self.zoom.set_texture(Gdk.Texture.new_for_pixbuf(pb), keep_zoom=True)
            return False
        threading.Thread(target=work, daemon=True).start()

    def edit_current(self):
        """notarealEdit: crop / curves / saturation; saves a copy next to the original."""
        if not self.view_list:
            return
        it = self.view_list[self.view_i]
        if it.path and os.path.isfile(it.path) and not getattr(it, "video", False) and \
                (Gio.content_type_guess(it.path, None)[0] or "").startswith("image/"):
            subprocess.Popen([os.path.expanduser("~/edit/nredit"), it.path], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            self.toast("only photos can be edited")

    def share_current(self):
        """notarealShare: send the photo/video to a chat or a new mail."""
        if not self.view_list:
            return
        path = self.view_list[self.view_i].path
        if path and os.path.isfile(path):
            subprocess.Popen([os.path.expanduser("~/share/nrshare"), path], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            self.toast("not on the phone yet")

    def delete_current(self):
        if not self.view_list:
            return
        it = self.view_list[self.view_i]
        self.video.set_media_stream(None)
        sid = GLib.timeout_add_seconds(UNDO_S, self._trash, it.path)
        self.pending_delete[it.path] = sid
        self.filter.changed(Gtk.FilterChange.MORE_STRICT)
        self._update_empty()
        del self.view_list[self.view_i]
        note = "" if gphotos.is_backed_up(self.state, it.path) or it.album == "google" else " (not backed up)"

        def undo():
            s = self.pending_delete.pop(it.path, None)
            if s:
                GLib.source_remove(s)
            self.filter.changed(Gtk.FilterChange.LESS_STRICT)
            self._update_empty()
        self.toast("moved to trash" + note, "UNDO", undo, timeout=UNDO_S)
        if not self.view_list:
            self.close_viewer()
            return
        self.view_i = min(self.view_i, len(self.view_list) - 1)
        self.show_current()

    def _trash(self, path):
        self.pending_delete.pop(path, None)
        try:
            Gio.File.new_for_path(path).trash(None)
        except GLib.Error as e:
            self.toast("could not delete: " + e.message)
        return False

    def info_current(self):
        it = self.view_list[self.view_i]
        try:
            size = fmt_size(os.path.getsize(it.path))
        except OSError:
            size = "?"
        dims = ""
        if not it.video:
            fmt = GdkPixbuf.Pixbuf.get_file_info(it.path)
            if fmt and fmt[0]:
                dims = f"{fmt[1]} × {fmt[2]}\n"
        cloud = ("backed up to Google Photos" if gphotos.is_backed_up(self.state, it.path)
                 else "imported from Google Photos" if it.album == "google" else "not backed up")
        d = Adw.AlertDialog(heading=os.path.basename(it.path),
                            body=f"{fmt_date(it.mtime)}\n{dims}{size}\n{cloud}\n\n{os.path.dirname(it.path)}")
        d.add_response("ok", "OK")
        d.present(self)

    def open_external(self):
        it = self.view_list[self.view_i]
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(it.path).get_uri(), None)

    def _key(self, _c, keyval, _code, _state):
        if self.stack.get_visible_child_name() != "viewer":
            return False
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_BackSpace):
            self.close_viewer()
        elif keyval == Gdk.KEY_Right:
            self.nav(1)
        elif keyval == Gdk.KEY_Left:
            self.nav(-1)
        else:
            return False
        return True

    def flush_deletes(self):
        for path, sid in list(self.pending_delete.items()):
            GLib.source_remove(sid)
            self._trash(path)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.win = None

    def do_activate(self):
        if self.win is None:
            notareal_theme.apply(os.path.join(HERE, "style.css"))
            self.win = Window(self)
            self.win.connect("close-request", lambda *_: (self.win.flush_deletes(), False)[1])
            if os.environ.get("GALLERY_TEST_OPEN"):   # debug: open the viewer at this grid position
                GLib.timeout_add(2000, lambda: self.win.open_viewer(int(os.environ["GALLERY_TEST_OPEN"])) and False)
        self.win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
