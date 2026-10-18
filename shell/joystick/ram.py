"""ram: the RAM page of the joystick panel (opened by tapping the battery line on the widget).

Shows memory totals and the biggest apps. Each app is a process group: everything from the
first process below the session (gnome-session, init, the compositor) down, so Firefox
content processes or a launcher loop's python child count toward one row. Sizes are PSS
(shared pages split between the processes using them), so the rows add up honestly.
END (right side, away from the left thumb) needs a second tap within CONFIRM_MS, then
SIGTERMs the whole group and SIGKILLs whatever is left KILL_AFTER_MS later.
"""

import os
import signal
import threading

from gi.repository import GLib, Gtk, Pango

ROWS = 7
REFRESH_MS = 3000
CONFIRM_MS = 3000
KILL_AFTER_MS = 3000

RAM_CSS = b"""
.rampage { background: alpha(var(--nr-bg), 0.94); border: 2px solid var(--nr-accent); border-radius: 12px;
           margin: 8px 10px 10px 10px; padding: 10px; font-family: var(--nr-font-code); }
.ram-title { color: var(--nr-pink); font-size: 15px; font-weight: bold; letter-spacing: 2px; }
.ram-sum { color: var(--nr-accent); font-size: 11px; }
.ram-name { color: var(--nr-white); font-size: 13px; font-weight: bold; }
.ram-sub { color: var(--nr-accent-light); font-size: 10px; }
.ram-size { color: var(--nr-mint); font-size: 13px; font-weight: bold; }
.ram-bar { min-height: 4px; border-radius: 2px; }
.ram-bar trough { background: alpha(var(--nr-accent), 0.15); min-height: 4px; border-radius: 2px; }
.ram-bar progress { background: var(--nr-gradient);
                    min-height: 4px; border-radius: 2px; }
button.ram-end { background: var(--nr-surface); color: var(--nr-hot-pink); border: 1px solid var(--nr-hot-pink); border-radius: 6px;
                 min-height: 32px; min-width: 58px; padding: 0 6px; font-size: 11px; font-weight: bold; }
button.ram-end.sure { background: var(--nr-hot-pink); color: var(--nr-bg); }
.ram-keep { color: var(--nr-accent); font-size: 10px; min-width: 58px; }
"""

# group roots: a process whose parent is one of these starts its own row
ROOT_PARENTS = {"init", "gnome-session-b", "phoc", "dbus-run-sessio",   # comm is cut at 15 chars
                "systemd", "greetd"}
# rows that get no END button: the session itself, things this panel lives in, the terminal
# (ending Console kills the Claude sessions running in it) and audio
PROTECT = {"PHOSH", "PHOC", "GNOME-SESSION-BINARY", "WIDGET", "KEYBOARD", "NOTAREALSHELL",
           "CONSOLE", "CLAUDE", "CLAUDE BACKGROUND", "PULSEAUDIO", "PIPEWIRE", "WIREPLUMBER", "XWAYLAND",
           "DBUS-DAEMON", "CRASHLOG", "CALLAUDIOD", "EDGE-SWIPE", "GESTURES", "CRT", "SWIPE"}
NAMES = {"spin": "PLAYER", "notarealreddit": "REDDIT", "vapormail": "MAIL", "vapormaild": "MAIL SYNC",
         "swipekbd": "KEYBOARD", "swipekbd-session": "KEYBOARD", "overview-widget": "WIDGET",
         "phantomshell": "NOTAREALSHELL", "crtd": "CRT", "crtd-loop": "CRT", "rotated": "ROTATE",
         "camflash": "CAMFLASH", "camflash-loop": "CAMFLASH", "voiced": "VOICE", "alarmd": "ALARM",
         "gnome-calls": "CALLS", "chatty": "CHATTY", "kgx": "CONSOLE", "kgx-logged": "CONSOLE",
         "claude": "CLAUDE", "firefox": "FIREFOX", "firefox-esr": "FIREFOX", "phosh": "PHOSH",
         "goa-daemon": "ONLINE ACCOUNTS", "valent": "VALENT", "vvmplayer": "VOICEMAIL",
         "crashlog": "CRASHLOG", "glyphd": "GLYPH", "snapshot": "CAMERA", "pipeline": "YOUTUBE",
         "gvoice": "VOICE CALLS", "droid-app": "DROID", "scrcpy": "DROID", "chatty-watchdog": "CHATTY",
         "Xwayland": "XWAYLAND", "maps": "MAPS", "notarealmaps": "MAPS", "karere": "KARERE"}
