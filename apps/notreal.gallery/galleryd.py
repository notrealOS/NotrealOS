#!/usr/bin/env python3
"""galleryd: backs the phone's camera photos/videos up to Google Photos (album "notarealPhone").

Watches the camera folders (and Screenshots if enabled), uploads 60 s after a change and every
30 min, only on unmetered networks when "Wi-Fi only" is on. SIGUSR1 = back up now (the app's
button). Progress for the app: ~/.local/share/gallery/status.json.
"""
import json
import os
import signal
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gi  # noqa: E402
from gi.repository import Gio, GLib  # noqa: E402
import gphotos  # noqa: E402
import media  # noqa: E402

PIDFILE = os.path.join(gphotos.DATA, "galleryd.pid")
STATUS = os.path.join(gphotos.DATA, "status.json")
SETTLE_S = 60
PERIOD_S = 30 * 60
MAX_SIZE = 2 * 1024 ** 3          # skip anything over 2 GB


def write_status(**kw):
    kw["t"] = int(time.time())
    os.makedirs(gphotos.DATA, exist_ok=True)
    with open(STATUS + ".tmp", "w") as f:
        json.dump(kw, f)
    os.replace(STATUS + ".tmp", STATUS)


class Daemon:
    def __init__(self):
        self.busy = False
        self.pending = None
        self.monitors = []
        self.loop = GLib.MainLoop()
        for folder, album, _rec in media.SOURCES:
            if album in ("camera", "screens") and os.path.isdir(folder):
                m = Gio.File.new_for_path(folder).monitor_directory(Gio.FileMonitorFlags.NONE, None)
                m.connect("changed", self.on_change)
                self.monitors.append(m)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self.now)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self.quit)
        GLib.timeout_add_seconds(PERIOD_S, self.periodic)
        GLib.timeout_add_seconds(20, self.periodic_once)

    def quit(self):
        self.loop.quit()
        return False

    def on_change(self, _m, _f, _o, event):
        if event in (Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.CREATED,
                     Gio.FileMonitorEvent.MOVED_IN):
            if self.pending:
                GLib.source_remove(self.pending)
            self.pending = GLib.timeout_add_seconds(SETTLE_S, self.settled)

    def settled(self):
        self.pending = None
        self.start(False)
        return False

    def periodic(self):
        self.start(False)
        return True

    def periodic_once(self):
        self.start(False)
        return False

    def now(self):
        self.start(True)
        return True

    def start(self, forced):
        if self.busy:
            return
        self.busy = True
        threading.Thread(target=self.run, args=(forced,), daemon=True).start()

    def run(self, forced):
        try:
            self._run(forced)
        except gphotos.AuthError as e:
            write_status(state="error", text=str(e))
        except Exception as e:
            traceback.print_exc()
            write_status(state="error", text=f"backup failed: {e}")
        finally:
            self.busy = False

    def _run(self, forced):
        s = media.settings()
        if not s.get("backup") or not gphotos.signed_in():
            write_status(state="off", text="backup off" if gphotos.signed_in() else "not signed in")
            return
        st = gphotos.load_state()
        todo = [p for p, _a, _m in media.backup_candidates(s)
                if not gphotos.is_backed_up(st, p) and os.path.getsize(p) <= MAX_SIZE]
        if not todo:
            write_status(state="done", text="all backed up", left=0)
            return
        if not gphotos.online_ok(s.get("wifi_only", True) and not forced):
            write_status(state="waiting", text=f"{len(todo)} waiting for Wi-Fi", left=len(todo))
            return
        # oldest first so an interrupted run keeps the order of the camera roll
        todo.sort(key=lambda p: os.path.getmtime(p))
        for i, p in enumerate(todo):
            write_status(state="uploading", text=f"backing up {i + 1}/{len(todo)}", left=len(todo) - i)
            try:
                gphotos.upload(p, media.mime(p), st)
            except gphotos.AuthError:
                raise
            except Exception as e:
                print("upload failed:", p, e, file=sys.stderr, flush=True)
                continue
            gphotos.save_state(st)
        left = sum(1 for p in todo if not gphotos.is_backed_up(st, p))
        write_status(state="done" if not left else "error",
                     text="all backed up" if not left else f"{left} failed, will retry", left=left)


def main():
    os.makedirs(gphotos.DATA, exist_ok=True)
    try:   # one instance: an older one keeps running
        old = int(open(PIDFILE).read())
        os.kill(old, 0)
        if open(f"/proc/{old}/cmdline").read().find("galleryd") >= 0:
            print("galleryd already running", old)
            return
    except (OSError, ValueError):
        pass
    open(PIDFILE, "w").write(str(os.getpid()))
    d = Daemon()
    d.loop.run()


if __name__ == "__main__":
    main()
