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
- 10-24 (v2): helpers that battery saver / DAP mode stopped on purpose count as paused, not
  down; duplicate daemons (two voice assistants...) are cut back to one; battery logger,
  backup scheduler (+ its stale lock) and circle-to-search are watched too; modem left on 4G
  after the saver -> 5G again; plus minor notes: no backup lately, Chromium crashing / falling
  back to X11, and battery-log trends (awake with the screen off, drain spikes, heat; trends.py).
  Every result gets plain-language "details" (why + what you can do), is saved to
  $XDG_RUNTIME_DIR/nrdoctor-last.json for the card (card.py), and anything that stays broken
  gets a report in ~/doctor/reports/ (for Claude). --auto also sends the weekly battery
  report (Sunday evening). --do <action> runs one of the safe ACTIONS (card / PC diagnosis).
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
NROUTPUT = f"{HOME}/nrvolume/nroutput.py"   # 10-24 DAC / SPEAKER choice
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
    ("battery logger", "/battery/nrbatteryd.py",
     f"exec {HOME}/battery/nrbatteryd.py >> {HOME}/battery/nrbatteryd.log 2>&1"),
    ("backup scheduler", "/backup/nrbackupd", f"{HOME}/backup/nrbackupd"),
    ("circle to search", "/circle/circled.py", f"{HOME}/circle/circled-loop"),
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
AUTOSTART = os.path.join(HOME, ".config", "autostart")
MODES = (("battery saver", os.path.join(HOME, ".local/state/nrsaver")),   # mode name, state dir
         ("DAP mode", os.path.join(HOME, ".local/state/nrdap")))
LAST = os.path.join(RUN, "nrdoctor-last.json")       # the last result, for the card
REPORTS = os.path.join(HOME, "doctor", "reports")     # hand-offs for Claude
BACKUP_STATE = os.path.join(HOME, ".local/state/nrbackup")
BACKUP_DAYS = 3
CHROMIUM_LOG = os.path.join(HOME, ".cache/chromium-safe.log")
CRASH_DIR = os.path.join(HOME, ".config/chromium/Crash Reports/pending")


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
    # after a restart pulse may pick the wrong output: put back the DAC / SPEAKER choice (10-24)
    subprocess.run([NROUTPUT, "apply"], capture_output=True, timeout=10)
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
    # 10-24: the user picks DAC or SPEAKER (nroutput); only DAC chosen means the DAC must be default
    usb = [n for n, _ in sinks() if ".usb-" in n]
    if not usb:
        return True
    want_dac = sh(NROUTPUT, "status").split()[:1] != ["speaker"]
    return (sh("pactl", "get-default-sink").strip() in usb) == want_dac


def dac_default_fix():
    sh(NROUTPUT, "apply")
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


# --- 10-24 (v2) -------------------------------------------------------------------

def mode_paused():
    """({launcher basenames stopped on purpose}, mode name) while battery saver / DAP mode is on."""
    for mode, st in MODES:
        if os.path.exists(os.path.join(st, "on")):
            names = set()
            for desk in read(os.path.join(st, "daemons")).split():
                ex = re.search(r"^Exec=(.*)$", read(os.path.join(AUTOSTART, desk)), re.M)
                if ex:
                    names.add(os.path.basename(ex.group(1).split()[0]))
            return names, mode
    return set(), None


def start_time(pid):
    f = read(f"/proc/{pid}/stat").rsplit(")", 1)
    return int(f[1].split()[19]) if len(f) == 2 else 0


def ppid(pid):
    f = read(f"/proc/{pid}/stat").rsplit(")", 1)
    return int(f[1].split()[1]) if len(f) == 2 else 0


def duplicates(match, launcher):
    """[pids to kill] when a .py daemon runs more than once: keep the instance started first
    (by its wrapper loop, if it has one), drop the rest together with their own wrappers."""
    if not match.endswith(".py"):
        return []
    inst = [p for p, uid, cmd in procs() if uid == UID and match in cmd and cmd.startswith(("python", "/usr/bin/python", "/usr/sbin/python"))]
    if len(inst) < 2:
        return []
    wrapper = os.path.basename(launcher.split()[0])

    def unit(pid):
        parent = ppid(pid)
        own_wrapper = parent > 1 and wrapper in read(f"/proc/{parent}/cmdline").replace("\0", " ")
        return (start_time(parent) if own_wrapper else start_time(pid)), ([parent] if own_wrapper else []) + [pid]
    units = sorted(unit(p) for p in inst)
    shared = set(units[0][1])
    return [p for _, pids in units[1:] for p in pids if p not in shared]


