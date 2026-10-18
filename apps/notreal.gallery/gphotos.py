"""Google Photos for the gallery: OAuth (loopback + PKCE), backup uploads, Picker imports.

Since 2025 Google only lets third-party apps
  - UPLOAD (photoslibrary.appendonly) and read back what they uploaded (readonly.appcreateddata), and
  - let the user PICK items from the whole library in Google's own Picker page (photospicker).
So backup = upload into our own "notarealPhone" album; older photos come in through the Picker.

The OAuth client is the user's own "Desktop app" client from the Google Cloud console, saved as
~/.config/gallery/client.json ({"client_id": ..., "client_secret": ...}, or the downloaded JSON
as is). The refresh token lives in ~/.config/gallery/token.json (0600).
"""
import base64
import datetime
import hashlib
import http.server
import json
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CONF = os.path.expanduser("~/.config/gallery")
CLIENT = os.path.join(CONF, "client.json")
TOKEN = os.path.join(CONF, "token.json")
DATA = os.path.expanduser("~/.local/share/gallery")
STATE = os.path.join(DATA, "backup.json")
IMPORT_DIR = os.path.expanduser("~/Pictures/Google")
ALBUM_TITLE = "notarealPhone"

SCOPES = ("https://www.googleapis.com/auth/photoslibrary.appendonly "
          "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata "
          "https://www.googleapis.com/auth/photospicker.mediaitems.readonly")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
LIB = "https://photoslibrary.googleapis.com/v1"
PICKER = "https://photospicker.googleapis.com/v1"


class AuthError(Exception):
    pass


def _adopt_download():
    """Move a Desktop client_secret_*.json saved from the browser into place."""
    import glob
    for f in sorted(glob.glob(os.path.expanduser("~/Downloads/client_secret_*.json")),
                    key=os.path.getmtime, reverse=True):
        try:
            if "installed" not in json.load(open(f)):
                continue
        except (OSError, ValueError):
            continue
        os.makedirs(CONF, exist_ok=True)
        if os.path.exists(CLIENT):
            os.replace(CLIENT, CLIENT + ".old")
        os.replace(f, CLIENT)
        os.chmod(CLIENT, 0o600)
        return


def _client():
    try:
        d = json.load(open(CLIENT))
    except (OSError, ValueError):
        d = {}
    if "installed" not in d:
        _adopt_download()
        try:
            d = json.load(open(CLIENT))
        except (OSError, ValueError):
            return None
    # only a Desktop ("installed") client may use the random 127.0.0.1 port; a Web client fails
    d = d.get("installed") or {}
    return d if d.get("client_id") and d.get("client_secret") else None


def has_client():
    return _client() is not None


def signed_in():
    try:
        return bool(json.load(open(TOKEN)).get("refresh_token"))
    except (OSError, ValueError):
        return False


def sign_out():
    try:
        os.remove(TOKEN)
    except OSError:
        pass


def _save_token(tok):
    os.makedirs(CONF, exist_ok=True)
    fd = os.open(TOKEN + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)
    os.replace(TOKEN + ".tmp", TOKEN)


