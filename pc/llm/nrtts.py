#!/usr/bin/env python3
"""nrtts: Kokoro text-to-speech for the phone's voiced (10-17).
POST /tts {"text": ..., "voice": "af_heart", "speed": 1.0} -> audio/wav (24 kHz mono 16-bit).
Listens on 127.0.0.1:8100; nrbrain (:8097) proxies /tts to it. CPU only, so it never takes
VRAM from the LLM or ComfyUI."""
import io
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import soundfile as sf
from kokoro_onnx import Kokoro

MODELS = os.path.expanduser("~/llm/models/kokoro")
VOICE = os.environ.get("NRTTS_VOICE", "af_heart")
kokoro = Kokoro(os.path.join(MODELS, "kokoro-v1.0.onnx"), os.path.join(MODELS, "voices-v1.0.bin"))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, b'{"ok": true}') if self.path == "/health" else self._send(404, b"{}")

    def do_POST(self):
        if self.path != "/tts":
            return self._send(404, b"{}")
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            t0 = time.time()
            samples, rate = kokoro.create(req["text"], voice=req.get("voice", VOICE),
                                          speed=float(req.get("speed", 1.0)), lang="en-us")
            buf = io.BytesIO()
            sf.write(buf, samples, rate, format="WAV", subtype="PCM_16")
            print(f"tts {len(req['text'])} chars -> {len(samples) / rate:.1f}s audio in {time.time() - t0:.2f}s",
                  flush=True)
            self._send(200, buf.getvalue(), "audio/wav")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}).encode())


ThreadingHTTPServer(("127.0.0.1", 8100), H).serve_forever()