def free_backup_lock():
    """10-24: a killed nrbackupd left its `sleep 900` child holding daemon.lock (flock), so every
    new nrbackupd exited at once. Kill whatever still holds it when no nrbackupd runs."""
    lock = os.path.join(BACKUP_STATE, "daemon.lock")
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            if any(os.readlink(f"/proc/{d}/fd/{fd}") == lock for fd in os.listdir(f"/proc/{d}/fd")):
                os.kill(int(d), signal.SIGTERM)
        except OSError:
            continue


def modem_modes():
    out = sh("mmcli", "-m", "any")
    m = re.search(r"current: allowed: (.*); preferred: (\w+)", out)
    return (m.group(1), m.group(2)) if m else (None, None)


def modem_5g_ok():
    """Battery saver puts the modem on 4G; with the saver off it must be back on 5G."""
    if mode_paused()[1]:
        return True
    allowed, _ = modem_modes()
    return allowed is None or "5g" in allowed or "5gnr" not in sh("mmcli", "-m", "any")


def modem_5g_fix():
    sh("mmcli", "-m", "any", "--set-allowed-modes=2g|3g|4g|5g", "--set-preferred-mode=5g", timeout=20)
    return wait_for(modem_5g_ok, 5)


def backup_notes():
    st = read(os.path.join(BACKUP_STATE, "status.json"))
    last = os.path.join(BACKUP_STATE, "last-ok")
    age = (time.time() - os.path.getmtime(last)) / 86400 if os.path.exists(last) else None
    notes = []
    if age is None or age > BACKUP_DAYS:
        notes.append("no backup in %s (needs the charger at night)" %
                     ("ever" if age is None else f"{age:.0f} days"))
    try:
        j = json.loads(st) if st else {}
        if j.get("ok") is False and time.time() - j.get("time", 0) < 86400:
            notes.append(f"last backup failed: {j.get('msg', '?')}")
    except ValueError:
        pass
    return notes


def chromium_notes():
    notes = []
    day = time.time() - 86400
    falls = 0
    for line in read(CHROMIUM_LOG).splitlines():
        try:
            t = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        falls += t > day and "fallback" in line

    try:
        dumps = [f for f in os.listdir(CRASH_DIR) if f.endswith(".dmp")
                 and os.path.getmtime(os.path.join(CRASH_DIR, f)) > day]
    except OSError:
        dumps = []
    browser = 0
    for f in dumps:
        try:
            with open(os.path.join(CRASH_DIR, f), "rb") as fh:
                data = fh.read()
            i = data.find(b"ptype")          # crashpad annotation: "ptype" then the process type
            browser += i >= 0 and b"browser" in data[i:i + 40]
        except OSError:
            pass
    if falls and browser <= falls:
        notes.append(f"Chromium crashed at launch {falls}× today (opened in X11 instead)")
    elif browser:
        notes.append(f"Chromium crashed {browser}× today" + (f" ({falls} at launch, opened in X11)" if falls else ""))
    return notes


def trend_notes():
    try:
        import trends
        return trends.notes()
    except Exception as e:  # noqa: BLE001 - trends are a bonus, never break the check
        log(f"trends: {e!r}")
        return []


