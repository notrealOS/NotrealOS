"""Voice typing for swipekbd: record the mic with parecord, transcribe with whisper.cpp
(same model and mic as ~/voice/voiced.py), hand the text back on the GTK main loop."""
import array, os, re, signal, subprocess, threading, wave

from gi.repository import GLib

RUN = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
WAV = os.path.join(RUN, "swipekbd-dict.wav")
MIC = "alsa_input.platform-sound.HiFi__Mic__source"
MODEL = os.path.expanduser("~/voice/models/ggml-base.bin")
MAX_REC_S = 60
THREADS = "4"  # faster than 6/8 on this SoC
# names and places the base model otherwise mangles (English only)
PROMPT_EN = "Deyse, Boris, Franco, Juancho, Stockholm, Gustavsberg, Södermalm, WhatsApp, EasyPark."


class Dictation:
    """idle -> recording (tap) -> working (tap / timeout) -> idle; on_text(str) on success."""

    def __init__(self, on_text, on_state):
        self.on_text, self.on_state = on_text, on_state
        self.state = "idle"
        self.rec = None
        self.timer = 0

    def toggle(self, lang):
        if self.state == "idle":
            self.start(lang)
        elif self.state == "recording":
            self.stop()

    def start(self, lang):
        try:
            self.rec = subprocess.Popen(
                ["parecord", "--device=" + MIC, "--rate=16000", "--channels=1",
                 "--format=s16le", "--file-format=wav", WAV],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return
        self.lang = lang
        self.timer = GLib.timeout_add_seconds(MAX_REC_S, self.timed_out)
        self.set_state("recording")

    def timed_out(self):
        self.timer = 0  # this source ends by returning False; don't remove it twice
        return self.stop()

    def stop(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        rec, self.rec = self.rec, None
        if not rec:
            return False
        rec.send_signal(signal.SIGINT)
        try:
            rec.wait(2)
        except subprocess.TimeoutExpired:
            rec.kill()
        self.set_state("working")
        threading.Thread(target=self.work, args=(self.lang,), daemon=True).start()
        return False

    def cancel(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        rec, self.rec = self.rec, None
        if rec:
            rec.kill()
            rec.wait()
        if self.state == "recording":
            self.set_state("idle")

    def set_state(self, s):
        self.state = s
        self.on_state(s)

    def work(self, lang):
        text = ""
        try:
            text = transcribe(lang)
        except Exception as e:  # never take the keyboard down
            print("swipekbd: dictation failed:", e, flush=True)
        GLib.idle_add(self.done, text)

    def done(self, text):
        self.set_state("idle")
        if text:
            self.on_text(text)
        return False


def normalize():
    """Scale to a peak of ~-3 dBFS (the mic is quiet, max 30x); False if it's silence."""
    with wave.open(WAV) as w:
        params, a = w.getparams(), array.array("h", w.readframes(w.getnframes()))
    peak = max((abs(x) for x in a), default=0)
    if peak < 50:
        return False
    g = min(23000 / peak, 30.0)
    a = array.array("h", (max(-32768, min(32767, int(x * g))) for x in a))
    with wave.open(WAV, "wb") as w:
        w.setparams(params)
        w.writeframes(a.tobytes())
    return True


def transcribe(lang):
    if not normalize():
        return ""
    cmd = ["whisper-cli", "-m", MODEL, "-f", WAV, "-l", lang, "-nt", "-np", "-t", THREADS, "-ng"]
    if lang == "en":
        cmd += ["--prompt", PROMPT_EN]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=180).stdout
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", " ".join(out.split()))  # [BLANK_AUDIO], (music)
    return " ".join(text.split())
