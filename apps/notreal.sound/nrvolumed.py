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

10-24 OUTPUT + FOLLOW:
- the DAC / SPEAKER choice (nroutput.py) is applied again whenever sinks come or go.
- the volume keys only move the default sink. When nothing plays there but something plays on
  another sink (HI-FI straight to the DAC with SPEAKER chosen, a stream that wouldn't move),
  the default sink takes that sink's volume and every change is passed on to it, so the keys,
  Phosh's OSD and the Sound app all show and change what you hear. The default sink gets its
  own volume back when that ends (kept in FOLLOW for a crash).
"""

import json
import os
import signal
import sys
import time

import glob

import pulsectl

import nroutput

CONF = os.path.expanduser("~/.config/nrvolume.json")
STATE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "nrvolumed.state")
ROLES = ("event", "notification", "ringtone")
RESTORE_KEYS = ["sink-input-by-media-role:" + r for r in ("event", "notification", "Ringtone")]
SETTLE = 0.3          # wait this long after the last event stream before undoing a raise
EPS = 0.006
FOLLOW = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "nrvolumed.follow")


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
        self.sinks_seen = None   # sink names last time: a change = a DAC came or went
        self.follow = None       # {"from": default sink, "orig": its volume, "mute", "to": playing sink, "last": vol}

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

    # --- output choice + follow ------------------------------------------------------------
    def sinks_changed(self):
        names = tuple(sorted(x.name for x in self.p.sink_list()))
        if names != self.sinks_seen:
            first = self.sinks_seen is None
            self.sinks_seen = names
            if nroutput.apply(self.p):
                log("output -> %s (%s chosen)" % (self.p.server_info().default_sink_name, nroutput.prefer()))
            return not first
        return False

    @staticmethod
    def hifi_open():
        """notarealPlayer's HI-FI holds the DAC's ALSA device itself (its pulse sink stays suspended)."""
        for st in glob.glob("/proc/asound/*/pcm*p/sub*/status"):
            card = st.split("/")[3]
            if card.startswith("card") or not os.path.exists("/proc/asound/%s/usbid" % card):
                continue
            try:
                with open(st) as f:
                    if f.read().strip() != "closed":
                        return True
            except OSError:
                pass
        return False

    def playing_elsewhere(self, s):
        """Name of the sink you hear while the default sink s is silent, else None."""
        if s.state == "running":
            return None
        hifi = self.hifi_open()
        if hifi and nroutput.is_dac(s.name):
            return None
        for x in self.p.sink_list():
            if x.index != s.index and x.state == "running":
                return x.name
        if hifi:
            dac = [x.name for x in self.p.sink_list() if nroutput.is_dac(x.name)]
            return dac[0] if dac else None
        return None

    def save_follow(self):
        try:
            if self.follow:
                with open(FOLLOW, "w") as f:
                    json.dump(self.follow, f)
            elif os.path.exists(FOLLOW):
                os.unlink(FOLLOW)
        except OSError:
            pass

    def end_follow(self):
        f, self.follow = self.follow, None
        try:
            s = self.sink(f["from"])
            self.set_vol(s, f["orig"], sink=True)
            self.p.sink_mute(s.index, f["mute"])
        except pulsectl.PulseIndexError:
            pass
        self.save_follow()
        log("follow %s ended, %s back to %.0f%%" % (f["to"], f["from"], f["orig"] * 100))

    def step_follow(self, s):
        to = self.playing_elsewhere(s)
        if self.follow and (to != self.follow["to"] or s.name != self.follow["from"]):
            self.end_follow()
            s = self.sink()
        if not to:
            return
        try:
            t = self.sink(to)
        except pulsectl.PulseIndexError:
            return
        if not self.follow:
            self.follow = {"from": s.name, "orig": avg(s.volume), "mute": bool(s.mute), "to": to,
                           "last": avg(t.volume), "last_mute": bool(t.mute)}
            self.save_follow()
            self.set_vol(s, avg(t.volume), sink=True)
            self.p.sink_mute(s.index, bool(t.mute))
            log("follow: keys now move %s (%.0f%%)" % (to, avg(t.volume) * 100))
            return
        f = self.follow
        dv, tv = avg(s.volume), avg(t.volume)
        if abs(dv - f["last"]) > EPS or bool(s.mute) != f["last_mute"]:     # keys / slider
            self.set_vol(t, dv, sink=True)
            self.p.sink_mute(t.index, bool(s.mute))
            f["last"], f["last_mute"] = dv, bool(s.mute)
        elif abs(tv - f["last"]) > EPS or bool(t.mute) != f["last_mute"]:   # changed on the DAC itself
            self.set_vol(s, tv, sink=True)
            self.p.sink_mute(s.index, bool(t.mute))
            f["last"], f["last_mute"] = tv, bool(t.mute)

    # --- main step ------------------------------------------------------------------------
    def step(self):
        if self.sinks_changed() and self.follow:
            self.end_follow()
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
        if not self.raised and not events:
            self.step_follow(s)
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
    """Undo a raise / follow left behind by a crash."""
    try:
        with open(FOLLOW) as f:
            fo = json.load(f)
        d = Daemon(pulse)
        d.follow = fo
        d.end_follow()
    except (OSError, ValueError, KeyError, pulsectl.PulseError):
        pass
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
            try:
                if d.raised:
                    d.lower()
                if d.follow:
                    d.end_follow()
            except pulsectl.PulseError:
                pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
