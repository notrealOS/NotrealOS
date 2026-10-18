"""Where the gallery's photos/videos live, their kinds, settings, and cached thumbnails."""
import hashlib
import json
import os
import subprocess

HOME = os.path.expanduser("~")
PICTURES = os.path.join(HOME, "Pictures")
VIDEOS = os.path.join(HOME, "Videos")
THUMBS = os.path.join(HOME, ".cache", "gallery", "thumbs")
SETTINGS = os.path.join(HOME, ".config", "gallery", "settings.json")
THUMB_PX = 360                      # cells are ~176 logical px at scale 2

IMAGE_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
             ".gif": "image/gif", ".heic": "image/heic", ".avif": "image/avif"}
VIDEO_EXT = {".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska", ".mov": "video/quicktime"}

# (folder, album, recursive?) - album decides the tab; Snapshot writes to the Camera folders,
# wf-recorder screen recordings land in ~/Videos itself
SOURCES = [
    (os.path.join(PICTURES, "Camera"), "camera", True),
    (os.path.join(VIDEOS, "Camera"), "camera", True),
    (os.path.join(PICTURES, "Screenshots"), "screens", True),
    (VIDEOS, "screens", False),
    (os.path.join(PICTURES, "Google"), "google", True),
    (PICTURES, "other", False),
]

DEFAULTS = {"backup": True, "screenshots": False, "wifi_only": True}


def settings():
    s = dict(DEFAULTS)
    try:
        s.update(json.load(open(SETTINGS)))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS + ".tmp", "w") as f:
        json.dump(s, f)
    os.replace(SETTINGS + ".tmp", SETTINGS)


def mime(path):
    ext = os.path.splitext(path)[1].lower()
    return IMAGE_EXT.get(ext) or VIDEO_EXT.get(ext)


def is_video(path):
    return os.path.splitext(path)[1].lower() in VIDEO_EXT


def scan():
    """[(path, album, mtime)] newest first."""
    seen, out = set(), []
    for folder, album, recursive in SOURCES:
        if not os.path.isdir(folder):
            continue
        walker = os.walk(folder) if recursive else [(folder, [], os.listdir(folder))]
        for root, _dirs, files in walker:
            for name in files:
                p = os.path.join(root, name)
                if p in seen or name.startswith(".") or name.endswith(".part") or not mime(p):
                    continue
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_size == 0:
                    continue
                seen.add(p)
                out.append((p, album, st.st_mtime))
    out.sort(key=lambda t: -t[2])
    return out


def backup_candidates(s=None):
    s = s or settings()
    albums = {"camera"} | ({"screens"} if s.get("screenshots") else set())
    return [(p, a, m) for p, a, m in scan() if a in albums]


def thumb_path(path):
    st = os.stat(path)
    h = hashlib.sha1(f"{path}:{st.st_size}:{int(st.st_mtime)}".encode()).hexdigest()
    return os.path.join(THUMBS, h + ".jpg")


def make_thumb(path):
    """Cached square-ish JPEG thumbnail; returns its path or None. Safe to call from threads."""
    try:
        out = thumb_path(path)
    except OSError:
        return None
    if os.path.exists(out):
        return out
    os.makedirs(THUMBS, exist_ok=True)
    tmp = out + f".{os.getpid()}.tmp"
    try:
        if is_video(path):
            r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", "0.5", "-i", path, "-frames:v", "1",
                                "-vf", f"scale={THUMB_PX}:{THUMB_PX}:force_original_aspect_ratio=increase",
                                "-f", "image2", "-c:v", "mjpeg", tmp], capture_output=True, timeout=30)
            if r.returncode != 0 or not os.path.exists(tmp):
                r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", path, "-frames:v", "1",
                                    "-vf", f"scale={THUMB_PX}:{THUMB_PX}:force_original_aspect_ratio=increase",
                                    "-f", "image2", "-c:v", "mjpeg", tmp], capture_output=True, timeout=30)
                if r.returncode != 0:
                    return None
        else:
            import gi
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, THUMB_PX * 2, THUMB_PX * 2, True)
            pb = pb.apply_embedded_orientation() or pb
            w, h = pb.get_width(), pb.get_height()
            k = THUMB_PX / min(w, h)
            if k < 1:
                pb = pb.scale_simple(max(1, round(w * k)), max(1, round(h * k)), GdkPixbuf.InterpType.BILINEAR)
            if pb.get_has_alpha():
                pb = pb.composite_color_simple(pb.get_width(), pb.get_height(),
                                               GdkPixbuf.InterpType.NEAREST, 255, 32, 0xffffff, 0xffffff)
            pb.savev(tmp, "jpeg", ["quality"], ["82"])
        os.replace(tmp, out)
        return out
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
