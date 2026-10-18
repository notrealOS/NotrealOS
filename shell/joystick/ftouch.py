"""Fake touchscreen: ftouch.py x,y,ms [x,y,ms ...]  (physical px; first point = finger down, last = lift after ms)"""
import fcntl, os, struct, sys, time
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
BTN_TOUCH, ABS_X, ABS_Y, SLOT, MX, MY, TID = 0x14a, 0, 1, 0x2f, 0x35, 0x36, 0x39
W, H = 1080, 2400
fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
fcntl.ioctl(fd, 0x40045564, EV_KEY); fcntl.ioctl(fd, 0x40045564, EV_ABS)
fcntl.ioctl(fd, 0x40045565, BTN_TOUCH)
for a in (ABS_X, ABS_Y, SLOT, MX, MY, TID):
    fcntl.ioctl(fd, 0x40045567, a)
fcntl.ioctl(fd, 0x4004556e, 1)  # INPUT_PROP_DIRECT
mx, mn = [0] * 64, [0] * 64
mx[ABS_X] = mx[MX] = W - 1; mx[ABS_Y] = mx[MY] = H - 1; mx[SLOT] = 4; mx[TID] = 65535
os.write(fd, struct.pack("80sHHHHi64i64i64i64i", b"claude-ftouch", 3, 1, 1, 1, 0, *mx, *mn, *([0] * 64), *([0] * 64)))
fcntl.ioctl(fd, 0x5501)
time.sleep(1.0)  # let the compositor pick the device up
def ev(t, c, v): os.write(fd, struct.pack("qqHHi", 0, 0, t, c, v))
pts = [tuple(int(v) for v in a.split(",")) for a in sys.argv[1:]]
for i, (x, y, ms) in enumerate(pts):
    if i == 0:
        ev(EV_ABS, SLOT, 0); ev(EV_ABS, TID, 1); ev(EV_KEY, BTN_TOUCH, 1)
    ev(EV_ABS, MX, x); ev(EV_ABS, MY, y); ev(EV_ABS, ABS_X, x); ev(EV_ABS, ABS_Y, y); ev(EV_SYN, 0, 0)
    time.sleep(ms / 1000)
ev(EV_ABS, TID, -1); ev(EV_KEY, BTN_TOUCH, 0); ev(EV_SYN, 0, 0)
time.sleep(0.3)
fcntl.ioctl(fd, 0x5502)
