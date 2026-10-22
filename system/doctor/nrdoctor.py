#!/usr/bin/env python3
"""nrdoctor: check the phone's background services and restart the ones that are down.

Run by the overview widget's CHECK button (or by hand). Prints a human summary, or with
--json one JSON object {"ok": [...], "fixed": [...], "failed": [...], "manual": [...],
"minor": [...]}. --check only looks (nothing restarted): the widget runs that every few
minutes for the face's mood (failed/manual -> sad, minor -> meh, else happy).

- user daemons (our autostart entries etc.): alive = a process of ours whose command line
  contains MATCH; fix = start the launcher again (detached, without LD_PRELOAD).
- PulseAudio: alive = the native socket exists AND `pactl info` answers (10-10 and 10-12 the
  daemon kept running with its socket gone -> every client got "connection refused").
  fix = restart it, put the USB DAC back as default, then restart callaudiod + callmode-user
  (both hold a pulse connection that dies with it).
- root services + the PC: checked only (fixing needs sudo / the PC), reported as manual.
- phone health (10-13): mobile network, SMS stuck on the modem (= notarealChat isn't taking
  them: restart it), the call voice mixers (calls are silent without them: set them),
  the chat server on the PC, $XDG_RUNTIME_DIR filling up (truncate big logs), free disk,
  and whether Tailscale reaches the PC directly or only through a slow relay (minor).
- 10-16: USB DAC (default sink + its 2nd volume control), speaker sample format (the DSP
  garbles anything but s16le), USB-C devices that never enumerated, internet, the home
  phone's adb link (droid apps), battery heat / low battery, low RAM, CPU hogs, and the
  sleep fixes (s2idle, Bluetooth wake off).
- --auto (the widget's periodic run since 10-16): like a tap, but a daemon that was
  restarted AUTO_MAX times within the last hour is left alone (crash loop, or the user
  closed it on purpose) and reported as failed.
"""

import re

import datetime
import json
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request


def pc(key, default=""):
    """Address of our PC server (see docs/pc-link.md); set in ~/.config/notreal/pc.json."""
    try:
        with open(os.path.expanduser("~/.config/notreal/pc.json")) as f:
            return json.load(f).get(key) or default
    except (OSError, ValueError):
        return default

HOME = os.path.expanduser("~")
RUN = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
UID = os.getuid()
LOG = os.path.join(RUN, "nrdoctor.log")

# (name, cmdline substring, launcher)
DAEMONS = [
    ("voice assistant", "/voice/voiced.py", f"{HOME}/voice/voiced"),
    ("camera flash", "/camflash/camflash", f"{HOME}/camflash/camflash-loop"),
    ("alarms", "/alarm/alarmd.py", f"{HOME}/alarm/alarmd"),
    ("notification volume", "/nrvolume/nrvolumed.py", f"{HOME}/nrvolume/nrvolumed"),
    ("CRT / power key", "/crt/crtd.py", f"{HOME}/crt/crtd-loop"),
    ("gallery backup", "/gallery/galleryd.py", f"{HOME}/gallery/galleryd"),
    ("app-switch gestures", "/gestures/gestures", f"{HOME}/gestures/gestures"),
    ("edge swipe", "/.local/bin/edge-swipe", f"{HOME}/.local/bin/edge-swipe"),
    ("glyph", "/.local/bin/glyphd", f"{HOME}/.local/bin/glyphd"),
    ("notarealShell", "/phantomshell/phantomshell.py", f"{HOME}/.local/bin/phantomshell"),
    ("notarealChat", "/chat/chat.py", f"{HOME}/chat/notarealchat"),
    ("auto-rotate", "/rotated/rotated.py", f"{HOME}/rotated/rotated"),
    ("sleep", "/sleepd/sleepd.py",
     f"exec {HOME}/sleepd/sleepd.py >> {HOME}/sleepd/sleepd.log 2>&1"),
    ("mail notifications", "/vapormail/vapormaild.py", f"{HOME}/vapormail/vapormaild"),
    ("droid notifications", "/droid/droid-notifyd", f"{HOME}/droid/droid-notifyd"),
    ("crash logger", "/crashlog/crashlog.py", f"{HOME}/crashlog/crashlog.py"),
    ("swipe keyboard", "/swipe/swipekbd.py", f"{HOME}/swipe/swipekbd-session"),
    ("call audio", "/usr/bin/callaudiod", "/usr/bin/callaudiod"),
    ("call helper", "/callfix/callmode/callmode-user", f"{HOME}/callfix/callmode/callmode-user"),
    ("incoming calls", "gnome-calls --gapplication-service", "gnome-calls --gapplication-service"),
    ("call screen", "/calls/nrcalls.py", f"{HOME}/calls/nrcalls --service"),
    ("DAC volume fix", "/.local/bin/ka5-volfix", f"{HOME}/.local/bin/ka5-volfix"),
    ("overview widget", "/overview-widget/overview-widget.py", f"{HOME}/overview-widget/overview-widget"),
]
AFTER_PULSE = ("call audio", "call helper")   # restarted whenever pulse is restarted

