"""Swipe (gesture) decoder: SHARK2-style template matching over a frequency dictionary.

Coordinates are in key widths: a letter key is 1.0 wide and ROW (= key height / key width)
tall, so the same decoder works for any keyboard size or position.
"""
import math, os, unicodedata
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
# words the user added from the suggestion bar (＋word), one per line, per language
USER_DIR = os.path.expanduser("~/.local/share/swipekbd")
USER_RANK = 0.03   # user words count like the top 3 % most frequent words
N = 40  # resample points

# key centres in key units (x) and rows (y); matches swipekbd.py's letter layer
ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
ROW_X0 = [0.5, 1.0, 2.0]
KEYS = {}
for r, letters in enumerate(ROWS):
    for i, ch in enumerate(letters):
        KEYS[ch] = (ROW_X0[r] + i, r + 0.5)


def base(w):
    return "".join(c for c in unicodedata.normalize("NFD", w.lower()) if c in KEYS)


def resample(pts, n=N):
    pts = np.asarray(pts, dtype=np.float32)
    if len(pts) == 1:
        return np.repeat(pts, n, axis=0)
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    if cum[-1] < 1e-6:
        return np.repeat(pts[:1], n, axis=0)
    t = np.linspace(0, cum[-1], n)
    return np.stack([np.interp(t, cum, pts[:, 0]), np.interp(t, cum, pts[:, 1])], axis=1)


def path_len(pts):
    return float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum()) if len(pts) > 1 else 0.0