# plain language for the card: (start of a result name, why it matters, what you can do)
EXPLAIN = [
    ("sound (pulseaudio)", "The sound server stopped, so no app can play or record sound.",
     "I restart it by myself. If it keeps failing: reboot."),
    ("free space for logs", "The small memory area for logs is full; background helpers start dying.",
     "I empty the biggest logs by myself."),
    ("mobile network", "The modem isn't connected to Tele2: no calls, SMS or mobile data.",
     "Turn mobile data / flight mode off and on. If it stays down: reboot, then check the SIM."),
    ("SMS delivery", "Text messages are stuck on the modem: the chat app isn't picking them up.",
     "I restart the chat app by myself; the messages then show up."),
    ("call audio routing", "The voice paths for calls are switched off: calls would be silent.",
     "I switch them back on by myself."),
    ("speaker sample format", "The speaker runs in a format the audio chip garbles (robotic sound).",
     "I restart the sound server by myself."),
    ("USB-C device detected", "Something is plugged into USB-C but the phone doesn't see it.",
     "Unplug it and plug it back in. If it stays dead: reboot."),
    ("DAC is the default output", "The KA5 is plugged in but sound goes to the speaker.",
     "I move the sound to the KA5 by myself."),
    ("DAC volume", "The KA5's second volume control dropped, so everything is very quiet.",
     "I turn it back up by myself."),
    ("camera services", "Two camera services are fighting, so the camera stays black.",
     "I restart them by myself. Close the camera app first if it's open."),
    ("internet", "The phone has no internet right now.",
     "Check mobile data (top bar). Chats, mail and the PC links wait until it's back."),
    ("home phone link", "Can't reach the Android phone at home (EasyPark, Uber, SL...).",
     "Make sure that phone is on and on Wi-Fi; its wireless debugging may have switched off."),
    ("sleep fix", "Bluetooth is allowed to wake the phone, so it barely sleeps (battery drain).",
     "Reboot (the fix is applied at boot). If it comes back: tell Claude."),
    ("disk space", "The phone's storage is almost full.",
     "Delete videos / downloads, or ask Claude what is safe to remove."),
    ("music server (PC)", "The PC isn't answering: music streaming, voice answers and backups wait.",
     "Check the PC is on and awake (and Tailscale is running there)."),
    ("chat server (PC)", "The PC's chat server isn't answering: WhatsApp / Messenger messages wait.",
     "Check the PC is on and awake (and Tailscale is running there)."),
    ("PC link relayed", "The phone reaches the PC only through a slow relay.",
     "Usually fixes itself; on the same Wi-Fi as the PC it's direct."),
    ("modem on 5G", "The modem was left on 4G after battery saver.",
     "I put it back on 5G by myself."),
    ("duplicate", "A background helper was running twice (they fight over the same keys / files).",
     "I stopped the extra copy by myself."),
    ("battery hot", "The battery is hot: something works hard or the phone sits in the sun.",
     "Close heavy apps (video calls, games), take it off the charger for a while."),
    ("battery low", "The battery is nearly empty.", "Plug it in. Battery saver switches on at 20 %."),
    ("low memory", "Memory is almost full, so apps get slow or closed.",
     "Close the biggest app shown, or reboot."),
    ("Chromium crashed at launch", "Chromium's Wayland start crashed and it fell back to X11 (keyboard issues).",
     "Close and reopen Chromium. If it keeps happening: tell Claude (see the report)."),
    ("Chromium crashed", "Chromium crashed today.", "Reopen it. If it repeats: tell Claude."),
    ("no backup", "The phone hasn't been backed up to the PC lately.",
     "Leave it on the charger tonight (the backup needs charger + internet)."),
    ("last backup failed", "The last backup to the PC failed.", "Make sure the PC is on; it retries hourly."),
    ("stayed awake", "The phone didn't sleep with the screen off: that's where battery goes.",
     "Close the app named; if it's a background helper, tell Claude."),
    ("screen-off drain", "Battery drains much faster than usual with the screen off.",
     "Something new keeps it busy; see the busiest processes, or ask the PC."),
    ("sleep drain", "Battery drains faster than usual while asleep.",
     "A radio or app keeps waking it; ask the PC or tell Claude."),
    ("got hot", "The phone ran hot in the last hour.", "Close the busiest app shown; let it cool down."),
    ("using", "One process keeps a CPU core busy (heat + drain).",
     "Close that app if it's yours; if it's a helper, tap CHECK AGAIN or tell Claude."),
]


def explain(name):
    for start, why, todo in EXPLAIN:
        if name.startswith(start) or start in name:
            return {"why": why, "todo": todo}
    if any(name.startswith(n) for n, _ in SYSTEM):
        return {"why": "A system service (root) is down.", "todo": "Reboot. If it stays down: tell Claude."}
    if any(name.split(" (")[0] == n for n, _, _ in DAEMONS):
        return {"why": f"A background helper isn't running ({name.split(' (')[0]}).",
                "todo": "Tap CHECK AGAIN to restart it. If it keeps stopping, it is crashing: see the report."}
    return {"why": "Something looks off.", "todo": "Tap ASK THE PC for a diagnosis, or tell Claude."}


# safe actions the card and the PC diagnosis may run (id -> label, fn); never anything else
def _restart_daemon(name):
    for n, match, launcher in DAEMONS:
        if n == name:
            kill(find(match))
            launch(launcher)
            return bool(wait_for(lambda: find(match), 8))
    return False


