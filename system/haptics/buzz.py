#!/usr/bin/env python3
"""buzz.py [ms] [strength 0-100] - one FF_RUMBLE buzz on the pm8350b-haptics input device."""
import fcntl, glob, os, struct, sys, time

EVIOCSFF = 0x40304580          # _IOW('E', 0x80, struct ff_effect), 48 bytes on aarch64
EV_FF, FF_RUMBLE = 0x15, 0x50


def find():
    for d in glob.glob("/sys/class/input/event*"):
        try:
            if open(d + "/device/name").read().strip() == "pm8350b-haptics":
                return "/dev/input/" + os.path.basename(d)
        except OSError:
            pass
    sys.exit("pm8350b-haptics not found (module loaded?)")


def main():
    ms = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    strength = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    mag = min(0xFFFF, strength * 0xFFFF // 100)
    fd = os.open(find(), os.O_RDWR)
    # type, id, direction, trigger(button, interval), replay(length, delay), pad, rumble(strong, weak)
    effect = bytearray(struct.pack("HhHHHHH2xHH", FF_RUMBLE, -1, 0, 0, 0, ms, 0, mag, mag).ljust(48, b"\0"))
    fcntl.ioctl(fd, EVIOCSFF, effect)
    eid = struct.unpack_from("h", effect, 2)[0]
    os.write(fd, struct.pack("llHHi", 0, 0, EV_FF, eid, 1))
    time.sleep(ms / 1000 + 0.05)
    os.close(fd)


main()
