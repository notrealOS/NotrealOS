#!/bin/sh
# Screenshot one app in a sandbox: headless phoc, a private D-Bus session and a throwaway home
# folder, so the app can't see any real accounts, messages, photos or settings.
# Usage: shoot.sh OUT.png DELAY_S SCRIPT [ARGS...]
#   env: THEME=micro  SEED=<dir copied into the sandbox home>  EXTRA_ENV="K=V ..."  SNAP_ACTION=<python, w = window>
set -e
HERE="$(dirname "$(readlink -f "$0")")"
REAL_HOME="$HOME"
USER_SITE="$(python3 -m site --user-site)"            # pip --user modules (pulsectl, ...): code only
SB="$(mktemp -d "${TMPDIR:-/tmp}/nrshot.XXXXXX")"
RUN="$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/nrshot.XXXXXX")"   # short path: Wayland sockets max 108 chars
cleanup() { sleep 1; for m in "$RUN"/doc "$RUN"/gvfs; do fusermount3 -u "$m" 2>/dev/null || fusermount -u "$m" 2>/dev/null || true; done; rm -rf "$SB" "$RUN"; }
trap cleanup EXIT
mkdir -p "$SB/home/.local/share" && chmod 700 "$RUN"
cp -r "$REAL_HOME/theme" "$SB/home/theme"            # theme code + CSS (no personal data)
rm -f "$SB/home/theme/lock/vapor"*                    # third-party artwork: never in a screenshot
echo "${THEME:-micro}" > "$SB/home/theme/active"
for d in fonts icons; do [ -d "$REAL_HOME/.local/share/$d" ] && cp -r "$REAL_HOME/.local/share/$d" "$SB/home/.local/share/"; done
cp -r "$HERE/home"/. "$SB/home/"                       # phone-like GTK settings (no window buttons)
[ -n "${SEED:-}" ] && cp -r "$SEED"/. "$SB/home/"
OUT="$(readlink -f "$(dirname "$1")")/$(basename "$1")"; shift
rm -f "$OUT"
cat > "$SB/inner.sh" <<EOI
#!/bin/sh
# a private, silent sound server: audio apps work, nothing reaches the real speaker
pulseaudio -n --daemonize=no --exit-idle-time=-1 --disallow-exit --load=module-native-protocol-unix \
    --load="module-null-sink sink_name=demo sink_properties=device.description=Speaker" >/dev/null 2>&1 &
A=\$!
sleep 1
phoc -C "$HERE/phoc-portrait.ini" -E "python3 $HERE/snap.py '$OUT' $*" &
P=\$!
i=0
while [ \$i -lt ${SHOT_TIMEOUT:-60} ] && [ ! -s "$OUT" ] && kill -0 \$P 2>/dev/null; do sleep 1; i=\$((i+1)); done
sleep 1; kill \$P \$A 2>/dev/null; wait \$P 2>/dev/null
EOI
env -i PATH="$PATH" HOME="$SB/home" USER="$USER" LANG="${LANG:-en_US.UTF-8}" XDG_RUNTIME_DIR="$RUN" \
    WLR_BACKENDS=headless WLR_RENDERER=pixman WLR_LIBINPUT_NO_DEVICES=1 GSK_RENDERER=cairo \
    DBUS_SYSTEM_BUS_ADDRESS=unix:path=/nonexistent GIO_USE_VFS=local GVFS_DISABLE_FUSE=1 GDK_DEBUG=no-portals GTK_USE_PORTAL=0 NOTREAL_SHOT=1 PYTHONPATH="$SB/home/theme:$USER_SITE" SNAP_ACTION="${SNAP_ACTION:-}" SNAP_PRELOAD="${SNAP_PRELOAD:-}" SNAP_ACTION_AT="${SNAP_ACTION_AT:-}" ${EXTRA_ENV:-} \
    dbus-run-session -- sh "$SB/inner.sh"
[ -s "$OUT" ] && echo "ok $OUT" || { echo "FAILED $OUT" >&2; exit 1; }
