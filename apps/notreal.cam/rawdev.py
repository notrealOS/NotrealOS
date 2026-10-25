"""Own raw developer for the main camera (Sony IMX766, 10-22) and the ultrawide (S5KJN1, 10-25).

The soft ISP's auto white balance misjudges the IMX766 (10-22 window scene: needed R x2.58 /
B x1.46, it used x2.21 / x1.26), so every photo came out green-cyan, worst in the shadows. 12 MP
photos from the main lens are now developed here from the raw Bayer frames instead:

  black level 64 -> (HDR: merge with a -3 EV frame) -> bilinear demosaic -> near-grey white
  balance -> imx766.yaml colour matrix -> chroma denoise -> local tone mapping (edge-aware base
  layer, compressed) -> highlight shoulder -> sRGB -> light sharpening

capture() runs `cam` with the raw stream plus a small processed stream (without it the soft ISP,
and so the auto exposure, never runs). HDR moves our AGC's EV file down 3 stops once the exposure
has settled and keeps the first settled dark frame. The dark frame only replaces the normal one
where the normal one is near clipping, so a moving subject can only ghost inside blown areas.
"""
import concurrent.futures
import glob
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import time

import numpy as np

from film import _box, _upsample

BLACK = 64
SAT = 1023 - BLACK                    # raw max after black level (measured 959)
HDR_EV = -3.0
CONTRAST = 0.22
# Nothing OS tuning for this module (com.qti.tuned.abra_qtech_imx766_wide.bin cc13_bps, decoded
# 10-25 by ~/imx766/tuned on the PC): colour matrices by light level (exposure x gain) and colour temp
CCM_BRIGHT = [[1.80578, -0.91970, 0.11392], [-0.27101, 1.40199, -0.13099], [0.05615, -0.94119, 1.88504]]
CCMS_INDOOR = {
    2450: [[1.59844, -0.41580, -0.18264], [-0.28984, 1.48775, -0.19791], [-0.11530, -0.94746, 2.06277]],
    3000: [[1.56740, -0.47274, -0.09466], [-0.25575, 1.40121, -0.14546], [-0.00605, -0.85636, 1.86241]],
    4000: [[1.64028, -0.57283, -0.06745], [-0.28736, 1.46599, -0.17862], [0.03649, -0.64355, 1.60706]],
    5000: [[1.64455, -0.73567, 0.09112], [-0.18662, 1.27682, -0.09020], [0.04547, -0.76221, 1.71673]],
}
CCMS_LOW = {
    2450: [[1.43860, -0.30474, -0.13386], [-0.20143, 1.33897, -0.13754], [-0.09292, -0.76357, 1.85649]],
    3000: [[1.41066, -0.34215, -0.06851], [-0.16643, 1.26109, -0.09466], [-0.00474, -0.67143, 1.67617]],
    4000: [[1.47625, -0.42608, -0.05017], [-0.19696, 1.31939, -0.12243], [0.02683, -0.47318, 1.44635]],
    5000: [[1.48009, -0.54797, 0.06787], [-0.10054, 1.14913, -0.04865], [0.03458, -0.57964, 1.54511]],
}
# exposure x gain (us): daylight matrix up to BRIGHT_EXPO[0], indoor set from [1], low-light set
# from LOW_EXPO[1] (log blends between); rough guesses, check the "expo" in the develop log line
BRIGHT_EXPO = (3000, 20000)
LOW_EXPO = (250000, 1000000)
CCM_STRENGTH = 1.0                    # < 1 blends toward identity if colours look too strong
# Nothing's AWB reference points (refPtV1, R/G, B/G); the CCT labels are ours (D50 and A match the
# golden-module calibration). Daylight/tungsten curve + the fluorescent branch (TL84/CWF/U30).
AWB_PLANCK = [(0.41964, 0.75670, 7500), (0.46326, 0.69773, 6500), (0.53307, 0.58808, 5000),
              (0.80759, 0.37600, 2850), (0.98964, 0.30375, 2300), (1.24937, 0.20461, 1800)]
AWB_FLUO = [(0.53307, 0.58808, 5000), (0.51909, 0.44181, 4150), (0.54414, 0.44748, 4000),
            (0.56918, 0.45315, 3500), (0.80759, 0.37600, 2850)]
