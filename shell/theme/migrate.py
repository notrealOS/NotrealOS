#!/usr/bin/env python3
"""One-off: rewrite an app style.css to use theme.css variables. Backup: style.css.before-theme."""
import re, shutil, sys
HEX = {"#1c2340": "bg", "#161c36": "bg-deep", "#232a4d": "surface", "#e9e6fb": "fg", "#eef0ff": "fg-bright",
       "#c5a0ff": "accent", "#d9c2ff": "accent-light", "#b38cf5": "violet", "#8fe0f2": "primary",
       "#6fd6f2": "cyan", "#f7a1e8": "pink", "#ff7eb6": "hot-pink", "#7ef0d2": "mint", "#fffb96": "yellow",
       "#ffb86b": "orange", "#5aa9ff": "blue"}
RGB = {"28,35,64": "bg", "35,42,77": "surface", "233,230,251": "fg", "238,240,255": "fg-bright",
       "197,160,255": "accent", "179,140,245": "violet", "143,224,242": "primary", "247,161,232": "pink",
       "255,126,182": "hot-pink", "126,240,210": "mint", "255,251,150": "yellow", "255,184,107": "orange",
       "90,169,255": "blue", "120,90,170": "glow"}
FONTS = [(r'"CozetteVector",\s*"JetBrains Mono",\s*monospace', "var(--nr-font)"),
         (r'"CozetteVector",\s*monospace', "var(--nr-font)"),
         (r'"JetBrains Mono",\s*monospace', "var(--nr-font-code)"),
         (r'"Noto Sans CJK KR",\s*"Noto Sans",\s*sans-serif', "var(--nr-font-text)")]
GRAD = re.compile(r"linear-gradient\(90deg,\s*#f7a1e8,\s*#b38cf5 50%,\s*#6fd6f2\)", re.I)

def convert(css, keep_root):
    # the app's libadwaita :root block now comes from theme.css (keep_root: keep only differing lines)
    m = re.search(r":root\s*\{(.*?)\}\s*", css, re.S)
    if m:
        body = m[1]
        kept = [d.strip() for d in body.split(";") if d.strip() and any(k in d for k in keep_root)]
        css = css[:m.start()] + (":root {\n  " + ";\n  ".join(kept) + ";\n}\n" if kept else "") + css[m.end():]
    css = GRAD.sub("var(--nr-gradient)", css)
    for pat, rep in FONTS:
        css = re.sub(pat, rep, css)
    css = re.sub(r"rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\s*\)",
                 lambda m: f"alpha(var(--nr-{RGB[f'{m[1]},{m[2]},{m[3]}']}), {m[4]})"
                 if f"{m[1]},{m[2]},{m[3]}" in RGB else m[0], css)
    css = re.sub(r"#[0-9a-fA-F]{6}\b", lambda m: f"var(--nr-{HEX[m[0].lower()]})" if m[0].lower() in HEX else m[0], css)
    return css

if __name__ == "__main__":
    path, keep = sys.argv[1], sys.argv[2:]
    shutil.copy(path, path + ".before-theme")
    css = open(path + ".before-theme").read()
    open(path, "w").write(convert(css, keep))
