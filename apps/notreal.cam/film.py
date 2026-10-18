"""35 mm film looks for notarealCamera (10-18). One definition drives the live preview (GLSL for
GStreamer's glshader) and the saved photo (numpy), so the viewfinder shows what gets saved.

Per stock, in order: exposure, a soft highlight shoulder, a gentle S-curve, saturation that keeps
warm tones (skin, oranges) and mutes greens, a midtone tint, lifted/tinted blacks and capped whites
(lift/gain), red-orange halation around highlights, vignette, grain. Photos are cropped to the
35 mm 3:2 frame (always along the sensor's short axis) and get the notreal.camera watermark.
Numbers were matched to the user's example scans in ~/Downloads/Camera examples."""
import concurrent.futures

import numpy as np

STOCKS = {
    # Kodak Portra 400/800: warm skin, teal-green shadows, soft contrast
    "portra": dict(label="PORTRA", ex=1.10, shoulder=1.4, contrast=0.18, sat=0.92, warm=0.45,
                   green=0.30, mid=(-0.004, 0.006, 0.000), lift=(0.026, 0.036, 0.036),
                   gain=(0.975, 0.955, 0.915), halation=0.18, vignette=0.40, grain=0.020),
    # Fujicolor C200: airy, faded warm blacks (blue crushed), capped whites, cool clean highlights
    "c200": dict(label="C200", ex=1.20, shoulder=1.6, contrast=0.15, sat=0.85, warm=0.35,
                 green=0.15, mid=(0.010, 0.004, -0.010), lift=(0.075, 0.072, 0.035),
                 gain=(0.895, 0.890, 0.880), halation=0.14, vignette=0.34, grain=0.017),
}
ORDER = [None, "portra", "c200"]
KEEP = 0.8889          # 4:3 -> 3:2 keeps this much of the sensor's short side
HALO_TINT = (1.0, 0.45, 0.25)
HALO_R = 0.012         # halation radius, fraction of the sensor's long side
LUMA = (0.299, 0.587, 0.114)
WATERMARK = "notreal.camera"


def label(stock):
    return STOCKS[stock]["label"] if stock in STOCKS else "OFF"


def _v(t):
    return "vec3(%.4f, %.4f, %.4f)" % tuple(t)


def glsl(stock, frame=True):
    """Fragment shader for glshader. frame: dim what the 3:2 photo will crop away."""
    s = STOCKS.get(stock)
    if not s:
        return None
    k = s["shoulder"]
    return f"""#ifdef GL_ES
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif
#endif
varying vec2 v_texcoord;
uniform sampler2D tex;
uniform float time;
float hash(vec2 p) {{ p = fract(p * vec2(0.1031, 0.1030)); p += dot(p, p.yx + 33.33);
  return fract((p.x + p.y) * p.x); }}
float bright(vec2 uv) {{ return clamp((dot(texture2D(tex, uv).rgb, vec3{LUMA}) - 0.7) / 0.3, 0.0, 1.0); }}
void main () {{
  vec2 uv = v_texcoord;
  vec4 s = texture2D(tex, uv);
  vec3 c = clamp(s.rgb * {s['ex']:.4f}, 0.0, 4.0);
  c = (1.0 - exp(-{k:.4f} * c)) / {1 - np.exp(-k):.5f};
  c = clamp(c, 0.0, 1.0);
  c = mix(c, c * c * (3.0 - 2.0 * c), {s['contrast']:.4f});
  float l = dot(c, vec3{LUMA});
  float warm = clamp((c.r - c.b) * 3.0, 0.0, 1.0) * clamp((c.r - c.g) * 4.0 + 0.3, 0.0, 1.0);
  float green = clamp((c.g - max(c.r, c.b)) * 5.0, 0.0, 1.0);
  c = l + (c - l) * ({s['sat']:.4f} + {s['warm']:.4f} * warm - {s['green']:.4f} * green);
  c += {_v(s['mid'])} * 4.0 * l * (1.0 - l);
  c = {_v(s['lift'])} + c * ({_v(s['gain'])} - {_v(s['lift'])});
  vec2 r = vec2({HALO_R:.4f}, {HALO_R * 4 / 3:.4f});
  float h = 0.0;
  for (int i = 0; i < 8; i++) {{
    float a = float(i) * 0.7854;
    h += bright(uv + r * vec2(cos(a), sin(a))) + bright(uv + 0.5 * r * vec2(cos(a + 0.39), sin(a + 0.39)));
  }}
  c += vec3{HALO_TINT} * (h / 16.0) * {s['halation']:.4f};
  vec2 q = vec2(uv.x, (uv.y - 0.5) / {KEEP:.4f} + 0.5) - 0.5;
  c *= 1.0 - {s['vignette']:.4f} * pow(length(q * 2.0) / 1.4142, 1.8);
  vec2 cell = floor(gl_FragCoord.xy / 1.6) + floor(time * 24.0) * vec2(17.0, 41.0);
  float n = (hash(cell) + hash(cell + 7.1) - 1.0) * 1.7;
  c += n * {s['grain']:.4f} * (0.4 + 2.4 * l * (1.0 - l));
  {"if (abs(uv.y - 0.5) > " + f"{KEEP / 2:.4f}" + ") c *= 0.3;" if frame else ""}
  gl_FragColor = vec4(clamp(c, 0.0, 1.0), s.a);
}}
"""


