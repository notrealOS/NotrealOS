#!/usr/bin/env python3
"""nroutput: pick where sound plays, DAC or SPEAKER (10-24).

The choice is ~/.local/state/nroutput/prefer ("dac" or "speaker", default dac).
DAC = the USB DAC's sink when one is plugged in, else the speaker. Everything plays on the
default sink (stream-restore no longer pins apps to a device), so applying a choice = set the
default sink + move every open stream there.

  nroutput dac | speaker | toggle   choose and apply
  nroutput apply                    apply the saved choice (after a replug / pulse restart)
  nroutput status                   "<prefer> <sink in use>"

nrvolumed applies the choice when sinks come and go; nrdoctor checks against it.
"""

import os
import sys

import pulsectl

STATE_DIR = os.path.expanduser("~/.local/state/nroutput")
PREFER = os.path.join(STATE_DIR, "prefer")


def prefer():
    try:
        with open(PREFER) as f:
            v = f.read().strip()
        return v if v in ("dac", "speaker") else "dac"
    except OSError:
        return "dac"


def set_prefer(v):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = PREFER + ".tmp"
    with open(tmp, "w") as f:
        f.write(v + "\n")
    os.replace(tmp, PREFER)


def is_dac(name):
    return ".usb-" in name


def wanted(pulse, pref=None):
    """Name of the sink the choice points at, or None when there is no such sink at all."""
    names = [s.name for s in pulse.sink_list() if not s.name.endswith(".monitor")]
    dac = [n for n in names if is_dac(n)]
    spk = [n for n in names if not is_dac(n) and "Speaker" in n] or [n for n in names if not is_dac(n)]
    if (pref or prefer()) == "dac" and dac:
        return dac[0]
    return (spk or dac or [None])[0]


def unpin(pulse):
    # pactl/pulsectl moves get saved as the app's device by module-stream-restore when it runs
    # without restore_device=false (until pulse restarts with ~/.config/pulse/default.pa)
    pinned = [e for e in pulse.stream_restore_list() if e.device]
    for e in pinned:
        e.device = None
    if pinned:
        pulse.stream_restore_write(pinned, mode="replace", apply_immediately=False)


def apply(pulse, pref=None):
    """Make the chosen sink the default and move every stream onto it. True if anything changed."""
    name = wanted(pulse, pref)
    if not name:
        return False
    changed = False
    if pulse.server_info().default_sink_name != name:
        pulse.sink_default_set(name)
        changed = True
    sink = pulse.get_sink_by_name(name)
    for si in pulse.sink_input_list():
        if si.sink != sink.index:
            try:
                pulse.sink_input_move(si.index, sink.index)
                changed = True
            except pulsectl.PulseOperationFailed:
                pass   # a stream that can't move (e.g. it ended meanwhile)
    if changed:
        unpin(pulse)
    return changed


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    with pulsectl.Pulse("nroutput") as pulse:
        if cmd in ("dac", "speaker"):
            set_prefer(cmd)
        elif cmd == "toggle":
            set_prefer("speaker" if prefer() == "dac" else "dac")
        elif cmd not in ("apply", "status"):
            print(__doc__.strip())
            return 2
        if cmd != "status":
            apply(pulse)
        print(prefer(), pulse.server_info().default_sink_name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
