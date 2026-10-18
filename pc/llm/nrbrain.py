#!/usr/bin/env python3
"""nrbrain: the phone's AI gateway on the PC (Tailscale only).

POST /v1/chat/completions  -> llama-server (Qwen3.6 35B-A3B on the RTX), started on demand:
    before loading, ComfyUI is asked to unload its models and free VRAM (POST :8188/free),
    because ComfyUI is often left running and holding the card.
    The model is stopped again after IDLE_S without requests (gives the VRAM back).
GET /health -> {"ok": true, "loaded": bool} (cheap: the phone checks reachability with it).
"""
import json
import os
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
LISTEN = (os.environ.get("NRBRAIN_HOST", "127.0.0.1"), 8097)
LLAMA = os.path.join(HOME, "llm/llama.cpp/build/bin/llama-server")
MODEL = os.path.join(HOME, "llm/models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf")
LLAMA_PORT = 8099
CPU_MOE = os.environ.get("NRBRAIN_CPU_MOE", "14")   # MoE expert layers kept in RAM (fits 16 GB VRAM)
COMFY = "http://127.0.0.1:8188"
WHISPER = "http://127.0.0.1:8098"   # nrwhisper.service (CPU)
TTS = "http://127.0.0.1:8100"   # nrtts.service (Kokoro, CPU)
IDLE_S = 600
LOG = os.path.join(HOME, "llm/nrbrain.log")

lock = threading.Lock()
state = {"proc": None, "last": 0.0}


def log(*a):
    with open(LOG, "a") as f:
        print(time.strftime("%F %T"), *a, file=f, flush=True)


def free_comfy():
    """ComfyUI holds GBs of VRAM when left open: unload its models (no-op if it isn't running)."""
    try:
        req = urllib.request.Request(COMFY + "/free", data=json.dumps(
            {"unload_models": True, "free_memory": True}).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        log("asked ComfyUI to free VRAM")
        time.sleep(1.0)
    except Exception:
        pass


def healthy():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{LLAMA_PORT}/health", timeout=1).read()
        return True
    except Exception:
        return False


def ensure():
    p = state["proc"]
    if p and p.poll() is None and healthy():
        return
    free_comfy()
    log("starting llama-server")
    t0 = time.time()
    state["proc"] = subprocess.Popen(
        [LLAMA, "-m", MODEL, "--host", "127.0.0.1", "--port", str(LLAMA_PORT),
         # auto-fit layers/experts to the VRAM actually free (desktop uses ~2 GB), keep 1 GB spare
         "--fit", "on", "--fit-target", "1024", "-c", "16384", "--jinja", "-fa", "on", "--no-webui",
         "-t", "12", "--parallel", "1"],
        stdout=open(os.path.join(HOME, "llm/llama-server.log"), "w"), stderr=subprocess.STDOUT)
    for _ in range(600):
        if state["proc"].poll() is not None:
            raise RuntimeError("llama-server exited, see ~/llm/llama-server.log")
        if healthy():
            log(f"llama-server ready in {time.time() - t0:.1f}s")
            return
        time.sleep(0.25)
    raise RuntimeError("llama-server did not come up")


def reaper():
    while True:
        time.sleep(30)
        with lock:
            p = state["proc"]
            if p and p.poll() is None and time.time() - state["last"] > IDLE_S:
                log("idle: stopping llama-server (VRAM back for ComfyUI)")
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
                state["proc"] = None


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
        if self.path == "/health":
            p = state["proc"]
            self._send(200, json.dumps({"ok": True, "loaded": bool(p and p.poll() is None)}).encode())
        else:
            self._send(404, b"{}")

    def do_POST(self):
        if self.path == "/inference":          # speech-to-text: straight through to whisper-server
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            t0 = time.time()
            try:
                req = urllib.request.Request(WHISPER + "/inference", data=body,
                                             headers={"Content-Type": self.headers.get("Content-Type")})
                with urllib.request.urlopen(req, timeout=60) as r:
                    out = r.read()
                log(f"/inference {len(body)}B in {time.time() - t0:.2f}s")
                return self._send(200, out)
            except Exception as e:
                log("whisper error:", repr(e))
                return self._send(503, json.dumps({"error": str(e)}).encode())
        if self.path == "/tts":                # text-to-speech: straight through to nrtts (Kokoro)
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            t0 = time.time()
            try:
                req = urllib.request.Request(TTS + "/tts", data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    out = r.read()
                log(f"/tts {len(body)}B in {time.time() - t0:.2f}s")
                return self._send(200, out, "audio/wav")
            except Exception as e:
                log("tts error:", repr(e))
                return self._send(503, json.dumps({"error": str(e)}).encode())
        if not self.path.startswith("/v1/"):
            return self._send(404, b"{}")
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        t0 = time.time()
        try:
            with lock:
                state["last"] = time.time()
                ensure()
                req = urllib.request.Request(f"http://127.0.0.1:{LLAMA_PORT}{self.path}", data=body,
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=300) as r:
                    out = r.read()
                state["last"] = time.time()
            log(f"{self.path} {len(body)}B -> {len(out)}B in {time.time() - t0:.2f}s")
            self._send(200, out)
        except Exception as e:
            log("error:", repr(e))
            self._send(503, json.dumps({"error": str(e)}).encode())


if __name__ == "__main__":
    threading.Thread(target=reaper, daemon=True).start()
    log("nrbrain listening on %s:%d" % LISTEN)
    ThreadingHTTPServer(LISTEN, H).serve_forever()