AWB_TOL = 0.10                        # how far (log R/G, B/G) grey-world may stray from those curves

# Ultrawide Samsung S5KJN1 (com.qti.tuned.abra_qtech_s5kjn1_uw.bin, same layout; 10-25)
UW_CCM_BRIGHT = [[2.13204, -1.14411, 0.01207], [-0.29045, 1.37106, -0.08060], [-0.11095, -1.20999, 2.32093]]
UW_CCMS_INDOOR = {
    2450: [[1.74409, -0.68928, -0.05481], [-0.30164, 1.25824, 0.04340], [-0.69386, -1.29385, 2.98772]],
    3150: [[1.71409, -0.71928, 0.00519], [-0.27164, 1.17824, 0.09340], [-0.44386, -1.14385, 2.58772]],
    4050: [[2.30352, -1.44371, 0.14019], [-0.23987, 1.30392, -0.06405], [0.00968, -1.40340, 2.39373]],
    5000: [[1.93822, -0.94822, 0.01000], [-0.19289, 1.24642, -0.05353], [-0.09345, -1.01285, 2.10630]],
}
UW_CCMS_LOW = {
    2450: [[1.63945, -0.59235, -0.04710], [-0.21346, 1.18274, 0.03071], [-0.63129, -1.17717, 2.80845]],
    3000: [[1.61125, -0.61569, 0.00444], [-0.16390, 1.10754, 0.05636], [-0.40046, -1.03199, 2.43245]],
    4000: [[1.77231, -0.92875, 0.15644], [-0.22640, 1.23820, -0.01180], [-0.10088, -1.20425, 2.30513]],
    5000: [[1.64068, -0.49203, -0.14865], [-0.20035, 1.24446, -0.04411], [-0.10052, -0.79512, 1.89564]],
    6500: [[1.65249, -0.57781, -0.07468], [-0.16648, 1.24566, -0.07918], [-0.03050, -0.73890, 1.76940]],
}
UW_AWB_PLANCK = [(0.54883, 0.76064, 7500), (0.59798, 0.71665, 6500), (0.68763, 0.62611, 5000),
                 (0.98043, 0.44782, 2850), (1.20608, 0.40639, 2300), (1.51998, 0.25712, 1800)]
UW_AWB_FLUO = [(0.68763, 0.62611, 5000), (0.67943, 0.47926, 4150), (0.70482, 0.48502, 4000),
               (0.73021, 0.49078, 3500), (0.98043, 0.44782, 2850)]

# Front Sony IMX471 (com.qti.tuned.abra_qtech_imx471_front.bin, 10-25): its daylight matrix also
# depends on the colour temperature; the 2nd-darkest of its 4 light bands is left out
FR_CCMS_BRIGHT = {
    2500: [[2.57472, -1.74335, 0.16863], [-0.31642, 1.17564, 0.14078], [-0.08515, -1.19080, 2.27595]],
    3000: [[1.98195, -1.08714, 0.10519], [-0.21912, 1.21281, 0.00631], [0.02944, -0.84640, 1.81696]],
    3950: [[1.87519, -1.06185, 0.18665], [-0.14725, 1.21108, -0.06382], [0.13709, -1.14706, 2.00998]],
    4800: [[1.87162, -0.99427, 0.12265], [-0.14313, 1.19489, -0.05177], [0.10605, -0.88051, 1.77446]],
}
FR_CCMS_INDOOR = {
    2500: [[2.20495, -1.23313, 0.02818], [-0.25547, 1.20834, 0.04712], [-0.02026, -0.81625, 1.83852]],
    3000: [[1.83594, -0.83402, -0.00192], [-0.20896, 1.22679, -0.01783], [0.01192, -0.71395, 1.70203]],
    3950: [[1.51859, -0.73743, 0.21884], [-0.23294, 1.25382, -0.02088], [0.05385, -0.78024, 1.72639]],
    4800: [[1.73824, -0.73109, -0.00715], [-0.10742, 1.20287, -0.09544], [0.09288, -0.70080, 1.60792]],
}
FR_CCMS_LOW = {
    2500: [[1.95711, -1.13009, 0.17298], [-0.21303, 1.03442, 0.17861], [-0.02286, -0.80313, 1.82599]],
    3000: [[1.76446, -0.73772, -0.02674], [-0.15950, 1.15335, 0.00615], [0.05740, -0.59663, 1.53924]],
    3950: [[1.54151, -0.83487, 0.29336], [-0.02813, 0.93107, 0.09707], [0.23857, -1.00388, 1.76532]],
    4800: [[1.51274, -0.57740, 0.06466], [-0.05851, 1.08869, -0.03018], [0.13372, -0.52740, 1.39368]],
}
FR_AWB_PLANCK = [(0.37287, 0.71611, 7500), (0.41759, 0.66084, 6500), (0.47841, 0.56412, 5000),
                 (0.76349, 0.34126, 2850), (0.94658, 0.27268, 2300), (1.21289, 0.17292, 1800)]