class Decoder:
    def __init__(self, lang="en"):
        self.lang = None
        self.load(lang)

    def load(self, lang):
        if lang == self.lang:
            return
        self.lang = lang
        self.words, self.paths, logf = [], [], []
        with open(os.path.join(HERE, "dict", lang + ".tsv"), encoding="utf-8") as f:
            for line in f:
                w, c = line.rstrip("\n").split("\t")
                p = self._path(w)
                if len(p) < 2:
                    continue
                self.words.append(w)
                self.paths.append(p)
                logf.append(math.log(int(c)))
        self.known = {w.lower() for w in self.words}
        self.user_logf = sorted(logf, reverse=True)[int(len(logf) * USER_RANK)]
        for w in self.user_words():
            if w.lower() not in self.known and len(self._path(w)) >= 2:
                self.words.append(w)
                self.paths.append(self._path(w))
                logf.append(self.user_logf)
                self.known.add(w.lower())
        self.logf = np.array(logf, dtype=np.float32)
        self._index()

    @staticmethod
    def _path(w):
        b = base(w)
        # collapse doubled letters: "hello" is traced h-e-l-o
        return "".join(ch for i, ch in enumerate(b) if i == 0 or ch != b[i - 1])

    def user_file(self):
        return os.path.join(USER_DIR, f"words-{self.lang}.txt")

    def user_words(self):
        try:
            with open(self.user_file(), encoding="utf-8") as f:
                return [l.strip() for l in f if l.strip()]
        except OSError:
            return []

    def is_known(self, w):
        return w.lower() in self.known

    def add_word(self, w):
        """Save w to the user dictionary and make it swipeable/completable right away."""
        w = w.strip()
        if not w or self.is_known(w):
            return False
        os.makedirs(USER_DIR, exist_ok=True)
        with open(self.user_file(), "a", encoding="utf-8") as f:
            f.write(w + "\n")
        self.known.add(w.lower())
        if len(self._path(w)) >= 2:   # one-letter words can't be swiped
            self.words.append(w)
            self.paths.append(self._path(w))
            self.logf = np.append(self.logf, np.float32(self.user_logf))
            self._index()
        return True

    def _index(self):
        self.cost_f = self.logf.max() - self.logf  # 0 for the most common word
        self.buckets = {}
        for i, p in enumerate(self.paths):
            self.buckets.setdefault((p[0], p[-1]), []).append(i)
        self.buckets = {k: np.array(v) for k, v in self.buckets.items()}
        self.sorted = sorted(range(len(self.words)), key=lambda i: self.words[i].lower())
        self.sorted_keys = [self.words[i].lower() for i in self.sorted]
        self._tcache = {}

    def _templates(self, key, row):
        """(M, N, 2) resampled ideal paths + (M,) lengths for one (first, last) bucket."""
        ck = (key, round(row, 2))
        hit = self._tcache.get(ck)
        if hit is None:
            idx = self.buckets[key]
            T = np.empty((len(idx), N, 2), np.float32)
            L = np.empty(len(idx), np.float32)
            for j, i in enumerate(idx):
                pts = [(KEYS[c][0], KEYS[c][1] * row) for c in self.paths[i]]
                T[j] = resample(pts)
                L[j] = path_len(np.asarray(pts))
            hit = self._tcache[ck] = (idx, T, L)
        return hit

    def near_keys(self, p, row, radius=1.25, min_n=2):
        d = sorted((math.hypot(x - p[0], y * row - p[1]), c) for c, (x, y) in KEYS.items())
        return [c for k, (dd, c) in enumerate(d) if dd < radius or k < min_n]

    def decode(self, pts, row=1.4, k=5):
        """pts: gesture in key units (x, y*row already applied). Returns [(word, score)]."""
        pts = np.asarray(pts, np.float32)
        G = resample(pts)
        glen = path_len(pts)
        starts = self.near_keys(G[0], row)
        ends = self.near_keys(G[-1], row)
        idx_l, T_l, L_l = [], [], []
        for s in starts:
            for e in ends:
                if (s, e) in self.buckets:
                    idx, T, L = self._templates((s, e), row)
                    idx_l.append(idx); T_l.append(T); L_l.append(L)
        if not idx_l:
            return []
        idx = np.concatenate(idx_l); T = np.concatenate(T_l); L = np.concatenate(L_l)
        # location channel: mean point distance
        loc = np.linalg.norm(T - G[None], axis=2).mean(axis=1)
        # shape channel: compare after removing position and scale
        def norm(A):
            A = A - A.mean(axis=-2, keepdims=True)
            s = np.abs(A).max(axis=(-2, -1), keepdims=True)
            return A / np.maximum(s, 0.5)
        shape = np.linalg.norm(norm(T) - norm(G)[None], axis=2).mean(axis=1)
        # gesture length vs template length (traced paths overshoot a bit)
        ratio = np.log((glen + 0.5) / (L + 0.5))
        lenpen = np.maximum(np.abs(ratio) - 0.25, 0)
        pre = loc + 0.6 * shape + 1.2 * lenpen + 0.07 * self.cost_f[idx]
        top = np.argsort(pre)[:60]
        res = []
        for j in top:
            i = idx[j]
            al = self._align(pts, self.paths[i], row)
            score = loc[j] + 0.6 * shape[j] + 1.2 * lenpen[j] + 1.6 * al + 0.07 * self.cost_f[i]
            res.append((float(score), self.words[i]))
        res.sort()
        out, seen = [], set()
        for s, w in res:
            if w.lower() not in seen:
                seen.add(w.lower())
                out.append((w, s))
            if len(out) == k:
                break
        return out

    @staticmethod
    def _align(pts, path, row):
        """Best in-order matching of each letter to a gesture point (DP); mean squared
        distance beyond 0.5 key of the letters the gesture didn't reach."""
        K = np.array([(KEYS[c][0], KEYS[c][1] * row) for c in path], np.float32)
        D = np.linalg.norm(pts[None, :, :] - K[:, None, :], axis=2)  # (letters, points)
        C = np.maximum(D - 0.5, 0) ** 2
        acc = C[0]
        for k in range(1, len(K)):
            acc = C[k] + np.minimum.accumulate(acc)
        return float(acc.min()) / len(K)

    def complete(self, prefix, k=3):
        """Most frequent words starting with prefix (case-insensitive) for tap typing."""
        import bisect
        p = prefix.lower()
        lo = bisect.bisect_left(self.sorted_keys, p)
        hi = bisect.bisect_left(self.sorted_keys, p + "￿")
        cand = self.sorted[lo:hi]
        cand = sorted(cand, key=lambda i: -self.logf[i])[:k]
        return [self.words[i] for i in cand]
