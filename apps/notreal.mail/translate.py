"""Machine translation through Google's free Chrome-extension endpoint (no key)."""
import json
import urllib.parse
import urllib.request

URL = "https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=auto&tl=%s"
CHUNK = 3500
NAMES = {"sv": "Swedish", "es": "Spanish", "en": "English", "fr": "French", "de": "German", "it": "Italian",
         "pt": "Portuguese", "nl": "Dutch", "da": "Danish", "no": "Norwegian", "fi": "Finnish", "pl": "Polish",
         "ru": "Russian", "ja": "Japanese", "zh-CN": "Chinese", "ko": "Korean", "ar": "Arabic", "tr": "Turkish"}


def _chunks(text):
    out, cur = [], ""
    for para in text.split("\n"):
        while len(para) > CHUNK:  # one giant line: hard split
            out.append(para[:CHUNK])
            para = para[CHUNK:]
        if cur and len(cur) + len(para) + 1 > CHUNK:
            out.append(cur)
            cur = ""
        cur = cur + "\n" + para if cur else para
    return out + [cur]


def translate(texts, target="en"):
    """[str] -> ([str], source language name). Blocking; run it in a thread."""
    pieces, spans = [], []
    for t in texts:
        c = _chunks(t or "")
        spans.append(len(c))
        pieces += c
    data = urllib.parse.urlencode([("q", p) for p in pieces]).encode()
    req = urllib.request.Request(URL % target, data=data, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.load(r)
    res = [x if isinstance(x, list) else [x, ""] for x in res]
    out, i = [], 0
    for n in spans:
        out.append("\n".join(x[0] for x in res[i:i + n]))
        i += n
    src = next((x[1] for x in res if x[1]), "")
    return out, NAMES.get(src, src)
