#!/usr/bin/env python3
"""ask.py: the doctor asks the PC's model what's wrong (10-24).

    ask.py ["why is my phone hot?"]   -> JSON {summary, cause, steps, actions, error?}

Builds a compact health snapshot (doctor result, battery trends, what uses the CPU right now,
radios, modes, recent log lines) and sends it with the question to Qwen on the PC (nrbrain,
~/llm/nrbrain.py). The model may only suggest actions from nrdoctor.ACTIONS; the card shows
them as buttons and nothing runs without a tap. The answer is also saved for the card.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nrdoctor as doc  # noqa: E402
import trends  # noqa: E402

PC_BRAIN = "http://%s:8097" % doc.pc("host", "localhost")
OUT = os.path.join(doc.RUN, "nrdoctor-ask.json")
LOGS = [   # (label, path, lines)
    ("doctor", os.path.join(doc.RUN, "nrdoctor.log"), 15),
    ("sleepd", os.path.join(doc.HOME, "sleepd/sleepd.log"), 10),
    ("battery saver", os.path.join(doc.HOME, "saver/nrsaver.log"), 4),
    ("chromium launcher", doc.CHROMIUM_LOG, 3),
]

PROMPT = """You are the health assistant of the user's own Linux phone (Nothing Phone 1 running \
postmarketOS with Phosh; many helpers are the user's own Python daemons: voiced = voice assistant, \
chat.py = messaging, vapormaild = mail, spin = music player, crtd = power key, phantomshell/overview-widget = \
home screen, swipekbd = keyboard, nrbatteryd = battery logger, sleepd = suspends after screen-off).

Question: %s

Answer ONLY from this snapshot, in plain language for a non-expert, short. If nothing is wrong, say so.
"cause": the most likely cause in one sentence (or "" if unclear). "steps": up to 4 concrete things the \
user can do. "actions": ids from the list below, ONLY when the snapshot shows that exact thing broken or \
stopped (e.g. "restart:voice assistant" only if the voice assistant is down); otherwise an empty \
list - an empty list is the normal case. They run only when the user taps them.

Allowed actions:
%s