def _box(a, k, axis):
    """Box blur of width k along axis (edge-padded), via cumulative sums."""
    if k <= 1:
        return a
    p = k // 2
    pad = [(0, 0)] * a.ndim
    pad[axis] = (p + 1, k - p - 1)
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float32)
    n = a.shape[axis]
    return (np.take(c, range(k, k + n), axis=axis) - np.take(c, range(0, n), axis=axis)) / k


def _upsample(m, h, w, y0, y1):
    """Bilinear rows y0..y1 of low-res map m stretched to h x w."""
    mh, mw = m.shape
    ys = np.clip((np.arange(y0, y1) + 0.5) * mh / h - 0.5, 0, mh - 1)
    xs = np.clip((np.arange(w) + 0.5) * mw / w - 0.5, 0, mw - 1)
    y_0 = ys.astype(int); y_1 = np.minimum(y_0 + 1, mh - 1); fy = (ys - y_0)[:, None]
    x_0 = xs.astype(int); x_1 = np.minimum(x_0 + 1, mw - 1); fx = (xs - x_0)[None, :]
    top = m[y_0][:, x_0] * (1 - fx) + m[y_0][:, x_1] * fx
    bot = m[y_1][:, x_0] * (1 - fx) + m[y_1][:, x_1] * fx
    return (top * (1 - fy) + bot * fy).astype(np.float32)