FR_AWB_FLUO = [(0.47841, 0.56412, 5000), (0.50214, 0.40637, 4150), (0.54048, 0.41219, 4000),
               (0.57881, 0.41802, 3500), (0.76349, 0.34126, 2850)]

# per sensor: raw size, columns dropped on the left to make the mosaic RGGB (S5KJN1 is GRBG)
SENSORS = {
    "imx766": dict(w=4096, h=3072, crop_x=0, bright=CCM_BRIGHT, indoor=CCMS_INDOOR, low=CCMS_LOW,
                   planck=AWB_PLANCK, fluo=AWB_FLUO, strength=CCM_STRENGTH),
    "s5kjn1": dict(w=4080, h=3072, crop_x=1, bright=UW_CCM_BRIGHT, indoor=UW_CCMS_INDOOR,
                   low=UW_CCMS_LOW, planck=UW_AWB_PLANCK, fluo=UW_AWB_FLUO, strength=1.0),
    "imx471": dict(w=2304, h=1728, crop_x=0, bright=FR_CCMS_BRIGHT, indoor=FR_CCMS_INDOOR,
                   low=FR_CCMS_LOW, planck=FR_AWB_PLANCK, fluo=FR_AWB_FLUO, strength=1.0),
}


def log(*a):
    print("rawdev:", *a, flush=True)


# ---------- capture ----------
def _settled(frames, start, run=3):
    """Index of the last of `run` frames in a row with the same exposure x gain, from start.
    Our AGC only moves every ~16 frames (10-22), so a short run of equal frames proves little:
    callers also check that the value is where they want it."""
    for i in range(max(start, run - 1), len(frames)):
        v = [frames[j]["expo"] for j in range(i - run + 1, i + 1)]
        if all(v) and max(v) / min(v) < 1.02:
            return i
    return None


