#!/usr/bin/env python3
"""nrvolumed: a notification volume separate from the media volume (10-13).

MEDIA volume M = the default sink's volume, exactly as before (Phosh slider, volume keys).
NOTIFICATION volume N = ~/.config/nrvolume.json {"notify": 0..1}, set by nrvolume.py.
Notification sounds are the streams PulseAudio tags media.role=event (feedbackd /
libcanberra: messages, mail, ringtone) plus the roles in ROLES.

The sink volume scales every stream, so:
- N <= M: the event streams play at N/M. Written into module-stream-restore's role entries,
  so it applies from the stream's first sample (and set again on the stream to be sure).
- N > M: while an event stream plays, the sink goes up to N and every other stream on it
  down by M/N (media loudness unchanged); undone when the last event stream ends.
No stream is ever set above 100 %, so nothing clips.
State of a raise is kept in STATE so a crash mid-notification can be undone at the next start.
"""

import json
import os
import signal
import sys
import time

import pulsectl

CONF = os.path.expanduser("~/.config/nrvolume.json")
STATE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "nrvolumed.state")
ROLES = ("event", "notification", "ringtone")
RESTORE_KEYS = ["sink-input-by-media-role:" + r for r in ("event", "notification", "Ringtone")]
SETTLE = 0.3          # wait this long after the last event stream before undoing a raise
EPS = 0.006


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def read_notify():
    try:
        with open(CONF) as f:
            return max(0.0, min(1.0, float(json.load(f).get("notify", 0.6))))
    except (OSError, ValueError, TypeError):
        return 0.6


def is_event(si):
    return (si.proplist.get("media.role") or "").lower() in ROLES


def avg(v):
    return v.value_flat if v.values else 0.0