Snapshot:
%s"""


def cpu_now():
    a = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            f = doc.read(f"/proc/{d}/stat").rsplit(")", 1)
            if len(f) == 2:
                v = f[1].split()
                a[d] = int(v[11]) + int(v[12])
    time.sleep(2)
    hz = os.sysconf("SC_CLK_TCK")
    out = []
    for d, t0 in a.items():
        f = doc.read(f"/proc/{d}/stat").rsplit(")", 1)
        if len(f) != 2:
            continue
        v = f[1].split()
        pct = (int(v[11]) + int(v[12]) - t0) * 100 / hz / 2
        if pct >= 3:
            cmd = doc.read(f"/proc/{d}/cmdline").replace("\0", " ").strip()
            out.append((round(pct), cmd[:90]))
    return sorted(out, reverse=True)[:8]


def snapshot():
    bat = doc.BAT
    temps = []
    for z in os.listdir("/sys/class/thermal"):
        t = doc.read(f"/sys/class/thermal/{z}/temp")
        if t.lstrip("-").isdigit():
            temps.append(int(t) / 1000)
    m = re.search(r"MemAvailable:\s+(\d+)", doc.read("/proc/meminfo"))
    last = json.loads(doc.read(doc.LAST) or "{}")
    lines = [
        time.strftime("now: %A %H:%M"),
        f"uptime: {float(doc.read('/proc/uptime', '0 0').split()[0]) / 3600:.1f} h, load {os.getloadavg()[0]:.2f}",
        f"battery: {doc.read(bat + '/capacity')}% {doc.read(bat + '/status')}, "
        f"{int(doc.read(bat + '/current_now', '0')) / 1000:.0f} mA (+ = charging), "
        f"{int(doc.read(bat + '/temp', '0')) / 10:.0f} °C; hottest chip {max(temps, default=0):.0f} °C",
        f"memory available: {int(m.group(1)) // 1024 if m else '?'} MB; disk used {doc.use_pct('/'):.0f}%",
        f"screen: {'on' if '<0>' in doc.sh('gdbus', 'call', '--session', '-d', 'org.gnome.Mutter.DisplayConfig', '-o', '/org/gnome/Mutter/DisplayConfig', '-m', 'org.freedesktop.DBus.Properties.Get', 'org.gnome.Mutter.DisplayConfig', 'PowerSaveMode') else 'off'}",
        "network: " + " ".join(doc.sh("nmcli", "-t", "-f", "TYPE,STATE", "device").split()),
        f"modes: battery saver {'on' if os.path.exists(os.path.expanduser('~/.local/state/nrsaver/on')) else 'off'}, "
        f"DAP {'on' if os.path.exists(os.path.expanduser('~/.local/state/nrdap/on')) else 'off'}",
        "CPU right now (% of one core, command): " + "; ".join(f"{p}% {c}" for p, c in cpu_now()),
        "doctor (last check): " + json.dumps({k: last.get(k, []) for k in ("fixed", "failed", "manual", "minor")}),
        "battery today: " + json.dumps(trends.summary(trends.today())),
        "trend notes: " + "; ".join(trends.notes()),
    ]
    quiet = re.compile(r"check: \d+ ok, fixed \[\], failed \[\], down \[\]")   # routine all-OK lines
    for label, path, n in LOGS:
        t = "\n".join(l for l in doc.tail(path, 60).splitlines() if not quiet.search(l))
        t = "\n".join(t.splitlines()[-n:]).strip()
        if t:
            lines.append(f"--- {label} log (last {n}) ---\n{t}")
    k = doc.tail(os.path.join(doc.HOME, "crashlog/kmsg.log"), 300)
    errs = [l[-160:] for l in k.splitlines() if re.search(r"error|fail|timeout|oops|bug:", l, re.I)][-12:]
    if errs:
        lines.append("--- kernel errors (last 12) ---\n" + "\n".join(errs))
    return "\n".join(lines)


def ask(question="What's wrong with my phone right now? Anything draining the battery or making it hot?"):
    acts = {k: v[0] for k, v in doc.ACTIONS.items()}
    schema = {"type": "object", "properties": {
        "summary": {"type": "string"}, "cause": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "actions": {"type": "array", "items": {"enum": list(acts)}, "maxItems": 3}},
        "required": ["summary", "cause", "steps", "actions"]}
    content = PROMPT % (question, "\n".join(f"{k}: {v}" for k, v in acts.items()), snapshot())
    body = {"messages": [{"role": "user", "content": content}], "temperature": 0.2, "max_tokens": 500,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema", "json_schema": {"schema": schema}}}
    t0 = time.time()
    try:
        req = urllib.request.Request(PC_BRAIN + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:      # first use loads the model (~30 s)
            out = json.loads(json.load(r)["choices"][0]["message"]["content"])
        out["actions"] = [a for a in out.get("actions", []) if a in acts]
        out["labels"] = {a: acts[a] for a in out["actions"]}
    except Exception as e:  # noqa: BLE001
        out = {"summary": "", "cause": "", "steps": [], "actions": [], "labels": {},
               "error": f"The PC didn't answer ({e.__class__.__name__}). Is it on?"}
    out["question"], out["time"], out["secs"] = question, time.time(), round(time.time() - t0, 1)
    doc.log(f"ask ({out['secs']}s): {out.get('error') or out.get('summary', '')[:120]}")
    try:
        with open(OUT, "w") as f:
            json.dump(out, f)
    except OSError:
        pass
    return out


if __name__ == "__main__":
    if "--snapshot" in sys.argv:
        print(snapshot())
    else:
        print(json.dumps(ask(*[a for a in sys.argv[1:] if not a.startswith("--")][:1]), indent=1))