def capture(cam_idx, ev_file, user_ev=0.0, hdr=False, workdir="/dev/shm/nrcamera-raw", timeout=12,
            model="imx766"):
    """-> list of (mosaic uint16 HxW, exposure x gain): [normal] or [normal, dark]."""
    sens = SENSORS[model]
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir)
    for n in range(200):                  # the processed stream's frames are not needed
        os.symlink("/dev/null", os.path.join(workdir, f"r-cam0-stream1-{n:06d}.bin"))
    cmd = ["cam", "-c", str(cam_idx), "-s", f"role=raw,width={sens['w']},height={sens['h']}",
           "-s", "role=viewfinder,width=1024,height=768,pixelformat=BGR888",
           "-C200", "--metadata", "-F" + os.path.join(workdir, "r-#.bin")]
    master, slave = pty.openpty()         # a tty, so cam's output arrives line by line
    proc = subprocess.Popen(cmd, stdout=slave, stderr=subprocess.DEVNULL,
                            env=dict(os.environ, LIBCAMERA_SOFTISP_MODE="cpu"))
    os.close(slave)
    frames, buf, normal, dark, ev_at = [], b"", None, None, None
    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            r, _, _ = select.select([master], [], [], 0.5)
            if not r:
                if proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                line = line.decode(errors="replace")
                m = re.search(r"stream0 seq: (\d+)", line)
                if m:
                    frames.append({"seq": int(m.group(1)), "exp": None, "gain": None, "expo": None})
                elif frames and "ExposureTime =" in line:
                    frames[-1]["exp"] = float(line.split("=")[1])
                elif frames and "AnalogueGain =" in line:
                    frames[-1]["gain"] = float(line.split("=")[1])
                if frames and frames[-1]["exp"] and frames[-1]["gain"] and not frames[-1]["expo"]:
                    frames[-1]["expo"] = frames[-1]["exp"] * frames[-1]["gain"]
            if normal is None and len(frames) >= 10:
                normal = _settled(frames, 9)
                if normal is None and len(frames) >= 20:
                    normal = len(frames) - 1          # never settled: take what we have
                if normal is not None and hdr:
                    _write(ev_file, f"{user_ev + HDR_EV:g}")
                    ev_at = len(frames)
            if normal is not None and not hdr:
                break
            if hdr and ev_at is not None:
                ref = frames[normal]["expo"]
                # wait for >= 2.5 stops down (the AGC may stop short at its minimum exposure)
                low = [i for i in range(ev_at, len(frames))
                       if frames[i]["expo"] and frames[i]["expo"] < ref / 5.6]
                if low and frames[-1]["expo"] < ref / 5.6:
                    dark = len(frames) - 1
                    break
                if len(frames) - ev_at > 90:          # ~3.5 s: take the darkest one we got
                    best = min(range(ev_at, len(frames)), key=lambda i: frames[i]["expo"] or 1e12)
                    if frames[best]["expo"] and frames[best]["expo"] < ref / 2:
                        dark = best
                    break
    finally:
        if hdr:
            _write(ev_file, f"{user_ev:g}")
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        os.close(master)
    time.sleep(0.05)
    log(f"cam: {len(frames)} frames in {time.time() - t0:.1f}s, normal {normal}, dark {dark}",
        [round(f["expo"] or 0) for f in frames])
    out = []
    for i in (normal, dark):
        if i is None:
            continue
        path = os.path.join(workdir, f"r-cam0-stream0-{frames[i]['seq']:06d}.bin")
        try:
            m = unpack10(np.fromfile(path, np.uint8), sens["w"], sens["h"])
            c = sens["crop_x"]
            out.append((m[:, c:sens["w"] - c] if c else m, frames[i]["expo"]))
        except (OSError, ValueError) as e:
            log("frame", i, "unreadable:", e)
    shutil.rmtree(workdir, ignore_errors=True)
    return out


def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def unpack10(b, w, h):
    """MIPI CSI-2 packed 10-bit (4 pixels in 5 bytes, rows padded to the buffer's stride) -> uint16 HxW."""
    b = b[: len(b) // h * h].reshape(h, -1)[:, : w * 5 // 4].reshape(-1, 5).astype(np.uint16)
    px = np.empty((b.shape[0], 4), np.uint16)
    for i in range(4):
        px[:, i] = (b[:, i] << 2) | ((b[:, 4] >> (2 * i)) & 3)
    return px.reshape(h, w)


# ---------- develop ----------
def _norm(mosaic):
    return np.clip((mosaic.astype(np.float32) - BLACK) / SAT, -0.05, 1.0)


def _quads(m):
    """RGGB mosaic -> half-res R, G, B (H/2 x W/2 x 3)."""
    return np.stack([m[0::2, 0::2], (m[0::2, 1::2] + m[1::2, 0::2]) * 0.5, m[1::2, 1::2]], -1)


def _shift(ref, mov):
    """Translation (dy, dx) that best aligns mov onto ref (phase correlation)."""
    a = np.log1p(ref * 50) - np.log1p(ref * 50).mean()
    b = np.log1p(mov * 50) - np.log1p(mov * 50).mean()
    win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1])).astype(np.float32)
    f = np.fft.rfft2(a * win) * np.conj(np.fft.rfft2(b * win))
    c = np.fft.irfft2(f / (np.abs(f) + 1e-6), s=a.shape)
    dy, dx = np.unravel_index(np.argmax(c), c.shape)
    dy = dy - a.shape[0] if dy > a.shape[0] // 2 else dy
    dx = dx - a.shape[1] if dx > a.shape[1] // 2 else dx
    return int(dy), int(dx)


