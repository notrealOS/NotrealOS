#!/bin/sh
# notreal.player installer for any Linux (desktop or phone), no root needed.
#   ./install.sh               install for this user (app menu, icon, `notreal-player` command, fonts)
#   ./install.sh --no-fonts    skip downloading the theme fonts
#   ./install.sh --uninstall   remove it again (your settings and offline songs stay in ~/.config/spin, ~/.local/share/spin)
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
APP=$DATA/notreal.player
BIN=$HOME/.local/bin/notreal-player
DESKTOP=$DATA/applications/dev.notareal.Player.desktop
ICON=$DATA/icons/hicolor/scalable/apps/dev.notareal.Player.svg

if [ "$1" = "--uninstall" ]; then
	rm -rf "$APP" "$BIN" "$DESKTOP" "$ICON"
	echo "notreal.player removed"
	exit 0
fi

if [ "$HERE" = "$HOME/spin" ]; then
	echo "this is the notrealOS phone's own copy (~/spin): it already runs from here" >&2
	exit 1
fi
if [ ! -f "$HERE/theme/notareal_theme.py" ]; then
	echo "theme/ is missing: get the folder from the notrealOS repo (apps/notreal.player)" >&2
	exit 1
fi

# what's missing, and the package names on the common distros
missing=""
command -v python3 >/dev/null || { echo "needs python3" >&2; exit 1; }
python3 - <<'EOF' || missing=1
import gi, sys
bad = []
for lib, ver in (("Gtk", "4.0"), ("Adw", "1"), ("Gst", "1.0")):
    try:
        gi.require_version(lib, ver)
        __import__("gi.repository." + lib)
    except (ValueError, ImportError):
        bad.append(f"{lib} {ver}")
if bad:
    print("missing:", ", ".join(bad), file=sys.stderr)
    sys.exit(1)
from gi.repository import Adw
if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 6):
    print("libadwaita 1.6 or newer is needed", file=sys.stderr)
    sys.exit(1)
EOF
if [ -n "$missing" ]; then
	cat >&2 <<'EOF'
install these first, then run ./install.sh again:
  Alpine / postmarketOS: apk add py3-gobject3 gtk4.0 libadwaita gst-plugins-good gst-plugins-bad gst-libav
  Debian / Ubuntu:       apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gstreamer-1.0 gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav
  Fedora:                dnf install python3-gobject gtk4 libadwaita gstreamer1-plugins-good gstreamer1-plugins-bad-free gstreamer1-libav
  Arch:                  pacman -S python-gobject gtk4 libadwaita gst-plugins-good gst-plugins-bad gst-libav
EOF
	exit 1
fi
command -v magick >/dev/null || echo "note: ImageMagick (magick) makes smaller cover thumbnails; optional"

mkdir -p "$APP" "$(dirname "$BIN")" "$(dirname "$DESKTOP")" "$(dirname "$ICON")"
# copy the app (not the PC-side server/), keep a theme you picked before
active=$(cat "$APP/theme/active" 2>/dev/null || true)
rm -rf "$APP"
mkdir -p "$APP"
(cd "$HERE" && tar cf - --exclude=server --exclude=__pycache__ --exclude=install.sh .) | (cd "$APP" && tar xf -)
[ -n "$active" ] && printf '%s\n' "$active" > "$APP/theme/active"

cat > "$BIN" <<EOF
#!/bin/sh
exec python3 "$APP/spin.py" "\$@"
EOF
chmod 755 "$BIN"
cp "$HERE/icons/dev.notareal.Player.svg" "$ICON"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Name=notreal.player
Comment=Navidrome music with a spinning vinyl
Keywords=music;player;navidrome;subsonic;
Exec=$BIN
Icon=dev.notareal.Player
Terminal=false
Type=Application
Categories=AudioVideo;Audio;Player;
StartupWMClass=dev.notareal.Player
X-Purism-FormFactor=Workstation;Mobile;
EOF
command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -qtf "$DATA/icons/hicolor" 2>/dev/null || true

if [ "$1" != "--no-fonts" ]; then
	if command -v curl >/dev/null; then
		sh "$HERE/theme/get-fonts.sh" || echo "fonts not downloaded (offline?): run $HERE/theme/get-fonts.sh later"
	else
		echo "note: no curl, theme fonts skipped (run theme/get-fonts.sh later)"
	fi
fi

echo "notreal.player installed: open it from the app menu, or run notreal-player"
echo "sign in with your Navidrome server's address; pick a theme in Settings > Look"