def _post_form(url, fields):
    req = urllib.request.Request(url, urllib.parse.urlencode(fields).encode(),
                                 {"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def sign_in(open_url, timeout=300):
    """Browser sign-in. open_url(url) shows Google's consent page; blocks until done (run in a thread)."""
    c = _client()
    if not c:
        raise AuthError("no OAuth client: put client.json in ~/.config/gallery")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in got
            self.wfile.write(("<html><body style='font-family:monospace;padding:2em'><h2>"
                              + ("Signed in. You can close this tab." if ok else "Sign-in failed.")
                              + "</h2></body></html>").encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    srv.timeout = 1
    redirect = f"http://127.0.0.1:{srv.server_port}"
    open_url(AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"}))
    end = time.time() + timeout
    while "code" not in got and "error" not in got and time.time() < end:
        srv.handle_request()
    srv.server_close()
    if "code" not in got:
        raise AuthError(got.get("error", "sign-in timed out"))
    tok = _post_form(TOKEN_URL, {"code": got["code"], "client_id": c["client_id"],
                                 "client_secret": c["client_secret"], "redirect_uri": redirect,
                                 "grant_type": "authorization_code", "code_verifier": verifier})
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    _save_token(tok)


_lock = threading.Lock()


def access_token():
    with _lock:
        try:
            tok = json.load(open(TOKEN))
        except (OSError, ValueError):
            raise AuthError("not signed in")
        if tok.get("access_token") and tok.get("expires_at", 0) > time.time():
            return tok["access_token"]
        c = _client()
        if not c or not tok.get("refresh_token"):
            raise AuthError("not signed in")
        try:
            new = _post_form(TOKEN_URL, {"refresh_token": tok["refresh_token"], "client_id": c["client_id"],
                                         "client_secret": c["client_secret"], "grant_type": "refresh_token"})
        except urllib.error.HTTPError as e:
            if e.code in (400, 401):
                raise AuthError("Google sign-in expired, sign in again")
            raise
        tok.update(access_token=new["access_token"], expires_at=time.time() + new.get("expires_in", 3600) - 60)
        _save_token(tok)
        return tok["access_token"]


def _api(method, url, body=None, raw=None, headers=None, timeout=60):
    h = {"Authorization": "Bearer " + access_token()}
    if body is not None:
        raw = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, raw, h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data


def _json(method, url, body=None):
    data = _api(method, url, body=body)
    return json.loads(data) if data else {}


# --- backup ---------------------------------------------------------------

def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {"album": None, "items": {}}


def save_state(st):
    os.makedirs(DATA, exist_ok=True)
    with open(STATE + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(STATE + ".tmp", STATE)


def file_key(path):
    s = os.stat(path)
    return f"{s.st_size}:{int(s.st_mtime)}"


def is_backed_up(st, path):
    it = st["items"].get(path)
    try:
        return bool(it) and it.get("key") == file_key(path)
    except OSError:
        return False


def _album(st):
    if st.get("album"):
        return st["album"]
    a = _json("POST", LIB + "/albums", {"album": {"title": ALBUM_TITLE}})
    st["album"] = a["id"]
    save_state(st)
    return a["id"]


def upload(path, mime, st):
    """Upload one file into the notarealPhone album and record it in st (caller saves)."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        tok = _api("POST", LIB + "/uploads", raw=f, timeout=600, headers={
            "Content-Type": "application/octet-stream", "Content-Length": str(size),
            "X-Goog-Upload-Content-Type": mime, "X-Goog-Upload-Protocol": "raw"}).decode()
    album = _album(st)
    body = {"albumId": album, "newMediaItems": [
        {"simpleMediaItem": {"uploadToken": tok, "fileName": os.path.basename(path)}}]}
    try:
        res = _json("POST", LIB + "/mediaItems:batchCreate", body)
    except urllib.error.HTTPError as e:
        if e.code not in (400, 404):
            raise
        st["album"] = None            # album deleted in Google Photos: make a new one
        body["albumId"] = _album(st)
        res = _json("POST", LIB + "/mediaItems:batchCreate", body)
    r = (res.get("newMediaItemResults") or [{}])[0]
    if r.get("status", {}).get("code", 0) not in (0, None) and "mediaItem" not in r:
        raise RuntimeError(r.get("status", {}).get("message", "upload refused"))
    st["items"][path] = {"key": file_key(path), "id": r.get("mediaItem", {}).get("id"),
                         "at": int(time.time())}


# --- picker -----------------------------------------------------------------

def picker_session():
    return _json("POST", PICKER + "/sessions", {})


def picker_poll(sid):
    return _json("GET", PICKER + "/sessions/" + sid)


def picker_items(sid):
    out, page = [], None
    while True:
        q = {"sessionId": sid, "pageSize": 100}
        if page:
            q["pageToken"] = page
        d = _json("GET", PICKER + "/mediaItems?" + urllib.parse.urlencode(q))
        out += d.get("mediaItems", [])
        page = d.get("nextPageToken")
        if not page:
            return out


def picker_close(sid):
    try:
        _api("DELETE", PICKER + "/sessions/" + sid)
    except Exception:
        pass


def _duration_s(s, default):
    try:
        return float(str(s).rstrip("s"))
    except (TypeError, ValueError):
        return default


def run_picker(open_url, progress, cancelled=lambda: False):
    """Open Google's picker, wait for the selection, download it to ~/Pictures/Google.
    progress(text) reports; returns the list of saved paths. Run in a thread."""
    s = picker_session()
    sid = s["id"]
    try:
        open_url(s["pickerUri"].rstrip("/") + "/autoclose")
        poll = _duration_s(s.get("pollingConfig", {}).get("pollInterval"), 3)
        end = time.time() + _duration_s(s.get("pollingConfig", {}).get("timeoutIn"), 1800)
        progress("pick photos in the Google page…")
        while not s.get("mediaItemsSet"):
            if cancelled() or time.time() > end:
                return []
            time.sleep(max(1.0, poll))
            s = picker_poll(sid)
        items = picker_items(sid)
        os.makedirs(IMPORT_DIR, exist_ok=True)
        saved = []
        for i, it in enumerate(items):
            if cancelled():
                break
            mf = it.get("mediaFile", {})
            name = os.path.basename(mf.get("filename") or it["id"])
            dst = os.path.join(IMPORT_DIR, name)
            if os.path.exists(dst):
                saved.append(dst)
                continue
            progress(f"downloading {i + 1}/{len(items)}")
            suffix = "=dv" if it.get("type") == "VIDEO" else "=d"
            data = _api("GET", mf["baseUrl"] + suffix, timeout=600)
            with open(dst + ".part", "wb") as f:
                f.write(data)
            os.replace(dst + ".part", dst)
            ct = it.get("createTime")
            if ct:
                try:
                    t = datetime.datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
                    os.utime(dst, (t, t))
                except ValueError:
                    pass
            saved.append(dst)
        return saved
    finally:
        picker_close(sid)


def open_in_browser(url):
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def online_ok(require_wifi):
    """True if uploading is allowed now (Wi-Fi only = not metered)."""
    try:
        socket.create_connection(("photoslibrary.googleapis.com", 443), timeout=5).close()
    except OSError:
        return False
    if not require_wifi:
        return True
    # GLib's metered flag stays False on the modem here, so ask NetworkManager what is connected
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "TYPE,STATE", "dev"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split(":")[0] in ("wifi", "ethernet") and line.split(":")[1] == "connected"
               for line in out.splitlines() if line.count(":") >= 1)
