"""Photo looks for each notareal theme. One definition drives both the live preview (GLSL for
GStreamer's glshader) and the saved photo (numpy), so what you see is what gets saved.

Per theme: sat (1 = unchanged), contrast around mid-grey, then black/white points remapped to
lift / gain colours (split toning), optional monochrome or a two-colour duotone, scanlines."""
import numpy as np

LOOKS = {
    # pink/violet split tone, punchy
    "vapor": dict(sat=1.25, contrast=1.06, lift=(0.10, 0.04, 0.18), gain=(1.00, 0.93, 1.00)),
    # pastel: soft contrast, lavender blacks
    "vaporlight": dict(sat=0.85, contrast=0.85, lift=(0.22, 0.17, 0.30), gain=(1.00, 0.98, 1.00)),
    # ink on paper
    "micro": dict(mono=True, contrast=1.25, lift=(0.07, 0.07, 0.06), gain=(0.93, 0.92, 0.88)),
    "microdark": dict(mono=True, contrast=1.35, lift=(0.00, 0.00, 0.00), gain=(0.94, 0.93, 0.90)),
    # CRT phosphor duotone with scanlines
    "alien": dict(mono=True, contrast=1.2, lift=(0.03, 0.05, 0.04), gain=(0.92, 0.95, 0.84), scan=True),
    # faded vintage print: teal shadows, warm cream highlights
    "nasa": dict(sat=0.80, contrast=0.90, lift=(0.10, 0.13, 0.15), gain=(1.00, 0.93, 0.80)),
}
PASS = """#ifdef GL_ES
precision mediump float;
#endif
varying vec2 v_texcoord;
uniform sampler2D tex;
void main () { gl_FragColor = texture2D(tex, v_texcoord); }
"""


def _v(t):
    return "vec3(%.4f, %.4f, %.4f)" % tuple(t)


def glsl(theme):
    look = LOOKS.get(theme)
    if not look:
        return PASS
    lines = ["#ifdef GL_ES", "precision mediump float;", "#endif",
             "varying vec2 v_texcoord;", "uniform sampler2D tex;",
             "void main () {",
             "  vec4 s = texture2D(tex, v_texcoord);",
             "  vec3 c = s.rgb;",
             "  float l = dot(c, vec3(0.299, 0.587, 0.114));"]
    if look.get("mono"):
        lines.append("  c = vec3(l);")
    else:
        lines.append("  c = mix(vec3(l), c, %.4f);" % look.get("sat", 1.0))
    lines.append("  c = clamp((c - 0.5) * %.4f + 0.5, 0.0, 1.0);" % look.get("contrast", 1.0))
    lines.append(f"  c = {_v(look['lift'])} + c * ({_v(look['gain'])} - {_v(look['lift'])});")
    if look.get("scan"):
        # every 3rd row (in sensor space: the preview is turned, so these run along the frame)
        lines.append("  c *= 0.86 + 0.14 * step(1.0, mod(gl_FragCoord.x, 3.0));")
    lines += ["  gl_FragColor = vec4(c, s.a);", "}"]
    return "\n".join(lines) + "\n"


def apply(rgb, theme):
    """rgb: HxWx3 uint8 (sensor orientation) -> same, with the theme look."""
    look = LOOKS.get(theme)
    if not look:
        return rgb
    out = np.empty_like(rgb)
    lift = np.array(look["lift"], np.float32)
    span = np.array(look["gain"], np.float32) - lift
    k = look.get("contrast", 1.0)
    sat = look.get("sat", 1.0)
    for y0 in range(0, rgb.shape[0], 256):              # in strips: 12 MP floats would need ~600 MB
        c = rgb[y0:y0 + 256].astype(np.float32) / 255.0
        l = c @ np.array([0.299, 0.587, 0.114], np.float32)
        if look.get("mono"):
            c = np.repeat(l[..., None], 3, axis=2)
        else:
            c = l[..., None] + (c - l[..., None]) * sat
        c = np.clip((c - 0.5) * k + 0.5, 0, 1)
        c = lift + c * span
        if look.get("scan"):
            cols = np.arange(c.shape[1])
            c = c * np.where(cols % 3 == 0, 0.86, 1.0)[None, :, None]
        out[y0:y0 + 256] = np.clip(c * 255 + 0.5, 0, 255).astype(np.uint8)
    return out