# root services (supervise-daemon); (name, cmdline substring)
SYSTEM = [
    ("autofocus (afd)", "/usr/local/sbin/afd"),
    ("call voice (q6voiced)", "/usr/bin/q6voiced"),
    ("earpiece", "/usr/local/sbin/earpiece-callmode"),
    ("tailscale", "/usr/sbin/tailscaled"),
    ("modem (ModemManager)", "/usr/sbin/ModemManager"),
]
MUSIC_SERVER = "http://%s:4533/ping" % pc("host", "localhost")
CHAT_SERVER = pc("matrix", "https://localhost") + "/_matrix/client/versions"
VOICE_MIXERS = ("PRI_MI2S_RX Voice Mixer VoiceMMode1", "VoiceMMode1 Capture Mixer TX_CODEC_DMA_TX_3")
TX_TOPOLOGY = ("VoiceMMode1 TX Topology", 69489)   # TX_SM_ECNS: echo cancel (NONE = 69488)
SMS_STUCK_S = 180     # a received SMS still on the modem after this = notarealChat missed it
STATE = os.path.join(RUN, "nrdoctor-state.json")   # --auto restart history
AUTO_MAX = 3          # restarts per hour before --auto gives up on a daemon
INTERNET = "http://connectivitycheck.gstatic.com/generate_204"
DROID = pc("android")   # home Android phone, "ip:5555" (empty = no Android phone) (scrcpy apps + droid-notifyd)
PC_NAME = urllib.parse.urlparse(pc("matrix")).hostname.split(".")[0] if pc("matrix") else ""
BT_WAKE = "/sys/devices/platform/soc@0/9c0000.geniqup/99c000.serial/power/wakeup"
BAT = "/sys/class/power_supply/qcom-battmgr-bat"
HOT_C = 45
LOW_RAM_MB = 400
HOG_PCT = 60          # one process above this share of a core (sampled over 2 s)


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def procs():
    """[(pid, uid, cmdline)] for every process."""
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
            uid = os.stat(f"/proc/{d}").st_uid
        except OSError:
            continue
        if cmd and int(d) != os.getpid():
            out.append((int(d), uid, cmd))
    return out


def find(match, mine=True):
    return [p for p, uid, cmd in procs() if match in cmd and (uid == UID or not mine)]


def child_env():
    env = dict(os.environ)
    env.pop("LD_PRELOAD", None)   # the widget preloads gtk4-layer-shell; launchers set their own
    return env


def launch(cmd):
    subprocess.Popen(["setsid", "-f", "sh", "-c", cmd], env=child_env(), cwd=HOME,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def kill(pids, wait=3.0):
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    end = time.time() + wait
    while time.time() < end and any(os.path.exists(f"/proc/{p}") for p in pids):
        time.sleep(0.1)
    for p in pids:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass


def wait_for(fn, secs):
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.3)
    return fn()


# --- pulse ------------------------------------------------------------------------

def pulse_ok():
    if not os.path.exists(os.path.join(RUN, "pulse", "native")):
        return False
    try:
        return subprocess.run(["pactl", "info"], capture_output=True, timeout=4).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pulse_fix():
    kill(find("/usr/bin/pulseaudio"))
    subprocess.run(["pulseaudio", "--start", "--log-target=syslog"], env=child_env(),
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=15)
    if not wait_for(pulse_ok, 8):
        return False
    # after a restart pulse may pick the speaker even with the DAC plugged in
    sinks = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True,
                           text=True).stdout.split("\n")
    usb = [s.split("\t")[1] for s in sinks if "\t" in s and ".usb-" in s.split("\t")[1]]
    if usb:
        subprocess.run(["pactl", "set-default-sink", usb[0]])
        inputs = subprocess.run(["pactl", "list", "short", "sink-inputs"], capture_output=True,
                                text=True).stdout.split("\n")
        for line in inputs:
            if line.strip():
                subprocess.run(["pactl", "move-sink-input", line.split("\t")[0], usb[0]])
    return True