def merge(frames):
    """[(mosaic, expo)] -> (linear mosaic float32, 1.0 = normal frame's white; clip map at half res)."""
    n = _norm(frames[0][0])
    qn = _quads(n)
    if len(frames) < 2:
        return n, qn.max(-1)
    d = _norm(frames[1][0])
    k = frames[0][1] / frames[1][1]
    # align at quarter res (green), shift by even pixels to keep the Bayer phase
    gn = _box(_box(qn[..., 1], 2, 0), 2, 1)[::2, ::2]
    gd = _box(_box(_quads(d)[..., 1], 2, 0), 2, 1)[::2, ::2] * k
    dy, dx = _shift(np.minimum(gn, 0.9), np.minimum(gd, 0.9))
    if abs(dy) > 60 or abs(dx) > 60:
        log("bracket shift too large", dy, dx, "-> no merge")
        return n, qn.max(-1)
    d = np.roll(d, (dy * 4, dx * 4), axis=(0, 1))
    qd = _quads(d)
    # weight of the normal frame: 1 below 75 % of clip, 0 from 95 % (per 2x2 quad, all channels)
    wq = np.clip((0.95 - qn.max(-1)) / 0.20, 0, 1)
    # the roll wraps the dark frame's far edge around: use the normal frame there
    sy, sx = dy * 2, dx * 2                   # shift in half-res pixels
    if sy > 0:
        wq[:sy] = 1
    elif sy < 0:
        wq[sy:] = 1
    if sx > 0:
        wq[:, :sx] = 1
    elif sx < 0:
        wq[:, sx:] = 1
    w = np.repeat(np.repeat(wq, 2, 0), 2, 1)
    log(f"merge: k {k:.2f}, shift {dy * 4},{dx * 4}, normal-frame clipped {(wq < 1).mean() * 100:.1f}%")
    return w * n + (1 - w) * d * k, qd.max(-1) * k        # clip map: where even the dark frame clipped


def white_balance(q):
    """Gains (R, G, B) from near-grey mid-tone pixels of the half-res image."""
    l = q.mean(-1)
    lo, hi = np.percentile(l, 5), np.percentile(l, 97)
    ok = (l > lo) & (l < hi) & (q.max(-1) < 0.9 * q.max())
    if ok.sum() < 2000:                    # flat frame (lens cap, wall): use everything
        ok = np.ones_like(ok)
    s = q[ok].astype(np.float64)
    g = s[:, 1].sum() / np.maximum(s.sum(0), 1e-9)
    for _ in range(3):                     # refine on pixels that are close to grey after balancing
        b = s * g
        cr = np.log(np.maximum(b[:, 0], 1e-6) / np.maximum(b[:, 1], 1e-6))
        cb = np.log(np.maximum(b[:, 2], 1e-6) / np.maximum(b[:, 1], 1e-6))
        near = (np.abs(cr) < 0.25) & (np.abs(cb) < 0.25)
        if near.sum() < 2000:
            break
        g = s[near, 1].sum() / np.maximum(s[near].sum(0), 1e-9)
    return np.clip(g / g[1], 1.0, 4.0).astype(np.float32)


def _locus(gains, sens):
    """Grey-world gains -> gains near Nothing's AWB curves (within AWB_TOL) + colour temperature."""
    p = np.log([1 / gains[0], 1 / gains[2]])
    best = None
    for curve in (sens["planck"], sens["fluo"]):
        for (r0, b0, t0), (r1, b1, t1) in zip(curve, curve[1:]):
            a, b = np.log([r0, b0]), np.log([r1, b1])
            f = float(np.clip((p - a) @ (b - a) / ((b - a) @ (b - a)), 0, 1))
            q = a + f * (b - a)
            d = float(np.hypot(*(p - q)))
            if best is None or d < best[0]:
                best = (d, q, 1e6 / ((1 - f) * 1e6 / t0 + f * 1e6 / t1))     # interpolate in mired
    d, q, ct = best
    if d > AWB_TOL:
        p = q + (p - q) * AWB_TOL / d
    rg, bg = np.exp(p)
    return np.float32([1 / rg, 1, 1 / bg]), ct


