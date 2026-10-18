"""Spoken directions with piper (same neural voice as the voice assistant), newest phrase wins."""
import hashlib
import json
import os
import subprocess
import sys
import threading

MODEL = os.path.expanduser("~/voice/models/en_US-amy-medium.onnx")
CACHE = os.path.expanduser("~/.cache/notarealmaps/voice")
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_worker.py")


class Voice:
    def __init__(self):
        self.enabled = True
        self.cv = threading.Condition()
        self.queue = []
        self.player = None
        self.piper = None          # voice_worker.py process
        self.ahead = []            # phrases to render before they're needed
        threading.Thread(target=self._run, daemon=True).start()

    def prepare(self, texts):
        """Render upcoming phrases in the background so "turn now" plays instantly."""
        with self.cv:
            self.ahead = [t for t in texts if t] + self.ahead[:20]
            self.cv.notify()

    def say(self, text):
        if not text or not self.enabled:
            return
        with self.cv:
            self.queue = [text]          # stale directions are worse than none
            self.cv.notify()

    def stop(self):
        with self.cv:
            self.queue = []
        if self.player and self.player.poll() is None:
            self.player.terminate()

    def _wav(self, text):
        os.makedirs(CACHE, exist_ok=True)
        path = os.path.join(CACHE, hashlib.sha1((MODEL + text).encode()).hexdigest()[:16] + ".wav")
        if not os.path.exists(path):
            tmp = path + ".tmp.wav"
            for attempt in (1, 2):           # a fresh worker fixes a wedged espeak
                reply = self._render(text, tmp)
                if reply.startswith("ok"):
                    break
                print("voice render %r: %s (attempt %d)" % (text, reply, attempt), flush=True)
                self._kill_worker()
            else:
                raise RuntimeError("piper gave no audio for %r" % text)
            os.replace(tmp, path)
        return path

    def _render(self, text, tmp):
        # model loaded once per worker, ~2 s; then ~0.5 s a phrase
        if self.piper is None or self.piper.poll() is not None:
            self.piper = subprocess.Popen([sys.executable, WORKER], stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, text=True)
        try:
            self.piper.stdin.write(json.dumps({"text": text, "path": tmp, "model": MODEL}) + "\n")
            self.piper.stdin.flush()
            return self.piper.stdout.readline().strip() or "err worker died"
        except OSError as e:
            return "err %s" % e

    def _kill_worker(self):
        if self.piper and self.piper.poll() is None:
            self.piper.kill()
            self.piper.wait()
        self.piper = None

    def _run(self):
        while True:
            with self.cv:
                while not self.queue and not self.ahead:
                    self.cv.wait()
                if not self.queue:
                    text = self.ahead.pop(0)
                    speak = False
                else:
                    text = self.queue.pop(0)
                    speak = True
            if not speak:
                try:
                    self._wav(text)
                except Exception as e:  # noqa: BLE001
                    print("voice prepare:", e, flush=True)
                continue
            try:
                wav = self._wav(text)
                with self.cv:
                    if self.queue:           # something newer arrived while rendering
                        continue
                self.player = subprocess.Popen(["paplay", "--property=media.role=navigation", wav],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.player.wait()
            except Exception as e:  # noqa: BLE001 - never kill the voice thread
                print("voice:", e, flush=True)
