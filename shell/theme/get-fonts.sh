#!/bin/sh
# Download the theme fonts (all SIL Open Font License, from github.com/google/fonts) into ~/.local/share/fonts.
set -e
D="${XDG_DATA_HOME:-$HOME/.local/share}/fonts"
mkdir -p "$D"
for f in michroma/Michroma-Regular.ttf sharetechmono/ShareTechMono-Regular.ttf \
         spacemono/SpaceMono-Regular.ttf spacemono/SpaceMono-Bold.ttf vt323/VT323-Regular.ttf; do
    [ -f "$D/${f#*/}" ] || curl -fsSL -o "$D/${f#*/}" "https://github.com/google/fonts/raw/main/ofl/$f"
done
fc-cache -f "$D" >/dev/null
echo "fonts installed in $D"