def _ccm_ct(table, ct):
    cts = sorted(table)
    i = int(np.clip(np.searchsorted(cts, ct) - 1, 0, len(cts) - 2))
    f = float(np.clip((ct - cts[i]) / (cts[i + 1] - cts[i]), 0, 1))
    return (1 - f) * np.array(table[cts[i]]) + f * np.array(table[cts[i + 1]])


def ccm_for(ct, expo, sens):
    """Nothing's colour matrix for the light's colour temperature and level (exposure x gain, us)."""
    le = np.log(max(expo or BRIGHT_EXPO[1], 1.0))
    w_bright = float(np.clip((np.log(BRIGHT_EXPO[1]) - le) / np.log(BRIGHT_EXPO[1] / BRIGHT_EXPO[0]), 0, 1))
    w_low = float(np.clip((le - np.log(LOW_EXPO[0])) / np.log(LOW_EXPO[1] / LOW_EXPO[0]), 0, 1))
    m = _ccm_ct(sens["indoor"], ct) * (1 - w_low) + _ccm_ct(sens["low"], ct) * w_low
    b = sens["bright"]
    m = m * (1 - w_bright) + (_ccm_ct(b, ct) if isinstance(b, dict) else np.array(b)) * w_bright
    m = sens["strength"] * m + (1 - sens["strength"]) * np.eye(3)
    return m.astype(np.float32), w_bright, w_low


def _guided(p, r, eps):
    """Self-guided filter (edge-aware blur) of a 2-D map."""
    def bx(a):
        return _box(_box(a, 2 * r + 1, 0), 2 * r + 1, 1)
    mp = bx(p)
    var = bx(p * p) - mp * mp
    a = var / (var + eps)
    b = mp - a * mp
    return bx(a) * p + bx(b)


def develop(frames, threads=4, model="imx766"):
    """[(mosaic uint16, expo)] (normal first, optional dark) -> HxWx3 uint8, sensor orientation."""
    t0 = time.time()
    hdr = len(frames) > 1
    m, clipq = merge(frames)
    q = _quads(m)
    gw = white_balance(np.clip(q, 0, 1))
    sens = SENSORS[model]
    gains, ct = _locus(gw, sens)
    gains = np.clip(gains, 0.7, 4.0).astype(np.float32)
    ccm, w_bright, w_low = ccm_for(ct, frames[0][1], sens)
    lin_q = np.clip(np.clip(q * gains, 0, None) @ ccm.T, 1e-5, None)
    lum_q = lin_q @ np.float32([0.2126, 0.7152, 0.0722])
    # exposure: the frame's median lands at ~18 % grey (scene key), within 0.7..2.5x of the AGC's
    expo = float(np.clip(0.11 / max(np.percentile(lum_q, 50), 1e-4), 0.7, 2.2))
    # black point: lens flare / haze lifts the darkest areas; pull the darkest 0.2 % to ~0
    flare = float(np.percentile(lum_q, 0.2)) * 0.85
    # local tone mapping: edge-aware base layer of log luminance at 1/8 res, highlights compressed
    small = np.log2(lum_q[::4, ::4] * expo + 1e-4)
    base = _guided(small, 12, 0.35)
    mid = float(np.percentile(base, 50))
    hi_c, lo_c = (0.45, 0.85) if hdr else (0.75, 0.95)
    tgt = np.where(base > mid, mid + (base - mid) * hi_c, mid + (base - mid) * lo_c)
    delta = (tgt - base).astype(np.float32)            # log2 gain per area
    h, w = m.shape
    out = np.empty((h, w, 3), np.uint8)
    M = 6                                              # strip overlap for demosaic + blurs

    def strip(y0, y1):
        a0, a1 = max(0, y0 - M), min(h, y1 + M)
        a0 -= a0 % 2
        rgb = _demosaic(m[a0:a1])
        rgb = np.clip(rgb * gains, 0, None)
        # clipped highlights -> neutral (else they turn pink/cyan after the gains)
        cl = np.clip((_upsample(clipq, h, w, a0, a1) - 0.85) / 0.12, 0, 1)[..., None]
        rgb = rgb + (rgb.max(-1, keepdims=True) - rgb) * cl
        rgb = rgb @ ccm.T - flare
        # chroma denoise: keep luma, blur colour difference ~5x5
        y = rgb @ np.float32([0.25, 0.5, 0.25])
        c = rgb - y[..., None]
        c = _box(_box(_box(_box(c, 5, 0), 5, 1), 3, 0), 3, 1)
        rgb = y[..., None] + c
        rgb = rgb * (expo * np.exp2(_upsample(delta, h, w, a0, a1)))[..., None]
        # highlight shoulder (extended Reinhard, white at 4) on luminance, colour kept
        l = np.maximum(rgb @ np.float32([0.2126, 0.7152, 0.0722]), 1e-6)
        lw = 4.0
        l2 = l * (1 + l / (lw * lw)) / (1 + l) * 1.25
        rgb = np.clip(rgb * (l2 / l)[..., None], 0, 1)
        rgb = np.where(rgb <= 0.0031308, 12.92 * rgb, 1.055 * np.power(rgb, 1 / 2.4) - 0.055)
        rgb = rgb + (rgb * rgb * (3 - 2 * rgb) - rgb) * CONTRAST      # gentle S-curve
        # light sharpening (bilinear demosaic is soft)
        rgb = rgb + 0.6 * (rgb - _box(_box(rgb, 3, 0), 3, 1))
        out[y0:y1] = np.clip(rgb[y0 - a0:y1 - a0] * 255 + 0.5, 0, 255).astype(np.uint8)

    with concurrent.futures.ThreadPoolExecutor(threads) as pool:
        list(pool.map(lambda y: strip(y, min(h, y + 256)), range(0, h, 256)))
    log(f"develop {model} {'HDR' if hdr else 'single'}: grey-world {gw.round(3)} -> gains {gains.round(3)}, "
        f"ct {ct:.0f}, expo {frames[0][1]} (bright {w_bright:.2f}, low {w_low:.2f}), "
        f"key {expo:.2f}, {time.time() - t0:.1f}s")
    return out


