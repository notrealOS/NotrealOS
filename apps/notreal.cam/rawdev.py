"""Own raw developer for the main camera (Sony IMX766), 10-22.

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

W, H = 4096, 3072
BLACK = 64
SAT = 1023 - BLACK                    # raw max after black level (measured 959)
HDR_EV = -3.0
CONTRAST = 0.22
# imx766.yaml CCMs (imx355 copies blended 30% toward identity), by colour temperature
CCMS = {
    2920: [[1.49316, -0.19604, -0.29712], [-0.46582, 1.89281, -0.42699], [-0.11316, -0.62550, 1.73867]],
    3630: [[1.57454, -0.33540, -0.23914], [-0.41098, 1.79598, -0.38501], [-0.08857, -0.52200, 1.61058]],
    4550: [[1.56405, -0.33599, -0.22807], [-0.36481, 1.86332, -0.49850], [-0.08202, -0.45459, 1.53661]],
    5580: [[1.59453, -0.36773, -0.22681], [-0.32122, 1.75545, -0.43423], [-0.08445, -0.39650, 1.48096]],
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


def capture(cam_idx, ev_file, user_ev=0.0, hdr=False, workdir="/dev/shm/nrcamera-raw", timeout=12):
    """-> list of (mosaic uint16 HxW, exposure x gain): [normal] or [normal, dark]."""
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir)
    for n in range(200):                  # the processed stream's frames are not needed
        os.symlink("/dev/null", os.path.join(workdir, f"r-cam0-stream1-{n:06d}.bin"))
    cmd = ["cam", "-c", str(cam_idx), "-s", f"role=raw,width={W},height={H}",
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
            out.append((unpack10(np.fromfile(path, np.uint8)), frames[i]["expo"]))
        except (OSError, ValueError) as e:
            log("frame", i, "unreadable:", e)
    shutil.rmtree(workdir, ignore_errors=True)
    return out


def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def unpack10(b, w=W, h=H):
    """MIPI CSI-2 packed 10-bit (4 pixels in 5 bytes) -> uint16 HxW."""
    b = b[: w * h * 5 // 4].reshape(-1, 5).astype(np.uint16)
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


def ccm_for(gains):
    """Colour matrix for the light: warm light needs little red gain, lots of blue gain."""
    t = np.log(gains[0] / gains[2])        # ~ +0.57 overcast daylight (10-22), ~ -0.5 tungsten
    ct = np.interp(t, [-0.45, 0.0, 0.3, 0.55], [2920, 3630, 4550, 5580])
    cts = sorted(CCMS)
    i = int(np.clip(np.searchsorted(cts, ct) - 1, 0, len(cts) - 2))
    f = float(np.clip((ct - cts[i]) / (cts[i + 1] - cts[i]), 0, 1))
    m = (1 - f) * np.array(CCMS[cts[i]]) + f * np.array(CCMS[cts[i + 1]])
    return m.astype(np.float32), ct


def _guided(p, r, eps):
    """Self-guided filter (edge-aware blur) of a 2-D map."""
    def bx(a):
        return _box(_box(a, 2 * r + 1, 0), 2 * r + 1, 1)
    mp = bx(p)
    var = bx(p * p) - mp * mp
    a = var / (var + eps)
    b = mp - a * mp
    return bx(a) * p + bx(b)


def develop(frames, threads=4):
    """[(mosaic uint16, expo)] (normal first, optional dark) -> HxWx3 uint8, sensor orientation."""
    t0 = time.time()
    hdr = len(frames) > 1
    m, clipq = merge(frames)
    q = _quads(m)
    gains = white_balance(np.clip(q, 0, 1))
    ccm, ct = ccm_for(gains)
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
    log(f"develop {'HDR' if hdr else 'single'}: gains {gains.round(3)}, ct {ct:.0f}, "
        f"expo {expo:.2f}, {time.time() - t0:.1f}s")
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
