"""GStreamer playback: queue, gapless, smart crossfade, ReplayGain, EQ, direct (bit-perfect) ALSA output.

Crossfade uses two playbins ("decks"): near the end of a song the next one is prerolled on a second
deck, then started while the first fades out (equal-power curves). self.pb is always the deck of the
current song; the fading-out one lives in self.xf until it is silent and dropped.
Smart parts: next track on the same album -> stays gapless; an outro that goes quiet starts the fade
early; a silent intro on the next song holds the fade until it is audible; skips get a 1 s fade;
short tracks and HI-FI (direct ALSA) never crossfade."""
import json
import math
import re
import os
import time
import random
import subprocess
import threading
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GObject, GLib

from store import DATA_DIR, QUALITY, metered

STATE = os.path.join(DATA_DIR, "queue.json")

Gst.init(None)

MAX_FAILS = 3         # stop after this many songs in a row fail to play
SKIP_FADE = 1.0       # s, crossfade when skipping / picking another song while playing
PREROLL_S = 12        # start buffering the next song this long before the fade
OUTRO_WINDOW = 25     # s before the end where a quiet outro may start the fade early
QUIET_SAMPLES = 20    # 2 s of level messages (100 ms) that must all be quiet
INTRO_WAIT_S = 4.0    # longest the fade waits for a silent intro to become audible

PLAY_FLAG_AUDIO = 0x2
PLAY_FLAG_SOFT_VOLUME = 0x10

EQ_FREQS = ["31", "62", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]
EQ_PRESETS = {
    "Flat": [0] * 10,
    "Bass boost": [6, 5, 4, 2, 0, 0, 0, 0, 0, 0],
    "Warm": [3, 3, 2, 1, 0, -1, -1, -2, -2, -3],
    "V-shape": [5, 4, 2, 0, -2, -2, 0, 2, 4, 5],
    "Vocal": [-2, -2, -1, 1, 3, 3, 2, 1, 0, -1],
    "Treble": [0, 0, 0, 0, 0, 1, 2, 4, 5, 6],
    "Harman IEM": [4, 4, 3, 1, -1, -1, 0, 2, 1, -2],
}


def usb_cards():
    """[(alsa_device, label)] for USB DACs."""
    out = []
    try:
        with open("/proc/asound/cards") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for i, line in enumerate(lines):
        if "USB-Audio" in line and "[" in line:
            card_id = line.split("[")[1].split("]")[0].strip()
            label = lines[i + 1].strip().split(" at ")[0] if i + 1 < len(lines) else card_id
            out.append((f"hw:CARD={card_id},DEV=0", label))
    return out


def pulse_sink_for(device):
    """PulseAudio sink name backed by the same USB card (to suspend it while we own the DAC)."""
    card_id = device.split("CARD=")[1].split(",")[0] if "CARD=" in device else ""
    try:
        sinks = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True,
                               timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in sinks.splitlines():
        name = line.split("\t")[1] if "\t" in line else ""
        if name.startswith("alsa_output.usb") and card_id.lower() in name.lower().replace("-", ""):
            return name
    for line in sinks.splitlines():
        name = line.split("\t")[1] if "\t" in line else ""
        if name.startswith("alsa_output.usb"):
            return name
    return None


