"""Automatic battery saver (10-24), driven by nrbatteryd once a minute.

- unplugged and at AUTO_AT % or less -> saver on (remembered as "auto")
- plugged in -> an auto-started saver goes off again (one you turned on yourself stays on)
- turned off by hand while low -> left off until the battery is back above REARM_AT % or charging
"""
import os
import subprocess

AUTO_AT = 20
REARM_AT = 25
SAVER = os.path.expanduser("~/saver/nrsaver")
STATE = os.path.expanduser("~/.local/state/nrsaver")
FLAG = os.path.join(STATE, "on")
AUTO = os.path.join(STATE, "auto")          # the saver that's on was started by us
DAP = os.path.expanduser("~/.local/state/nrdap/on")


def decide(pct, charging, on, auto, armed):
    """-> (action: "on" | "off" | None, armed)"""
    if charging:
        return ("off" if on and auto else None), True
    if pct > REARM_AT:
        armed = True
    if armed and pct <= AUTO_AT and not on:
        return "on", False
    return None, armed


class AutoSaver:
    def __init__(self):
        self.armed = True

    def step(self, pct, charging):
        if pct is None:
            return None
        on, auto = os.path.exists(FLAG), os.path.exists(AUTO)
        if auto and not on:                 # turned off by hand (or by the app) since: forget it
            os.remove(AUTO)
            auto = False
        action, self.armed = decide(pct, charging, on, auto, self.armed)
        if action == "on" and os.path.exists(DAP):
            return None                     # DAP mode already saves more
        if action == "on":
            os.makedirs(STATE, exist_ok=True)
            open(AUTO, "w").close()
            subprocess.Popen([SAVER, "on", "auto"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif action == "off":
            os.remove(AUTO)
            subprocess.Popen([SAVER, "off", "auto"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return action