class Daemon:
    def __init__(self, pulse):
        self.p = pulse
        self.n = read_notify()
        self.conf_mtime = self._mtime()
        self.rel = None          # last value written to stream-restore
        self.raised = None       # {"sink": name, "m": M, "mute": bool, "others": {idx: [vols]}}
        self.idle_since = None   # time the last event stream ended during a raise
        self.saved = None        # during a raise: stream-restore entries before we scaled
        self.dirty = True

    def _mtime(self):
        try:
            return os.stat(CONF).st_mtime
        except OSError:
            return 0

    # --- helpers ------------------------------------------------------------------------
    def sink(self, name=None):
        name = name or os.environ.get("NRVOL_SINK") or self.p.server_info().default_sink_name
        return self.p.get_sink_by_name(name)

    def media(self, s):
        """M for sink s (0 when muted), ignoring our own raise."""
        if self.raised and self.raised["sink"] == s.name:
            return 0.0 if self.raised["mute"] else self.raised["m"]
        return 0.0 if s.mute else avg(s.volume)

    def set_vol(self, obj, v, sink=False):
        vol = pulsectl.PulseVolumeInfo(max(0.0, min(1.0, v)), len(obj.volume.values) or 2)
        (self.p.sink_volume_set if sink else self.p.sink_input_volume_set)(obj.index, vol)

    def write_restore(self, rel):
        if self.rel is not None and abs(rel - self.rel) < EPS:
            return
        for key in RESTORE_KEYS:
            e = pulsectl.PulseExtStreamRestoreInfo(key, [rel, rel], ["front-left", "front-right"], mute=False)
            self.p.stream_restore_write(e, mode="replace", apply_immediately=False)
        self.rel = rel
        log("notification streams = %.0f%% of the sink (N %.0f%%)" % (rel * 100, self.n * 100))

    def save_state(self):
        try:
            if self.raised:
                with open(STATE, "w") as f:
                    json.dump(self.raised, f)
            elif os.path.exists(STATE):
                os.unlink(STATE)
        except OSError:
            pass

    # --- raise / lower ------------------------------------------------------------------
    def raise_for(self, s, m):
        others = {}
        self.saved = {}
        for si in self.p.sink_input_list():
            if si.sink != s.index:
                continue
            if is_event(si):
                self.set_vol(si, 1.0)
            else:
                others[si.index] = list(si.volume.values)
                self.scale(si, m / self.n if self.n else 0.0)
        self.raised = {"sink": s.name, "m": avg(s.volume), "mute": bool(s.mute), "others": others,
                       "target": self.n, "saved": self.saved}
        self.save_state()
        if s.mute:
            self.p.sink_mute(s.index, False)
        self.set_vol(s, self.n, sink=True)
        log("raise %s to %.0f%% (media %.0f%%)" % (s.name, self.n * 100, m * 100))

    def scale(self, si, f):
        # module-stream-restore saves the scaled volume as the app's remembered volume;
        # keep the real one so lower() can put it back (also for streams that end meanwhile)
        key = si.proplist.get("module-stream-restore.id")
        if key and self.saved is not None and key not in self.saved:
            # no usable entry yet: the stream's own volume is what should be remembered
            self.saved[key] = [list(si.volume.values), [str(c) for c in si.channel_list], bool(si.mute)]
            for e in self.p.stream_restore_list():
                if e.name == key and e.volume.values:
                    self.saved[key] = [list(e.volume.values), [str(c) for c in e.channel_list], bool(e.mute)]
        vals = [v * f for v in si.volume.values]
        self.p.sink_input_volume_set(si.index, pulsectl.PulseVolumeInfo(vals))

    def lower(self):
        r, self.raised = self.raised, None
        self.idle_since = None
        try:
            s = self.sink(r["sink"])
        except pulsectl.PulseIndexError:
            s = None
        if s:
            v = pulsectl.PulseVolumeInfo(r["m"], len(s.volume.values) or 2)
            self.p.sink_volume_set(s.index, v)
            if r["mute"]:
                self.p.sink_mute(s.index, True)
        for si in self.p.sink_input_list():
            vals = r["others"].get(si.index) or r["others"].get(str(si.index))
            if vals:
                self.p.sink_input_volume_set(si.index, pulsectl.PulseVolumeInfo(vals))
        for key, (vals, chans, mute) in (r.get("saved") or {}).items():
            if vals:
                e = pulsectl.PulseExtStreamRestoreInfo(key, vals, chans, mute=mute)
                self.p.stream_restore_write(e, mode="replace", apply_immediately=False)
        self.saved = None
        self.save_state()
        log("lowered back to %.0f%%" % (r["m"] * 100))

    # --- main step ------------------------------------------------------------------------
    def step(self):
        mt = self._mtime()
        if mt != self.conf_mtime:
            self.conf_mtime, self.n = mt, read_notify()
            self.dirty = True
        s = self.sink()

        if self.raised:
            if self.raised["sink"] != s.name:
                self.lower()
            elif abs(avg(s.volume) - self.raised["target"]) > EPS or s.mute:
                # the user moved the media volume mid-notification: that is the new M
                m, mute = avg(s.volume), bool(s.mute)
                self.raised["m"], self.raised["mute"] = m, mute
                self.lower()
                log("media volume changed during a notification -> %.0f%%" % (m * 100))
            s = self.sink()

        m = self.media(s)
        self.write_restore(min(1.0, self.n / m) if m > 0 else 1.0)

        events = [si for si in self.p.sink_input_list() if si.sink == s.index and is_event(si)]
        if self.raised:
            # streams that appeared during the raise get scaled down too
            for si in self.p.sink_input_list():
                if si.sink == s.index and not is_event(si) and si.index not in self.raised["others"]:
                    self.raised["others"][si.index] = list(si.volume.values)
                    t = self.raised["target"]
                    self.scale(si, m / t if t else 0.0)
            if events:
                self.idle_since = None
            elif self.idle_since is None:
                self.idle_since = time.monotonic()
            elif time.monotonic() - self.idle_since >= SETTLE:
                self.lower()
        elif events:
            if self.n > m + EPS:
                self.raise_for(s, m)
            else:
                for si in events:
                    want = self.n / m if m else 0.0
                    if abs(avg(si.volume) - want) > EPS:
                        self.set_vol(si, want)


def recover(pulse):
    """Undo a raise left behind by a crash."""
    try:
        with open(STATE) as f:
            r = json.load(f)
    except (OSError, ValueError):
        return
    d = Daemon(pulse)
    d.raised = r
    d.saved = r.get("saved")
    try:
        d.lower()
    except pulsectl.PulseError as e:
        log("recover failed:", e)
    log("recovered a leftover raise")


def main():
    with pulsectl.Pulse("nrvolumed") as pulse:
        recover(pulse)
        d = Daemon(pulse)
        pulse.event_mask_set("sink", "sink_input", "server")
        def wake(ev):
            raise pulsectl.PulseLoopStop
        pulse.event_callback_set(wake)
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
        log("running, notifications at %.0f%%" % (d.n * 100))
        try:
            while True:
                try:
                    d.step()
                except pulsectl.PulseIndexError:
                    pass  # a stream vanished while we looked at it
                # wake on any sink / stream event, at least every 0.1 s while raised, else 1 s
                pulse.event_listen(timeout=0.1 if d.raised else 1.0)
        finally:
            if d.raised:
                try:
                    d.lower()
                except pulsectl.PulseError:
                    pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