class Player(GObject.Object):
    __gsignals__ = {
        "track-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "position": (GObject.SignalFlags.RUN_FIRST, None, (float, float)),
        "queue-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "format": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "auto-queued": (GObject.SignalFlags.RUN_FIRST, None, (int, str)),
    }

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.s = store.s
        self.queue = []
        self.original = None          # unshuffled queue
        self.index = -1
        self._pulse_retry_at = 0.0
        self.pending = None           # index queued via about-to-finish
        self.playing = False
        self.scrobbled = False
        self.suspended_sink = None
        self.autoq_busy = False
        self.refill = None            # play_tracks(refill=...): endless pools
        self.fails = 0                # songs in a row that failed to start (error loop guard)
        self.loaded = False           # uri set on playbin (False after a restored session)
        self.resume_at = 0.0
        self.saved_at = 0
        self.xf = None                # active crossfade: {"old": deck, "len", "t", "last", "waited"}
        self.next_deck = None         # prerolled deck for the next song
        self.next_id = None           # (index, track id) it was prepared for
        self.levels = []              # RMS dB of the current song, ~10/s (outro detection)
        self.in_level = -100.0        # RMS of the incoming song during a crossfade
        self.pb = None
        self.pb = self._new_deck()
        GLib.timeout_add(500, self._tick)

    # --- decks -----------------------------------------------------------
    def _new_deck(self):
        pb = Gst.ElementFactory.make("playbin")
        pb.connect("about-to-finish", self._about_to_finish)
        bus = pb.get_bus()
        bus.add_signal_watch()
        for name, fn in (("eos", self._on_eos), ("error", self._on_error),
                         ("stream-start", self._on_stream_start), ("async-done", self._on_async_done)):
            # only the current deck drives the player; a fading or prerolling deck stays quiet
            bus.connect("message::" + name, lambda b, m, fn=fn, pb=pb: fn(b, m) if pb is self.pb else None)
        bus.connect("message::element", lambda b, m, pb=pb: self._on_level(pb, m))
        pb.set_property("audio-sink", self.build_output(pb))
        return pb

    @staticmethod
    def _drop_deck(pb):
        if pb is not None:
            pb.set_state(Gst.State.NULL)
            pb.get_bus().remove_signal_watch()

    def _decks(self):
        return [d for d in (self.pb, self.next_deck, self.xf and self.xf["old"]) if d is not None]

    # --- output chain --------------------------------------------------
    def build_output(self, pb):
        s = self.s
        direct = s["output"] == "direct" and s["direct_device"]
        bin_ = Gst.Bin.new()
        chain = []
        pb._eq = None
        if direct:
            chain.append(Gst.ElementFactory.make("audioconvert"))
            sink = Gst.ElementFactory.make("alsasink")
            sink.set_property("device", s["direct_device"])
            chain.append(sink)
            pb.set_property("flags", PLAY_FLAG_AUDIO)
            self._suspend_pulse(s["direct_device"])
        else:
            self._suspend_pulse(None)
            if s["replaygain"] != "off":
                rg = Gst.ElementFactory.make("rgvolume")
                rg.set_property("album-mode", s["replaygain"] == "album")
                rg.set_property("pre-amp", float(s["rg_preamp"]))
                chain += [rg, Gst.ElementFactory.make("rglimiter")]
            chain.append(Gst.ElementFactory.make("audioconvert"))
            level = Gst.ElementFactory.make("level")       # loudness for the smart crossfade
            if level:
                level.set_property("interval", 100 * Gst.MSECOND)
                level.set_property("post-messages", True)
                chain.append(level)
            if s["eq_enabled"]:
                pb._eq = Gst.ElementFactory.make("equalizer-10bands")
                chain += [pb._eq, Gst.ElementFactory.make("audioconvert")]
            chain += [Gst.ElementFactory.make("audioresample"), Gst.ElementFactory.make("pulsesink")]
            chain[-1].set_property("client-name", "notreal.player")
            pb.set_property("flags", PLAY_FLAG_AUDIO | PLAY_FLAG_SOFT_VOLUME)
            pb.set_property("volume", s["volume"])
        for e in chain:
            bin_.add(e)
        for a, b in zip(chain, chain[1:]):
            a.link(b)
        bin_.add_pad(Gst.GhostPad.new("sink", chain[0].get_static_pad("sink")))
        pb._sink = chain[-1]
        self._apply_eq_to(pb)
        return bin_

    def _suspend_pulse(self, device):
        """Free the DAC from PulseAudio while playing bit-perfect, give it back otherwise."""
        want = pulse_sink_for(device) if device else None
        if self.suspended_sink and self.suspended_sink != want:
            subprocess.run(["pactl", "suspend-sink", self.suspended_sink, "0"], timeout=3)
            self.suspended_sink = None
        if want and want != self.suspended_sink:
            subprocess.run(["pactl", "suspend-sink", want, "1"], timeout=3)
            self.suspended_sink = want

    def apply_output(self):
        """Rebuild the sink chain after a settings change and resume where we were."""
        self._finish_xf()
        self._drop_next()
        pos = self.position()
        was_playing = self.playing
        self.pb.set_state(Gst.State.NULL)
        self.pb.set_property("audio-sink", self.build_output(self.pb))
        if 0 <= self.index < len(self.queue):
            self._load(self.index, start=False)
            self.pb.set_state(Gst.State.PAUSED)
            self.pb.get_state(5 * Gst.SECOND)
            if pos > 0:
                self.pb.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                                    int(pos * Gst.SECOND))
            if was_playing:
                self.pb.set_state(Gst.State.PLAYING)

    @property
    def sink(self):
        return getattr(self.pb, "_sink", None) if self.pb is not None else None

    @property
    def eq(self):
        return getattr(self.pb, "_eq", None) if self.pb is not None else None

    def _apply_eq_to(self, pb):
        eq = getattr(pb, "_eq", None)
        if eq:
            for i, g in enumerate(self.s["eq_bands"]):
                eq.set_property(f"band{i}", float(g))

    def apply_eq(self):
        for pb in self._decks():
            self._apply_eq_to(pb)

    def set_volume(self, v):
        self.s.set("volume", v)
        if self.s["output"] != "direct" and not self.xf:   # during a fade _xf_step applies it
            self.pb.set_property("volume", v)

    # --- queue -----------------------------------------------------------
    @property
    def current(self):
        return self.queue[self.index] if 0 <= self.index < len(self.queue) else None

    def play_tracks(self, tracks, start=None, shuffle=None, refill=None):
        """start=None: first track (or a random one when shuffling).
        refill: callable -> more tracks, used instead of AudioMuse auto-queue when the queue runs low
        (shuffle-all / smart playlists that are bigger than one queue)."""
        tracks = list(tracks)
        if not tracks:
            return
        self.refill = refill
        if shuffle is None:
            shuffle = self.s["shuffle"]
        else:
            self.s.set("shuffle", shuffle)
        self.original = None
        if shuffle:
            first = tracks.pop(random.randrange(len(tracks)) if start is None else start)
            self.original = [first] + tracks
            random.shuffle(tracks)
            tracks.insert(0, first)
            start = 0
        self.queue = tracks
        self.emit("queue-changed")
        self.play_index(start or 0)

    def play_next(self, tracks):
        at = self.index + 1
        self.queue[at:at] = tracks
        self.emit("queue-changed")
        if self.index < 0:
            self.play_index(0)

    def replace_upcoming(self, tracks):
        """Keep the current song playing; everything after it becomes `tracks`."""
        self.queue = self.queue[:self.index + 1] + list(tracks)
        self.original = None
        self.pending = None
        self.emit("queue-changed")
        self.store.prefetch(self.queue[self.index + 1:])
        self.save_state()

    def add_to_queue(self, tracks):
        self.queue.extend(tracks)
        self.emit("queue-changed")
        if self.index < 0:
            self.play_index(0)

    def remove_at(self, i):
        if i == self.index or not 0 <= i < len(self.queue):
            return
        del self.queue[i]
        if i < self.index:
            self.index -= 1
        self.emit("queue-changed")

    def toggle_shuffle(self):
        on = not self.s["shuffle"]
        self.s.set("shuffle", on)
        cur = self.current
        if on and self.queue:
            self.original = list(self.queue)
            rest = self.queue[self.index + 1:] + self.queue[:self.index]
            random.shuffle(rest)
            self.queue = ([cur] if cur else []) + rest
            self.index = 0 if cur else -1
        elif self.original:
            self.queue = self.original
            self.index = next((i for i, t in enumerate(self.queue) if cur and t["id"] == cur["id"]), 0)
            self.original = None
        self.pending = None
        self.emit("queue-changed")
        self.emit("state-changed")

    def cycle_repeat(self):
        nxt = {"off": "all", "all": "one", "one": "off"}[self.s["repeat"]]
        self.s.set("repeat", nxt)
        self.emit("state-changed")

    def _next_index(self, auto):
        if not self.queue:
            return None
        if auto and self.s["repeat"] == "one":
            return self.index
        i = self.index + 1
        if i >= len(self.queue):
            return 0 if self.s["repeat"] == "all" else None
        return i

    # --- transport -------------------------------------------------------
    def _uri(self, track):
        path = self.store.local_path(track["id"])
        if path:
            return Gst.filename_to_uri(path)
        if not self.store.api:
            return None
        _label, fmt, br = QUALITY[self.s["quality_mobile" if metered() else "quality_wifi"]]
        return self.store.api.stream_url(track["id"], fmt, br)

    def _load(self, i, start=True):
        self.index = i
        self.pending = None
        self.scrobbled = False
        track = self.queue[i]
        uri = self._uri(track)
        self.pb.set_state(Gst.State.READY)
        self.pb.set_property("uri", uri or "")
        self.loaded = True
        self.resume_at = 0.0
        if start:
            self.pb.set_state(Gst.State.PLAYING)
            self.playing = True
        self._track_started()

    def play_index(self, i):
        if 0 <= i < len(self.queue):
            if self.playing and self.loaded and self._can_crossfade():
                self._start_xf(i, SKIP_FADE)        # skip / tap another song: short crossfade
                return
            self._load(i)
            self.emit("state-changed")

    def _track_started(self):
        self.levels = []
        track = self.current
        if not track:
            return
        self.store.played(track)
        self.store.prefetch(self.queue[self.index + 1:])
        self.store.scrobble(track["id"], False)
        self.emit("track-changed")
        self.save_state()
        self._maybe_autoqueue()

    def _maybe_autoqueue(self):
        """Near the end of the queue, append AudioMuse songs like the current one (Navidrome
        getSimilarSongs2), so the music doesn't just stop."""
        if self.refill and not self.autoq_busy and len(self.queue) - self.index - 1 <= 5:
            return self._refill()
        if (not self.s["auto_queue"] or self.s["repeat"] != "off" or self.autoq_busy
                or not self.store.api or len(self.queue) - self.index - 1 > 1):
            return
        seed = self.current
        if not seed:
            return
        self.autoq_busy = True

        def run():
            try:
                songs = self.store.api.similar(seed["id"], 40)
            except Exception as e:  # noqa: BLE001 - offline etc.: just don't extend
                print("auto-queue failed:", e)
                songs = []
            GLib.idle_add(done, songs)

        def done(songs):
            self.autoq_busy = False
            have = {t["id"] for t in self.queue[-200:]}
            new = [t for t in songs if t["id"] not in have][:25]
            if new:
                for t in new:
                    t["autoQueued"] = True
                self.queue.extend(new)
                self.emit("queue-changed")
                self.store.prefetch(self.queue[self.index + 1:])
                self.save_state()
                self.emit("auto-queued", len(new), seed.get("title", ""))
            return False
        threading.Thread(target=run, daemon=True).start()

    def _refill(self):
        fn = self.refill
        self.autoq_busy = True

        def run():
            try:
                songs = fn()
            except Exception as e:  # noqa: BLE001
                print("refill failed:", e)
                songs = []
            GLib.idle_add(done, songs)

        def done(songs):
            self.autoq_busy = False
            if songs and self.refill is fn:
                self.queue.extend(songs)
                self.emit("queue-changed")
                self.store.prefetch(self.queue[self.index + 1:])
                self.save_state()
            return False
        threading.Thread(target=run, daemon=True).start()

    def play(self):
        if self.index < 0 and self.queue:
            return self.play_index(0)
        if not self.loaded and self.current:     # first play after a restored session
            resume = self.resume_at
            self._load(self.index)
            self.resume_at = resume
            self.emit("state-changed")
            return
        if self.s["output"] == "direct":
            self._suspend_pulse(self.s["direct_device"])
        self.pb.set_state(Gst.State.PLAYING)
        self.playing = True
        self.emit("state-changed")

    def pause(self):
        self._finish_xf()
        self.pb.set_state(Gst.State.PAUSED)
        self.playing = False
        self.emit("state-changed")
        self.save_state()
        if self.s["output"] == "direct":
            GLib.timeout_add_seconds(20, self._release_dac)

    def stop(self):
        """Pause and hand the DAC back to the system right away (voice assistant, calls)."""
        self.pause()
        self._release_dac()

    def _release_dac(self):
        """Paused in bit-perfect mode: close the DAC so system sounds can use it again."""
        if not self.playing and self.loaded and self.s["output"] == "direct":
            self.resume_at = self.position()
            self.pb.set_state(Gst.State.NULL)
            self.loaded = False
            self._suspend_pulse(None)
        return False

    def toggle(self):
        self.pause() if self.playing else self.play()

    def next(self):
        i = self._next_index(auto=False)
        if i is None:
            i = 0 if self.queue else None
        if i is not None:
            self.play_index(i)

    def previous(self):
        if self.position() > 4 or self.index <= 0:
            self.seek(0)
        else:
            self.play_index(self.index - 1)

    def seek(self, secs):
        self.pb.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                            int(max(0, secs) * Gst.SECOND))

    def position(self):
        ok, pos = self.pb.query_position(Gst.Format.TIME)
        return pos / Gst.SECOND if ok else 0.0

    def duration(self):
        t = self.current
        if t and t.get("duration"):
            return float(t["duration"])
        ok, dur = self.pb.query_duration(Gst.Format.TIME)
        return dur / Gst.SECOND if ok and dur > 0 else 0.0

    # --- gst callbacks -----------------------------------------------------
    def _about_to_finish(self, pb):
        # streaming thread: queue the next uri for gapless playback
        if pb is not self.pb:
            return
        i = self._next_index(auto=True)
        if i is None:
            return
        if self._xf_length(self.current, self.queue[i]):
            return                      # the crossfade starts the next song on the other deck
        uri = self._uri(self.queue[i])
        if uri:
            pb.set_property("uri", uri)
            self.pending = i

    def _on_stream_start(self, *_):
        self.fails = 0
        if self.pending is not None:
            self.index, self.pending = self.pending, None
            self.scrobbled = False
            self._track_started()
        self._emit_format()

    def _on_async_done(self, *_):
        if self.resume_at:
            pos, self.resume_at = self.resume_at, 0.0
            self.seek(pos)
        self._emit_format()

    # --- session persistence -------------------------------------------------
    def save_state(self):
        try:
            tmp = STATE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"queue": self.queue, "original": self.original, "index": self.index,
                       "playing": self.playing,
                           "position": self.position() if self.loaded else self.resume_at}, f)
            os.replace(tmp, STATE)
        except OSError as e:
            print("save queue failed", e)

    def restore_state(self):
        try:
            with open(STATE) as f:
                st = json.load(f)
        except (OSError, ValueError):
            return
        self.queue, self.original = st.get("queue") or [], st.get("original")
        self.index = st.get("index", -1) if 0 <= st.get("index", -1) < len(self.queue) else -1
        self.resume_at = float(st.get("position") or 0)
        if self.current:
            self.emit("queue-changed")
            self.emit("track-changed")
            self.emit("position", self.resume_at, self.duration())
            self.emit("state-changed")
            if st.get("playing") and os.environ.pop("SPIN_AUTOPLAY", None):
                self.play()

    def _on_eos(self, *_):
        i = self._next_index(auto=True)
        if i is not None and self.current and self._xf_length(self.current, self.queue[i]):
            self._load(i)               # the fade never started (no position?): don't just stop
            self.emit("state-changed")
            return
        self.playing = False
        self.pb.set_state(Gst.State.READY)
        self.emit("state-changed")

    def _on_error(self, _bus, msg):
        err, dbg = msg.parse_error()
        print("gst error:", err.message, dbg)
        self.emit("error", err.message)
        if self.s["output"] == "direct" and "alsa" in (dbg or "").lower():
            # DAC gone or busy: fall back to the system mixer
            self.s.set("output", "pulse")
            self.emit("error", "DAC unavailable, switched to system output")
            GLib.idle_add(lambda: (self.apply_output(), False)[1])
            return
        if self.s["output"] != "direct" and self._is_pulse_loss(err, dbg):
            # PulseAudio restarted (or its socket vanished): the old pulsesink is dead for good.
            # Rebuild the output chain (a fresh pulsesink reconnects) and resume the same track.
            now = time.monotonic()
            if now - self._pulse_retry_at > 5:
                self._pulse_retry_at = now
                print("pulse connection lost: rebuilding output", flush=True)
                GLib.timeout_add(1000, lambda: (self.apply_output(), False)[1])
                return
        if self.playing:
            self.fails += 1
            if self.fails >= MAX_FAILS:     # a whole queue of broken entries: don't spin forever
                self.fails = 0
                self.pause()
                self.emit("error", "Stopped: %d songs in a row couldn't be played" % MAX_FAILS)
                return
            GLib.timeout_add(800, lambda: (self.next(), False)[1])

    @staticmethod
    def _is_pulse_loss(err, dbg):
        text = ((err.message or "") + " " + (dbg or "")).lower()
        return "pulse" in text or "connection terminated" in text or "connection refused" in text

    def _tick(self):
        if self.playing and self.current:
            pos, dur = self.position(), self.duration()
            self.emit("position", pos, dur)
            self.saved_at += 1
            if self.saved_at % 20 == 0:       # every ~10 s
                self.save_state()
            if not self.scrobbled and dur > 0 and (pos > dur / 2 or pos > 240):
                self.scrobbled = True
                self.store.scrobble(self.current["id"], True)
            self._xf_tick(pos, dur)
        return True

    # --- smart crossfade ---------------------------------------------------
    def _can_crossfade(self):
        return bool(self.s["crossfade"]) and self.s["output"] != "direct"

    def _xf_length(self, cur, nxt):
        """Fade length in seconds for cur -> nxt, or None for a gapless (or no) transition."""
        if not cur or not nxt or not self._can_crossfade() or cur is nxt:
            return None
        if (cur.get("albumId") and cur.get("albumId") == nxt.get("albumId")
                and (cur.get("discNumber") or 1) == (nxt.get("discNumber") or 1)
                and nxt.get("track") == (cur.get("track") or 0) + 1):
            return None                 # next track of the same album: keep it gapless
        dc, dn = float(cur.get("duration") or 0), float(nxt.get("duration") or 0)
        if min(dc, dn) < 30:
            return None
        return min(float(self.s["crossfade_s"]), dc / 4, dn / 4)

    def _xf_tick(self, pos, dur):
        if self.xf or dur <= 0 or self.s["repeat"] == "one":
            return
        i = self._next_index(auto=True)
        if i is None or i == self.index:
            return
        length = self._xf_length(self.current, self.queue[i])
        if not length:
            return
        rem = dur - pos
        if rem <= length + PREROLL_S and self.next_id != (i, self.queue[i]["id"]):
            self._prepare(i)
        if rem <= length + 0.3:
            self._start_xf(i, length)
        elif rem <= OUTRO_WINDOW and pos > 30 and self._outro_quiet():
            print("crossfade: quiet outro, %.0f s early" % (rem - length), flush=True)
            self._start_xf(i, min(length, 3.0))

    def _outro_quiet(self):
        """The song has gone quiet for good (fade-out, silent tail, hidden track gap)."""
        lv = self.levels
        if len(lv) < 300:               # < 30 s of meter data
            return False
        loud = sorted(x for x in lv if x > -80)
        if not loud:
            return False
        median = loud[len(loud) // 2]
        tail = lv[-QUIET_SAMPLES:]
        return len(tail) == QUIET_SAMPLES and all(x < max(median - 20, -50) for x in tail)

    def _on_level(self, pb, msg):
        st = msg.get_structure()
        if not st or st.get_name() != "level":
            return
        m = re.search(r"rms=\(GValueArray\)<([^>]*)>", st.to_string())
        if not m:
            return
        try:
            rms = max(float(x) for x in m.group(1).split(","))
        except ValueError:
            return
        if pb is self.pb:
            if self.xf:
                self.in_level = rms      # the incoming song during a fade
            else:
                self.levels.append(rms)
                if len(self.levels) > 20000:
                    del self.levels[:10000]

    def _prepare(self, i):
        """Preroll the next song (paused, silent) on a fresh deck."""
        self._drop_next()
        uri = self._uri(self.queue[i])
        if not uri:
            return
        d = self._new_deck()
        d.set_property("uri", uri)
        d.set_property("volume", 0.0)
        d.set_state(Gst.State.PAUSED)
        self.next_deck, self.next_id = d, (i, self.queue[i]["id"])

    def _drop_next(self):
        self._drop_deck(self.next_deck)
        self.next_deck, self.next_id = None, None

    def _start_xf(self, i, length):
        self._finish_xf()
        if self.next_id != (i, self.queue[i]["id"]):
            self._prepare(i)
        if self.next_deck is None:     # no uri (offline, not cached): plain switch
            self._load(i)
            self.emit("state-changed")
            return
        old_dur = self.duration()
        old, new = self.pb, self.next_deck
        self.next_deck, self.next_id = None, None
        new.set_property("volume", 0.0)
        new.set_state(Gst.State.PLAYING)
        self.pb = new
        self.index, self.pending, self.scrobbled = i, None, False
        self.loaded, self.resume_at, self.playing = True, 0.0, True
        self.in_level = -100.0
        self.xf = {"old": old, "len": max(0.2, length), "t": 0.0, "last": time.monotonic(), "waited": 0.0,
                   "old_dur": old_dur}
        self._track_started()
        self.emit("state-changed")
        GLib.timeout_add(40, self._xf_step)

    def _xf_step(self):
        x = self.xf
        if not x:
            return False
        now = time.monotonic()
        dt, x["last"] = now - x["last"], now
        # a silent intro holds the fade (max INTRO_WAIT_S) so the old song doesn't vanish into silence
        if self.in_level > -45 or x["waited"] >= INTRO_WAIT_S or x["len"] <= SKIP_FADE:
            x["t"] += dt
        else:
            x["waited"] += dt
        p = min(1.0, x["t"] / x["len"])
        # the old song must be silent by its own end, even while the fade waits for an intro
        p_out = p
        ok, opos = x["old"].query_position(Gst.Format.TIME)
        if ok and x["old_dur"] > 0:
            old_rem = x["old_dur"] - opos / Gst.SECOND
            p_out = max(p, min(1.0, 1.0 - old_rem / x["len"]))
        v = float(self.s["volume"])
        self.pb.set_property("volume", v * math.sin(p * math.pi / 2))   # equal power: no dip
        x["old"].set_property("volume", v * math.cos(p_out * math.pi / 2))
        if p >= 1.0:
            self._finish_xf()
            return False
        return True

    def _finish_xf(self):
        x, self.xf = self.xf, None
        if x:
            self._drop_deck(x["old"])
            self.pb.set_property("volume", float(self.s["volume"]))

    # --- format info ---------------------------------------------------------
    def source_format(self):
        t = self.current or {}
        parts = [(t.get("suffix") or "").upper()]
        bd, sr = t.get("bitDepth"), t.get("samplingRate")
        if bd and sr:
            parts.append(f"{bd}-bit / {sr / 1000:g} kHz")
        elif sr:
            parts.append(f"{sr / 1000:g} kHz")
        if t.get("bitRate"):
            parts.append(f"{t['bitRate']} kbps")
        local = t.get("id") and self.store.is_local(t["id"])
        if not local:
            label = QUALITY[self.s["quality_mobile" if metered() else "quality_wifi"]][0]
            if not label.startswith("Original"):
                parts = [label]
        parts.append("offline" if local else "stream")
        return " · ".join(p for p in parts if p)

    def _emit_format(self):
        out = ""
        pad = self.sink.get_static_pad("sink") if self.sink else None
        caps = pad.get_current_caps() if pad else None
        if caps:
            st = caps.get_structure(0)
            rate = st.get_value("rate") or 0
            fmt = st.get_value("format") or ""
            if self.s["output"] == "direct":
                out = f"→ DAC {fmt} {rate / 1000:g} kHz · bit-perfect"
            else:
                extras = [x for x, on in (("RG " + self.s["replaygain"], self.s["replaygain"] != "off"),
                                          ("EQ", self.s["eq_enabled"])) if on]
                out = f"→ system mixer {rate / 1000:g} kHz" + (" · " + " · ".join(extras) if extras else "")
        self.emit("format", out)

    def shutdown(self):
        if self.current:
            self.save_state()
        self.pb.set_state(Gst.State.NULL)
        self._suspend_pulse(None)
