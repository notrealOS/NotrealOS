#!/usr/bin/python3
"""afd - focus the rear cameras of the Nothing Phone (1).

Main (IMX766 wide): AKM AK7377 at 0x0c on CCI bus 19, 1.8 V via GPIO 96 (regulator-camw-vaf,
also without consumer). Position is 10-bit, sent as a 16-bit word (pos << 6) to reg 0x00;
reg 0x02 = 0 wakes it. 10-13 sweep: sharp ~192 for a scene at 1-2 m, 1023 very blurry.
Position file ~/.config/af-position-wide (default 192).

Ultrawide (s5kjn1):

The lens sits on a Dongwoon DW9800 voice-coil driver at 0x0c on CCI bus 18. Its 1.8 V
supply (vreg_camu_vaf_1p8, TLMM GPIO 71) exists in the device tree but has no consumer,
so the lens rested unpowered at its close-focus end. While the sensor is streaming
(runtime PM "active"), this powers the motor and holds the lens at the position in
POSITION_FILE (default 400 = sharp from ~1 m to infinity; factory infinity cal = 404).
Writing a new number to that file refocuses live (0..1023, higher = closer).
"""
import fcntl
import os
import pwd
import subprocess
import time

I2C_SLAVE = 0x0703
FIXED_DRV = "/sys/bus/platform/drivers/reg-fixed-voltage"
POLL_S = 0.4
FAST_POLL = 0.03      # while a lens is powered: notarealCamera's tap-to-focus sweeps the file (10-16)
HOME = os.path.join(pwd.getpwuid(int(os.environ.get("AFD_UID", "10000"))).pw_dir, ".config")   # the phone user (postmarketOS: uid 10000)
CAMERAS = [
    dict(name="uw", bus="/dev/i2c-18", gpio=71, regulator="regulator-camu-vaf",
         pm="/sys/bus/i2c/devices/18-002d/power/runtime_status",
         pos_file=f"{HOME}/af-position", default=400, chip="dw9800"),
    dict(name="wide", bus="/dev/i2c-19", gpio=96, regulator="regulator-camw-vaf",
         pm="/sys/bus/i2c/devices/19-0010/power/runtime_status",
         pos_file=f"{HOME}/af-position-wide", default=192, chip="ak7377"),
]


def log(*a):
    print("afd:", *a, flush=True)


class Motor:
    def __init__(self, cam):
        self.cam = cam
        self.gpio = None
        self.fd = None
        self.pos = None

    def on(self):
        if self.gpio:
            return
        c = self.cam
        if os.path.exists(f"{FIXED_DRV}/{c['regulator']}"):   # the unused regulator holds the GPIO
            with open(f"{FIXED_DRV}/unbind", "w") as f:
                f.write(c["regulator"])
        self.gpio = subprocess.Popen(["gpioset", "-c", "gpiochip4", f"{c['gpio']}=1"])
        time.sleep(0.05)
        self.fd = os.open(c["bus"], os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, 0x0C)
        for _ in range(10):
            try:
                self._write(0x02, 0x00)           # control: power-down bit clear
                break
            except OSError:
                time.sleep(0.02)
        self.pos = 0
        log(self.cam["name"], "on")

    def _write(self, reg, *vals):
        os.write(self.fd, bytes((reg,) + vals))

    def move(self, pos, steps=1):
        start = self.pos or 0
        for i in range(1, steps + 1):
            p = round(start + (pos - start) * i / steps)
            if self.cam["chip"] == "ak7377":
                w = p << 6
                self._write(0x00, w >> 8, w & 0xFF)
            else:
                self._write(0x03, p >> 8, p & 0xFF)
            if steps > 1:
                time.sleep(0.015)
        self.pos = pos

    def off(self):
        if not self.gpio:
            return
        try:
            self.move(0, steps=6)                  # set the lens down gently, no click
        except OSError:
            pass
        os.close(self.fd)
        self.gpio.terminate()
        self.gpio.wait()
        self.gpio = self.fd = self.pos = None
        log(self.cam["name"], "off")


def wanted_position(cam):
    try:
        with open(cam["pos_file"]) as f:
            return max(0, min(1023, int(f.read().split()[0])))
    except (OSError, ValueError, IndexError):
        return cam["default"]


def tick(m):
    c = m.cam
    try:
        with open(c["pm"]) as f:
            streaming = f.read().strip() == "active"
        if streaming:
            m.on()
            want = wanted_position(c)
            if want != m.pos:
                m.move(want, steps=4)
                log(c["name"], "position", want)
        else:
            m.off()
    except OSError as e:
        log(c["name"], "error:", e)
        m.off()


def main():
    subprocess.run(["modprobe", "i2c-dev"], check=False)
    motors = [Motor(c) for c in CAMERAS if os.path.exists(c["pm"])]
    try:
        while True:
            for m in motors:
                tick(m)
            time.sleep(FAST_POLL if any(m.gpio for m in motors) else POLL_S)
    finally:
        for m in motors:
            m.off()


if __name__ == "__main__":
    main()