ACTIONS = {
    "sound": ("Restart the sound server", pulse_fix),
    "call_audio": ("Switch the call voice paths on", mixers_fix),
    "modem_5g": ("Put the modem back on 5G", modem_5g_fix),
    "free_logs": ("Empty big logs", run_fix),
    "camera": ("Restart the camera services", camera_services_fix),
    "usb_reset": ("Reset the USB-C port", usb_reset),
    "home_phone": ("Reconnect the home Android phone", adb_fix),
    "saver_on": ("Turn battery saver on", lambda: subprocess.run([os.path.join(HOME, "saver/nrsaver"), "on"], timeout=60).returncode == 0),
    "saver_off": ("Turn battery saver off", lambda: subprocess.run([os.path.join(HOME, "saver/nrsaver"), "off"], timeout=60).returncode == 0),
}
for _n, _m, _l in DAEMONS:
    ACTIONS["restart:" + _n] = (f"Restart {_n}", lambda n=_n: _restart_daemon(n))


def do_action(aid):
    if aid not in ACTIONS:
        return {"ok": False, "msg": f"unknown action {aid}"}
    label, fn = ACTIONS[aid]
    try:
        ok = bool(fn())
    except Exception as e:  # noqa: BLE001
        log(f"action {aid} failed: {e!r}")
        ok = False
    log(f"action {aid}: {'ok' if ok else 'FAILED'}")
    return {"ok": ok, "msg": label + (" - done" if ok else " - didn't work")}


REPORT_LOGS = {   # which logs go into a report for an issue (start of its name -> paths)
    "sound": [os.path.join(RUN, "nrdoctor.log")],
    "Chromium": [CHROMIUM_LOG],
    "no backup": [os.path.join(BACKUP_STATE, "backup.log")],
    "last backup": [os.path.join(BACKUP_STATE, "backup.log")],
    "sleep": [os.path.join(HOME, "sleepd/sleepd.log")],
    "stayed awake": [os.path.join(HOME, "sleepd/sleepd.log")],
    "swipe keyboard": [os.path.join(RUN, "swipekbd.log")],
    "voice assistant": [os.path.join(RUN, "voiced.log")],
    "notarealChat": [os.path.join(RUN, "notarealchat.log")],
}


def tail(path, n=40):
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