SHELLS = {"python3", "python", "sh", "ash", "bash", "dash", "busybox", "env", "gjs"}


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def meminfo():
    m = {}
    for line in _read("/proc/meminfo").decode().splitlines():
        k, _, v = line.partition(":")
        m[k] = int(v.split()[0]) if v.split() else 0
    return m


def proc_name(argv, comm):
    """A readable app name for one process."""
    if not argv:
        return NAMES.get(comm, comm.upper())
    exe = os.path.basename(argv[0])
    if exe in SHELLS or exe.startswith("python"):
        for a in argv[1:]:
            if not a.startswith("-"):
                exe = os.path.basename(a)
                break
    if exe.startswith("python"):
        exe = comm
    exe = exe[:-3] if exe.endswith(".py") else exe
    return NAMES.get(exe, NAMES.get(comm, exe.upper()))


def scan():
    """[(name, kib, pids, protected)] biggest first, for this user's processes."""
    uid = os.getuid()
    procs = {}
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        pid = int(p)
        try:
            if os.stat(f"/proc/{p}").st_uid != uid:
                continue
        except OSError:
            continue
        stat = _read(f"/proc/{p}/stat").decode(errors="replace")
        if not stat:
            continue
        comm = stat[stat.find("(") + 1:stat.rfind(")")]
        ppid = int(stat[stat.rfind(")") + 2:].split()[1])
        pss = 0
        for line in _read(f"/proc/{p}/smaps_rollup").decode().splitlines():
            if line.startswith("Pss:"):
                pss = int(line.split()[1])
                break
        argv = [a.decode(errors="replace") for a in _read(f"/proc/{p}/cmdline").split(b"\0") if a]
        procs[pid] = (ppid, comm, pss, argv)

    def parent_comm(ppid):
        if ppid in (0, 1):
            return "init"
        if ppid in procs:
            return procs[ppid][1]
        return _read(f"/proc/{ppid}/comm").decode().strip() or "init"

    def root(pid):
        seen = 0
        while seen < 64:
            ppid = procs[pid][0]
            if ppid not in procs or parent_comm(ppid) in ROOT_PARENTS:
                return pid
            pid, seen = ppid, seen + 1
        return pid

    groups = {}
    for pid, (ppid, comm, pss, argv) in procs.items():
        groups.setdefault(root(pid), []).append(pid)

    rows = []
    for r, pids in groups.items():
        total = sum(procs[p][2] for p in pids)
        if total <= 0:
            continue
        name = proc_name(procs[r][3], procs[r][1])
        if "daemon" in procs[r][3][1:2] and name == "CLAUDE":
            name = "CLAUDE BACKGROUND"
        root_exe = os.path.basename((procs[r][3] or [""])[0])
        if (root_exe in SHELLS and len(pids) > 1 and name not in NAMES.values()):
            big = max(pids, key=lambda p: procs[p][2])  # launcher loop: name it after the worker
            if big != r:
                name = proc_name(procs[big][3], procs[big][1])
        exe = (procs[r][3] or [""])[0]
        protected = (name in PROTECT or r == os.getpid() or os.getpid() in pids
                     or exe.startswith(("/usr/libexec/", "/usr/lib/")) or procs[r][1] in ROOT_PARENTS)
        rows.append((name, total, pids, protected))
    rows.sort(key=lambda x: -x[1])
    return rows


def fmt(kib):
    return f"{kib / 1048576:.1f}G" if kib >= 1024 * 1000 else f"{kib // 1024}M"


