#!/usr/bin/env python3
"""Build dict/<lang>.tsv (word<TAB>count) from the OpenSubtitles 50k lists in dl-freq/."""
import os, re, sys, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
LANGS = {"en": "en_50k.txt", "es": "es_50k.txt", "fr": "fr_50k.txt", "sv": "sv_50k.txt"}
OK = re.compile(r"^[a-zà-öø-ÿœ']+$")
EN_CONTRACTIONS = """don't can't won't didn't doesn't isn't wasn't aren't weren't haven't hasn't hadn't
couldn't wouldn't shouldn't it's that's what's there's here's he's she's who's let's I'm I'll I've I'd
you're you'll you've you'd we're we'll we've we'd they're they'll they've they'd ain't y'all""".split()
EN_JUNK = set("dont im cant didnt doesnt isnt wasnt arent werent havent hasnt hadnt couldnt wouldnt "
               "shouldnt thats whats theres youre theyre ive youve weve youll theyll".split())
EN_CAPS = {"i": "I", "i'm": "I'm", "i'll": "I'll", "i've": "I've", "i'd": "I'd"}


def base(w):
    return "".join(c for c in unicodedata.normalize("NFD", w) if c.isascii() and c.isalpha())


def main():
    os.makedirs(os.path.join(HERE, "dict"), exist_ok=True)
    for lang, fn in LANGS.items():
        seen, out = set(), []
        with open(os.path.join(HERE, "dl-freq", fn), encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) != 2 or not parts[1].isdigit():
                    continue
                w, c = parts[0], int(parts[1])
                if not OK.match(w) or w.startswith("'") or w.endswith("'") or c < 3:
                    continue
                b = base(w)
                if not b or (len(b) == 1 and w not in ("a", "i", "y", "o", "e", "à", "å", "ö")):
                    continue
                if lang == "en":
                    if w in EN_JUNK:
                        continue
                    w = EN_CAPS.get(w, w)
                if w in seen:
                    continue
                seen.add(w)
                out.append((w, c))
        if lang == "en":
            # the subtitle tokenizer split contractions; add them at a solid frequency
            top = out[0][1]
            for i, w in enumerate(EN_CONTRACTIONS):
                if w not in seen:
                    seen.add(w)
                    out.append((w, int(top * (0.06 if i < 12 else 0.02))))
        with open(os.path.join(HERE, "dict", lang + ".tsv"), "w", encoding="utf-8") as f:
            for w, c in out:
                f.write(f"{w}\t{c}\n")
        print(lang, len(out), file=sys.stderr)


if __name__ == "__main__":
    main()