def write_report(issue, res=None, note=""):
    """~/doctor/reports/<date>-<slug>.md, at most one per issue per day. Returns the path."""
    os.makedirs(REPORTS, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", issue.lower()).strip("-")[:40] or "report"
    path = os.path.join(REPORTS, time.strftime("%Y-%m-%d-") + slug + ".md")
    if os.path.exists(path) and not note:
        return path
    ex = explain(issue)
    parts = [f"# {issue}", "", f"{time.strftime('%Y-%m-%d %H:%M')} · up {float(read('/proc/uptime', '0').split()[0]) / 3600:.1f} h",
             "", f"**Why it matters:** {ex['why']}", f"**Suggested:** {ex['todo']}"]
    if note:
        parts += ["", "**Note:** " + note]
    if res:
        parts += ["", "## Doctor result (problems only)", "```",
                  json.dumps({k: res.get(k) for k in ("fixed", "failed", "manual", "minor", "mode") if res.get(k)}, indent=1), "```"]
    asked = json.loads(read(os.path.join(RUN, "nrdoctor-ask.json")) or "{}")
    if asked and time.time() - asked.get("time", 0) < 6 * 3600:
        parts += ["", f"## The PC's diagnosis ({time.strftime('%H:%M', time.localtime(asked['time']))}: {asked.get('question', '')})",
                  asked.get("error") or asked.get("summary", ""), "", "Cause: " + asked.get("cause", ""),
                  *["- " + st for st in asked.get("steps", [])]]
    parts += ["", "## Battery / heat", "```", "\n".join(trend_notes()) or "(nothing unusual)", "```"]
    logs = [os.path.join(RUN, "nrdoctor.log")]
    for start, paths in REPORT_LOGS.items():
        if issue.startswith(start) or start in issue:
            logs += paths
    quiet = re.compile(r"check: \d+ ok, fixed \[\], failed \[\], down \[\]")   # routine all-OK lines
    for pth in dict.fromkeys(logs):
        t = "".join(l for l in tail(pth, 200).splitlines(True) if not quiet.search(l))[-6000:]
        if t:
            parts += ["", f"## {pth}", "```", t.rstrip(), "```"]
    k = tail(os.path.join(HOME, "crashlog/kmsg.log"), 400)
    errs = [l for l in k.splitlines() if re.search(r"error|fail|timeout|oops|bug:", l, re.I)][-25:]
    if errs:
        parts += ["", "## kernel errors (last 25)", "```", "\n".join(errs), "```"]
    with open(path, "w") as f:
        f.write("\n".join(parts) + "\n")
    log(f"report written: {path}")
    return path


WEEKLY = os.path.join(HOME, ".local/state/nrdoctor-weekly")


def weekly_report():
    """--auto: Sunday from 19:00, once per ISO week, the week's battery report as a notification."""
    now = datetime.datetime.now()
    week = now.strftime("%G-W%V")
    if now.weekday() != 6 or now.hour < 19 or read(WEEKLY) == week:
        return
    try:
        import trends
        text = trends.text_report(7)
    except Exception as e:  # noqa: BLE001
        log(f"weekly report: {e!r}")
        return
    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, f"weekly-{week}.md"), "w") as f:
        f.write(f"# Battery week {week}\n\n{text}\n")
    with open(WEEKLY, "w") as f:
        f.write(week)
    subprocess.Popen(["notify-send", "-a", "System check", "Your battery week", text])


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

    paused, mode = mode_paused()

    def restart(name, match, launcher, force=False):
        if os.path.basename(launcher.split()[0]) in paused and not force:
            res["ok"].append(f"{name} (paused by {mode})")
            return
        extra = duplicates(match, launcher)
        if extra:
            if fix:
                kill(extra)
                res["fixed"].append(f"duplicate {name} (stopped the extra copy)")
                log(f"duplicate {name}: killed {extra}")
            else:
                res["minor"].append(f"duplicate {name} running")
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
        if name == "backup scheduler":
            free_backup_lock()
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
        check("output is the one picked (DAC / SPEAKER)", dac_default_ok, dac_default_fix)
        check("DAC volume (2nd control at max)", dac_volume_ok, dac_volume_fix)
    check("camera services (1 pipewire + 1 wireplumber)", camera_services_ok, camera_services_fix)
    check("internet", lambda: reachable(INTERNET, 6), bad="manual")
    if DROID:
        check("home phone link (droid apps)", adb_ok, adb_fix, bad="minor")
    check("sleep fix (Bluetooth wake off)", sleep_ok, bad="manual")
    check("modem on 5G", modem_5g_ok, modem_5g_fix, bad="minor")
    res["minor"] += battery_notes() + [n for n in [ram_note()] if n] + cpu_hogs()
    res["minor"] += backup_notes() + chromium_notes() + trend_notes()
    if mode:
        res["mode"] = mode
    check("disk space", lambda: use_pct("/") < 90, bad="minor")

    for name, match in SYSTEM:
        (res["ok"] if find(match, mine=False) else res["manual"]).append(name)

    check("music server (PC)", lambda: reachable(MUSIC_SERVER, 5), bad="manual")
    check("chat server (PC)", lambda: reachable(CHAT_SERVER), bad="manual")
    if "tailscale" in res["ok"] and pc_relayed():
        res["minor"].append("PC link relayed (slow chats)")
    save_state(state)
    res["details"] = {n: explain(n) for k in ("fixed", "failed", "manual", "minor") for n in res[k]}
    res["time"] = time.time()
    try:
        with open(LAST, "w") as f:
            json.dump(res, f)
    except OSError:
        pass
    for n in res["failed"] + res["manual"]:     # still broken after trying: a hand-off for Claude
        write_report(n, res)
    if fix or res["failed"] or res["manual"]:
        log(f"check: {len(res['ok'])} ok, fixed {res['fixed']}, failed {res['failed']}, "
            f"down {res['manual']}" + ("" if fix else " (check only)"))
    return res


PAUSE = os.path.join(RUN, "nrdoctor.pause")   # sleep tests stop daemons on purpose (10-17)


def main():
    if "--do" in sys.argv:                       # one safe action (card / PC diagnosis)
        print(json.dumps(do_action(sys.argv[sys.argv.index("--do") + 1])))
        return
    if "--report" in sys.argv:                   # card's SAVE REPORT: snapshot + note for Claude
        i = sys.argv.index("--report")
        note = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        last = json.loads(read(LAST) or "{}") or None
        print(write_report("manual report " + time.strftime("%H%M"), last, note or "saved from the card"))
        return
    if "--auto" in sys.argv:
        weekly_report()
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