def develop(rgb, stock, seed=None):
    """rgb: HxWx3 uint8 in sensor orientation (landscape) -> the stock's look, cropped to 3:2."""
    s = STOCKS.get(stock)
    if not s:
        return rgb
    H, W = rgb.shape[:2]
    keep = int(round(min(H, W * 2 / 3) / 2)) * 2
    y_top = (H - keep) // 2
    src = rgb[y_top:y_top + keep]
    h, w = src.shape[:2]
    luma = np.array(LUMA, np.float32)

    # halation map from a 1/8 copy of the WHOLE sensor frame (glows reach across the crop line)
    f = 8
    small = rgb[:H // f * f, :W // f * f].reshape(H // f, f, W // f, f, 3).mean((1, 3)) / 255.0
    b = np.clip((small @ luma - 0.7) / 0.3, 0, 1).astype(np.float32)
    rad = max(1, int(HALO_R * max(W, H) / f))
    for _ in range(3):
        b = _box(_box(b, rad + 1, 0), rad + 1, 1)
    halo = b[int(y_top / f):int((y_top + keep) / f) + 1]

    rng = np.random.default_rng(seed)
    gk = int(np.clip(round(1.6 * max(W, H) / 1600), 1, 5))     # grain size matches the preview
    # clumped grain tile (same strength after the blur), placed at a random offset per strip
    tile = rng.standard_normal((512 + 256, w + 512), dtype=np.float32)
    tile = _box(_box(tile, gk, 0), gk, 1) * gk
    # exposure + shoulder + S-curve only depend on the channel value: a 256-entry table
    k = s["shoulder"]
    x = np.clip(np.arange(256, dtype=np.float32) / 255 * s["ex"], 0, 4)
    x = np.clip((1 - np.exp(-k * x)) / (1 - np.exp(-k)), 0, 1)
    tone = (x + (x * x * (3 - 2 * x) - x) * s["contrast"]).astype(np.float32)
    lift = np.array(s["lift"], np.float32)
    span = np.array(s["gain"], np.float32) - lift
    mid = np.array(s["mid"], np.float32)
    tint = np.array(HALO_TINT, np.float32)
    xs = (np.arange(w, dtype=np.float32) + 0.5) / w - 0.5
    out = np.empty_like(src)
    starts = list(range(0, h, 256))           # strips: 12 MP of floats would need ~600 MB
    offsets = [(int(rng.integers(0, 256)), int(rng.integers(0, 512))) for _ in starts]

    def strip(i):
        y0 = starts[i]
        y1 = min(h, y0 + 256)
        c = tone[src[y0:y1]]
        l = (c @ luma)[..., None]
        r, g, bl = c[..., 0:1], c[..., 1:2], c[..., 2:3]
        warm = np.clip((r - bl) * 3, 0, 1) * np.clip((r - g) * 4 + 0.3, 0, 1)
        green = np.clip((g - np.maximum(r, bl)) * 5, 0, 1)
        c = l + (c - l) * (s["sat"] + s["warm"] * warm - s["green"] * green)
        c = c + mid * 4 * l * (1 - l)
        c = lift + c * span
        hy = _upsample(halo, keep, w, y0, y1) if halo.size else 0
        c = c + tint * (hy[..., None] * s["halation"])
        ys = (np.arange(y0, y1, dtype=np.float32) + 0.5) / h - 0.5
        d = np.sqrt(xs[None, :] ** 2 + ys[:, None] ** 2) * 2 / 1.4142
        c = c * (1 - s["vignette"] * d[..., None] ** 1.8)
        oy, ox = offsets[i]
        n = tile[oy:oy + y1 - y0, ox:ox + w]
        lum = c @ luma
        c = c + (n * s["grain"] * (0.4 + 2.4 * lum * (1 - lum)))[..., None]
        out[y0:y1] = np.clip(c * 255 + 0.5, 0, 255).astype(np.uint8)

    # numpy drops the GIL in its loops: 4 strips at a time use 4 of the 8 cores
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        list(pool.map(strip, range(len(starts))))
    return out


# ---------- watermark: an old date-back style dot-matrix stamp ----------
GLYPHS = {
    "n": ["00000", "00000", "10110", "11001", "10001", "10001", "10001"],
    "o": ["00000", "00000", "01110", "10001", "10001", "10001", "01110"],
    "t": ["01000", "01000", "11100", "01000", "01000", "01001", "00110"],
    "r": ["00000", "00000", "10110", "11001", "10000", "10000", "10000"],
    "e": ["00000", "00000", "01110", "10001", "11111", "10000", "01110"],
    "a": ["00000", "00000", "01110", "00001", "01111", "10001", "01111"],
    "l": ["01100", "00100", "00100", "00100", "00100", "00100", "01110"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "c": ["00000", "00000", "01110", "10000", "10000", "10001", "01110"],
    "m": ["00000", "00000", "11010", "10101", "10101", "10001", "10001"],
}
STAMP_CORE = (1.0, 0.84, 0.42)     # slightly yellow
STAMP_GLOW = (1.0, 0.55, 0.16)     # orange bleed, like the LEDs exposing the film


def watermark(rgb, text=WATERMARK):
    """rgb: upright HxWx3 uint8 -> stamped bottom-right (returns a new array)."""
    H, W = rgb.shape[:2]
    pitch = max(2.0, min(H, W) * 0.026 / 7)          # 7 dot rows = 2.6% of the short side
    cols = len(text) * 6 - 1
    pad = int(pitch * 3)
    cw, ch = int(cols * pitch) + 2 * pad, int(7 * pitch) + 2 * pad
    yy, xx = np.mgrid[0:ch, 0:cw].astype(np.float32)
    dots = np.zeros((ch, cw), np.float32)
    rad = pitch * 0.40
    for i, chr_ in enumerate(text):
        for row, bits in enumerate(GLYPHS.get(chr_, ["00000"] * 7)):
            for col, bit in enumerate(bits):
                if bit == "1":
                    cx = pad + (i * 6 + col + 0.5) * pitch
                    cy = pad + (row + 0.5) * pitch
                    y0, y1 = int(cy - pitch), int(cy + pitch) + 1
                    x0, x1 = int(cx - pitch), int(cx + pitch) + 1
                    dd = np.hypot(xx[y0:y1, x0:x1] - cx, yy[y0:y1, x0:x1] - cy)
                    dots[y0:y1, x0:x1] = np.maximum(dots[y0:y1, x0:x1],
                                                    np.clip((rad - dd) / (pitch * 0.18) + 0.5, 0, 1))
    glow = dots
    gr = max(1, int(pitch * 0.8))
    for _ in range(3):
        glow = _box(_box(glow, gr, 0), gr, 1)
    light = (dots[..., None] * np.array(STAMP_CORE, np.float32) * 0.92
             + glow[..., None] * np.array(STAMP_GLOW, np.float32) * 0.75)
    light = np.clip(light, 0, 1)
    m = int(min(H, W) * 0.045)
    y0, x0 = H - m - ch, W - m - cw
    out = rgb.copy()
    region = out[y0:y0 + ch, x0:x0 + cw].astype(np.float32) / 255.0
    region = 1 - (1 - region) * (1 - light)                # screen blend: light on film
    out[y0:y0 + ch, x0:x0 + cw] = np.clip(region * 255 + 0.5, 0, 255).astype(np.uint8)
    return out