def _demosaic(m):
    """Bilinear RGGB demosaic of an even-aligned strip -> h x w x 3 float32."""
    p = np.pad(m, 1, mode="reflect")
    n, s = p[:-2, 1:-1], p[2:, 1:-1]
    wv, e = p[1:-1, :-2], p[1:-1, 2:]
    cross = (n + s + wv + e) * 0.25
    diag = (p[:-2, :-2] + p[:-2, 2:] + p[2:, :-2] + p[2:, 2:]) * 0.25
    hor, ver = (wv + e) * 0.5, (n + s) * 0.5
    r = np.empty_like(m); g = np.empty_like(m); b = np.empty_like(m)
    # R at (even, even), G at (even, odd) + (odd, even), B at (odd, odd)
    r[0::2, 0::2] = m[0::2, 0::2]; g[0::2, 0::2] = cross[0::2, 0::2]; b[0::2, 0::2] = diag[0::2, 0::2]
    r[0::2, 1::2] = hor[0::2, 1::2]; g[0::2, 1::2] = m[0::2, 1::2]; b[0::2, 1::2] = ver[0::2, 1::2]
    r[1::2, 0::2] = ver[1::2, 0::2]; g[1::2, 0::2] = m[1::2, 0::2]; b[1::2, 0::2] = hor[1::2, 0::2]
    r[1::2, 1::2] = diag[1::2, 1::2]; g[1::2, 1::2] = cross[1::2, 1::2]; b[1::2, 1::2] = m[1::2, 1::2]
    return np.stack([r, g, b], -1)


if __name__ == "__main__":
    # test: rawdev.py <raw.bin> [<dark.bin> <k>] <out.jpg>
    import sys
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib
    args = sys.argv[1:]
    fr = [(unpack10(np.fromfile(args[0], np.uint8)), 1.0)]
    if len(args) == 4:
        fr.append((unpack10(np.fromfile(args[1], np.uint8)), 1.0 / float(args[2])))
    img = develop(fr)
    hh, ww = img.shape[:2]
    GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(img.tobytes()), GdkPixbuf.Colorspace.RGB, False, 8,
                                    ww, hh, ww * 3).savev(args[-1], "jpeg", ["quality"], ["92"])