# --- phone health -----------------------------------------------------------------

def sh(*cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def network_ok():
    out = sh("mmcli", "-m", "any")
    state = re.search(r"\bstate: (?:\x1b\[[0-9;]*m)?(\w+)", out)
    return bool(state) and state.group(1) in ("registered", "connected", "connecting")


def stuck_sms():
    """Received SMS that have sat on the modem longer than SMS_STUCK_S."""
    n = 0
    for path in re.findall(r"(/org/freedesktop/ModemManager1/SMS/\d+) \(received\)",
                           sh("mmcli", "-m", "any", "--messaging-list-sms")):
        ts = re.search(r"timestamp: (\S+)", sh("mmcli", "-s", path))
        try:
            t = datetime.datetime.fromisoformat(ts.group(1)).timestamp()
        except (AttributeError, ValueError):
            t = 0
        if time.time() - t > SMS_STUCK_S:
            n += 1
    return n


def mixers_ok():
    for name in VOICE_MIXERS:
        if "values=on" not in sh("amixer", "-c0", "cget", f"name={name}"):
            return False
    name, val = TX_TOPOLOGY
    return f": values={val}\n" in sh("amixer", "-c0", "cget", f"name={name}")


def mixers_fix():
    for name in VOICE_MIXERS:
        sh("amixer", "-q", "-c0", "cset", f"name={name}", "1")
    sh("amixer", "-q", "-c0", "cset", f"name={TX_TOPOLOGY[0]}", str(TX_TOPOLOGY[1]))
    return mixers_ok()


def use_pct(path):
    st = os.statvfs(path)
    return 100 * (1 - st.f_bavail / st.f_blocks) if st.f_blocks else 0


def run_fix():
    """Truncate big logs in $XDG_RUNTIME_DIR (10-12: a 736 MB log spam killed daemons)."""
    for f in os.listdir(RUN):
        p = os.path.join(RUN, f)
        try:
            if os.path.isfile(p) and f.endswith(".log") and os.path.getsize(p) > 20 << 20:
                os.truncate(p, 0)
        except OSError:
            pass
    return use_pct(RUN) < 85


def pc_relayed():
    line = [l for l in sh("tailscale", "status").split("\n") if PC_NAME and PC_NAME in l]
    return bool(line) and "relay" in line[0]


def reachable(url, timeout=10):
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


# --- 10-16: DAC, USB-C, battery, RAM, CPU, sleep -----------------------------------

def sinks():
    """[(name, spec)] from pactl."""
    out = []
    for line in sh("pactl", "list", "short", "sinks").split("\n"):
        f = line.split("\t")
        if len(f) >= 4:
            out.append((f[1], f[3]))
    return out


def dac_card():
    try:
        return "KA5" in open("/proc/asound/cards").read()
    except OSError:
        return False


def dac_default_ok():
    usb = [n for n, _ in sinks() if ".usb-" in n]
    return not usb or sh("pactl", "get-default-sink").strip() in usb


def dac_default_fix():
    usb = [n for n, _ in sinks() if ".usb-" in n]
    sh("pactl", "set-default-sink", usb[0])
    for line in sh("pactl", "list", "short", "sink-inputs").split("\n"):
        if line.strip():
            sh("pactl", "move-sink-input", line.split("\t")[0], usb[0])
    return dac_default_ok()


KA5_VOL = ("-c", "KA5", "cget", "name=PCM Playback Volume,index=1")


def dac_volume_ok():
    return not dac_card() or ": values=127" in sh("amixer", *KA5_VOL)


def dac_volume_fix():
    sh("amixer", "-q", "-c", "KA5", "cset", "name=PCM Playback Volume,index=1", "127")
    return dac_volume_ok()


def speaker_format_ok():
    """10-11: the q6 DSP garbles S24 (robotic speaker + mic); daemon.conf forces s16le."""
    return all(spec.startswith("s16le") for n, spec in sinks() if "Speaker" in n)


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def usb_enumerated():
    """A Type-C partner in host role (DAC, stick, ...) must show up on the USB bus
    (10-10 'HC died' and the boot-time 'xhci removed' quirk leave it powered but dead)."""
    if not os.path.exists("/sys/class/typec/port0-partner"):
        return True
    # typec data_role stays "[host]" even for a charger; the dwc3 role switch is the truth
    roles = [read(f"/sys/class/usb_role/{r}/role") for r in os.listdir("/sys/class/usb_role")]
    if "host" not in roles or "[source]" not in read("/sys/class/typec/port0/power_role"):
        return True     # charger / PC: nothing to enumerate
    return any(re.fullmatch(r"\d+-\d+", d) for d in os.listdir("/sys/bus/usb/devices"))


def usb_reset():
    """10-20: ~/usbreset (passwordless sudo): dwc3 mode flip, then driver rebind + flip."""
    try:
        subprocess.run(["sudo", "-n", "/usr/local/sbin/usbreset"], timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    time.sleep(2)   # let pulse add the DAC sink before the DAC checks
    return usb_enumerated()


def adb_ok():
    return f"{DROID}\tdevice" in sh("adb", "devices", timeout=6)


def adb_fix():
    sh("adb", "connect", DROID, timeout=10)
    return wait_for(adb_ok, 4)


def battery_notes():
    notes = []
    t = read(f"{BAT}/temp")
    if t.lstrip("-").isdigit() and int(t) / 10 >= HOT_C:
        notes.append(f"battery hot ({int(t) / 10:.0f} °C)")
    cap = read(f"{BAT}/capacity")
    if cap.isdigit() and int(cap) <= 15 and read(f"{BAT}/status") == "Discharging":
        notes.append(f"battery low ({cap}%)")
    return notes


def biggest_app():
    best = (0, "")
    for p, uid, cmd in procs():
        if uid != UID:
            continue
        rss = read(f"/proc/{p}/statm").split()
        if len(rss) > 1 and int(rss[1]) > best[0]:
            best = (int(rss[1]), cmd)
    return os.path.basename(best[1].split()[0]) if best[1] else "?"


def ram_note():
    m = re.search(r"MemAvailable:\s+(\d+)", read("/proc/meminfo"))
    if m and int(m.group(1)) // 1024 < LOW_RAM_MB:
        return f"low memory ({int(m.group(1)) // 1024} MB free; biggest: {biggest_app()})"
    return None


def cpu_hogs():
    def ticks():
        out = {}
        for d in os.listdir("/proc"):
            if d.isdigit():
                f = read(f"/proc/{d}/stat").rsplit(")", 1)
                if len(f) == 2:
                    v = f[1].split()
                    out[d] = (int(v[11]) + int(v[12]), read(f"/proc/{d}/comm"))
        return out
    a = ticks()
    time.sleep(2)
    b = ticks()
    hz = os.sysconf("SC_CLK_TCK")
    hogs = []
    for pid, (t, comm) in b.items():
        if pid in a and pid != str(os.getpid()):
            pct = (t - a[pid][0]) * 100 / hz / 2
            if pct >= HOG_PCT:
                hogs.append(f"{comm} using {pct:.0f}% CPU")
    return hogs


def sleep_ok():
    """sleepfix: BT UART wake must stay disabled (else every sleep bounces in 0.3 s)."""
    return read(BT_WAKE, "disabled") == "disabled"


def camera_services_ok():
    """10-16: two WirePlumbers (one survived a restart) fought over the cameras -> no preview."""
    sys.path.insert(0, os.path.join(HOME, "camera"))
    import camsvc
    return camsvc.healthy()


def camera_services_fix():
    r = subprocess.run(["python3", os.path.join(HOME, "camera", "camsvc.py")], capture_output=True, timeout=60)
    return r.returncode == 0


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    try:
        with open(STATE, "w") as f:
            json.dump(st, f)
    except OSError:
        pass


# --- main -------------------------------------------------------------------------

def run(fix=True, auto=False):
    res = {"ok": [], "fixed": [], "failed": [], "manual": [], "minor": []}
    state = load_state()

    def restart(name, match, launcher, force=False):
        pids = find(match)
        if not pids and fix and not force:
            # 10-16: a wrapper loop (voiced, crtd, chat…) restarts its daemon after a few seconds;
            # launching another one meanwhile gave TWO voice assistants
            pids = wait_for(lambda: find(match), 5)
        if pids and not force:
            res["ok"].append(name)
            return
        if not fix:
            res["failed"].append(name)
            return
        recent = [t for t in state.get(name, []) if time.time() - t < 3600]
        if auto and len(recent) >= AUTO_MAX:
            res["failed"].append(f"{name} (keeps stopping; tap to restart)")
            return
        state[name] = recent + [time.time()]
        if pids:
            kill(pids)
        launch(launcher)
        if wait_for(lambda: find(match), 6):
            res["fixed"].append(name)
            log(f"restarted {name}")
        else:
            res["failed"].append(name)
            log(f"FAILED to start {name}")

    def check(name, ok, fixer=None, bad="failed"):
        """ok() -> fine; else fixer() (when fixing) -> fixed / still bad."""
        if ok():
            res["ok"].append(name)
        elif fix and fixer and fixer():
            res["fixed"].append(name)
            log(f"fixed {name}")
        else:
            res[bad].append(name)

    pulse_restarted = False
    if pulse_ok():
        res["ok"].append("sound (pulseaudio)")
    elif not fix:
        res["failed"].append("sound (pulseaudio)")
    elif pulse_fix():
        res["fixed"].append("sound (pulseaudio)")
        pulse_restarted = True
        log("restarted pulseaudio")
    else:
        res["failed"].append("sound (pulseaudio)")
        log("FAILED to restart pulseaudio")

    check("free space for logs", lambda: use_pct(RUN) < 85, run_fix)
    for name, match, launcher in DAEMONS:
        restart(name, match, launcher, force=pulse_restarted and name in AFTER_PULSE)

    check("mobile network", network_ok, bad="manual")
    stuck = stuck_sms()
    if not stuck:
        res["ok"].append("SMS delivery")
    elif fix:   # 10-13: notarealChat had never attached to the modem; a restart re-lists
        restart("SMS delivery", "/chat/chat.py", f"{HOME}/chat/notarealchat", force=True)
    else:
        res["failed"].append(f"SMS delivery ({stuck} stuck on the modem)")
    check("call audio routing", mixers_ok, mixers_fix)
    check("speaker sample format (s16le)", speaker_format_ok, pulse_fix)
    # tap only (not --auto): a USB controller rebind should never happen behind the user's back
    check("USB-C device detected (if not: replug it, reboot if it stays dead)", usb_enumerated,
          None if auto else usb_reset, bad="manual")
    if dac_card():
        check("DAC is the default output", dac_default_ok, dac_default_fix)
        check("DAC volume (2nd control at max)", dac_volume_ok, dac_volume_fix)
    check("camera services (1 pipewire + 1 wireplumber)", camera_services_ok, camera_services_fix)
    check("internet", lambda: reachable(INTERNET, 6), bad="manual")
    if DROID:
        check("home phone link (droid apps)", adb_ok, adb_fix, bad="minor")
    check("sleep fix (Bluetooth wake off)", sleep_ok, bad="manual")
    res["minor"] += battery_notes() + [n for n in [ram_note()] if n] + cpu_hogs()
    check("disk space", lambda: use_pct("/") < 90, bad="minor")

    for name, match in SYSTEM:
        (res["ok"] if find(match, mine=False) else res["manual"]).append(name)

    check("music server (PC)", lambda: reachable(MUSIC_SERVER, 5), bad="manual")
    check("chat server (PC)", lambda: reachable(CHAT_SERVER), bad="manual")
    if "tailscale" in res["ok"] and pc_relayed():
        res["minor"].append("PC link relayed (slow chats)")
    save_state(state)
    if fix or res["failed"] or res["manual"]:
        log(f"check: {len(res['ok'])} ok, fixed {res['fixed']}, failed {res['failed']}, "
            f"down {res['manual']}" + ("" if fix else " (check only)"))
    return res


PAUSE = os.path.join(RUN, "nrdoctor.pause")   # sleep tests stop daemons on purpose (10-17)


def main():
    if "--auto" in sys.argv and os.path.exists(PAUSE):
        print("paused (", PAUSE, ")")
        return
    res = run(fix="--check" not in sys.argv, auto="--auto" in sys.argv)
    if "--json" in sys.argv:
        print(json.dumps(res))
        return
    for key, mark in (("fixed", "FIXED"), ("failed", "FAILED"), ("manual", "DOWN (needs you)"),
                      ("minor", "minor")):
        for name in res[key]:
            print(f"{mark:18} {name}")
    print(f"{len(res['ok'])} ok")


if __name__ == "__main__":
    main()
