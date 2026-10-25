"""QR codes for notarealCamera (10-24): libzbar through ctypes (no py3-zbar on the phone, the
library itself is installed), plus what a decoded payload means (link, Wi-Fi, phone, text).

    qr.scan(gray)  -> list of decoded strings; gray = 2-D uint8 numpy array (C-contiguous)
    qr.parse(text) -> {"kind": "url"|"wifi"|"tel"|"sms"|"mail"|"geo"|"text", "title": ..., ...}
"""
import ctypes
import re

_z = ctypes.CDLL("libzbar.so.0")
_z.zbar_image_scanner_create.restype = ctypes.c_void_p
_z.zbar_image_scanner_set_config.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
_z.zbar_image_create.restype = ctypes.c_void_p
_z.zbar_image_set_format.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
_z.zbar_image_set_size.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
_z.zbar_image_set_data.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
_z.zbar_image_destroy.argtypes = [ctypes.c_void_p]
_z.zbar_scan_image.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_z.zbar_image_first_symbol.argtypes = [ctypes.c_void_p]
_z.zbar_image_first_symbol.restype = ctypes.c_void_p
_z.zbar_symbol_next.argtypes = [ctypes.c_void_p]
_z.zbar_symbol_next.restype = ctypes.c_void_p
_z.zbar_symbol_get_data.argtypes = [ctypes.c_void_p]
_z.zbar_symbol_get_data.restype = ctypes.c_void_p
_z.zbar_symbol_get_data_length.argtypes = [ctypes.c_void_p]
_z.zbar_symbol_get_data_length.restype = ctypes.c_uint

QRCODE, CFG_ENABLE, CFG_BINARY = 64, 0, 66
Y800 = 0x30303859

_scanner = _z.zbar_image_scanner_create()
_z.zbar_image_scanner_set_config(_scanner, 0, CFG_ENABLE, 0)          # nothing but QR
_z.zbar_image_scanner_set_config(_scanner, QRCODE, CFG_ENABLE, 1)
_z.zbar_image_scanner_set_config(_scanner, QRCODE, CFG_BINARY, 1)     # raw bytes; we decode UTF-8


def scan(gray):
    h, w = gray.shape
    img = _z.zbar_image_create()
    try:
        _z.zbar_image_set_format(img, Y800)
        _z.zbar_image_set_size(img, w, h)
        _z.zbar_image_set_data(img, gray.ctypes.data, w * h, None)
        if _z.zbar_scan_image(_scanner, img) <= 0:
            return []
        out, sym = [], _z.zbar_image_first_symbol(img)
        while sym:
            raw = ctypes.string_at(_z.zbar_symbol_get_data(sym), _z.zbar_symbol_get_data_length(sym))
            try:
                out.append(raw.decode("utf-8"))
            except UnicodeDecodeError:
                out.append(raw.decode("latin-1"))
            sym = _z.zbar_symbol_next(sym)
        return out
    finally:
        _z.zbar_image_destroy(img)


def _wifi_field(body, key):
    # WIFI:T:WPA;S:name;P:pass;H:false;;  values may escape ; , : \ with a backslash
    m = re.search(r"(?:^|;)" + key + r":((?:\\.|[^;])*)", body)
    return re.sub(r"\\(.)", r"\1", m.group(1)) if m else ""


def parse(text):
    t = text.strip()
    low = t.lower()
    if low.startswith("wifi:"):
        body = t[5:]
        ssid = _wifi_field(body, "S")
        return {"kind": "wifi", "title": f"Wi-Fi  {ssid}", "ssid": ssid,
                "password": _wifi_field(body, "P"), "security": _wifi_field(body, "T").upper(),
                "hidden": _wifi_field(body, "H").lower() == "true"}
    if low.startswith(("http://", "https://")):
        host = re.sub(r"^https?://(www\.)?", "", t, flags=re.I).split("/")[0]
        return {"kind": "url", "title": host or t, "uri": t}
    if re.match(r"^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(/\S*)?$", low):
        return {"kind": "url", "title": t.split("/")[0], "uri": "https://" + t}
    if low.startswith("tel:"):
        return {"kind": "tel", "title": t[4:], "uri": t}
    if low.startswith(("smsto:", "sms:")):
        num = t.split(":", 2)[1]
        return {"kind": "sms", "title": f"SMS  {num}", "uri": "sms:" + num}
    if low.startswith("mailto:") or low.startswith("matmsg:"):
        addr = re.search(r"(?:mailto:|TO:)([^;?]+)", t, re.I)
        addr = addr.group(1) if addr else t
        return {"kind": "mail", "title": addr, "uri": "mailto:" + addr}
    if low.startswith("geo:"):
        return {"kind": "geo", "title": t[4:].split("?")[0], "uri": t}
    first = t.splitlines()[0] if t else ""
    return {"kind": "text", "title": first[:60] or "(empty)", "text": t}
