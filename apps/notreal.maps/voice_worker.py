"""piper renderer in its own process: inside the GTK app espeak-ng broke after the first phrase
(every later phrase came out empty), so the app talks to this over stdin/stdout instead.
Protocol: one JSON line {"text", "path", "model"} in, one line "ok <bytes>" or "err <msg>" out."""
import json
import sys
import wave

from piper import PiperVoice, SynthesisConfig

voice, loaded = None, None
for line in sys.stdin:
    try:
        job = json.loads(line)
        if loaded != job["model"]:
            voice, loaded = PiperVoice.load(job["model"]), job["model"]
        audio = b"".join(c.audio_int16_bytes for c in voice.synthesize(
            job["text"], syn_config=SynthesisConfig(length_scale=0.88)))
        if not audio:
            print("err empty audio", flush=True)
            continue
        with wave.open(job["path"], "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(voice.config.sample_rate)
            w.writeframes(audio)
        print("ok %d" % len(audio), flush=True)
    except Exception as e:  # noqa: BLE001 - report and keep serving
        print("err %s" % str(e).replace("\n", " "), flush=True)