class RamPage(Gtk.Box):
    def __init__(self, on_back):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["rampage"])
        top = Gtk.Box(spacing=10)
        back = Gtk.Button(label="‹", css_classes=["chip", "all-back"])   # left: left-handed
        back.connect("clicked", lambda *_: on_back())
        top.append(back)
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        head.append(Gtk.Label(label="RAM", xalign=0, css_classes=["ram-title"]))
        self.summary = Gtk.Label(label="", xalign=0, css_classes=["ram-sum"])
        head.append(self.summary)
        top.append(head)
        self.append(top)
        self.total_bar = Gtk.ProgressBar(css_classes=["ram-bar"])
        self.append(self.total_bar)
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, vexpand=True)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.list)
        self.append(scroll)
        self._timer = 0
        self._busy = False
        self._sure = None        # (name, root pid) waiting for the second tap
        self._sure_id = 0

    def start(self):
        if not self._timer:
            self._refresh()
            self._timer = GLib.timeout_add(REFRESH_MS, self._refresh)

    def stop(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self._sure = None

    def _refresh(self):
        if not self._busy:
            self._busy = True
            threading.Thread(target=self._worker, daemon=True).start()
        return True

    def _worker(self):
        try:
            data = (meminfo(), scan())
        except Exception as e:  # never kill the widget over a vanished /proc entry
            print("ram:", e, flush=True)
            data = None
        GLib.idle_add(self._show, data)

    def _show(self, data):
        self._busy = False
        if data is None or not self._timer:
            return False
        m, rows = data
        total, avail = m.get("MemTotal", 1), m.get("MemAvailable", 0)
        swap = m.get("SwapTotal", 0) - m.get("SwapFree", 0)
        self.summary.set_label(f"USED {fmt(total - avail)} / {fmt(total)} · FREE {fmt(avail)}"
                               f" · SWAP {fmt(swap)}")
        self.total_bar.set_fraction((total - avail) / total)
        while (c := self.list.get_first_child()) is not None:
            self.list.remove(c)
        top = rows[:ROWS]
        biggest = top[0][1] if top else 1
        for name, kib, pids, protected in top:
            self.list.append(self._row(name, kib, pids, protected, kib / biggest))
        return False

    def _row(self, name, kib, pids, protected, frac):
        row = Gtk.Box(spacing=8)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        line = Gtk.Box(spacing=6)
        line.append(Gtk.Label(label=name, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                              css_classes=["ram-name"]))
        line.append(Gtk.Label(label=fmt(kib), css_classes=["ram-size"]))
        col.append(line)
        bar = Gtk.ProgressBar(fraction=frac, css_classes=["ram-bar"])
        col.append(bar)
        n = len(pids)
        col.append(Gtk.Label(label=f"{n} process{'es' if n > 1 else ''}", xalign=0, css_classes=["ram-sub"]))
        row.append(col)
        if protected:
            row.append(Gtk.Label(label="SYSTEM", css_classes=["ram-keep"]))
        else:
            key = (name, min(pids))
            sure = self._sure == key
            btn = Gtk.Button(label="SURE?" if sure else "END", css_classes=["ram-end"] + (["sure"] if sure else []),
                             valign=Gtk.Align.CENTER)
            btn.connect("clicked", lambda b, k=key, p=pids: self._on_end(b, k, p))
            row.append(btn)
        return row

    def _on_end(self, btn, key, pids):
        if self._sure != key:
            self._sure = key
            btn.set_label("SURE?")
            btn.add_css_class("sure")
            if self._sure_id:
                GLib.source_remove(self._sure_id)
            self._sure_id = GLib.timeout_add(CONFIRM_MS, self._unsure)
            return
        self._unsure()
        btn.set_sensitive(False)
        btn.set_label("…")
        end_group(pids)
        GLib.timeout_add(600, lambda: self._refresh() and False)

    def _unsure(self):
        self._sure = None
        if self._sure_id:
            GLib.source_remove(self._sure_id)
            self._sure_id = 0
        self._refresh()
        return False


def end_group(pids):
    me = os.getpid()
    pids = [p for p in pids if p != me]
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass

    def finish():
        for p in pids:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        return False
    GLib.timeout_add(KILL_AFTER_MS, finish)
