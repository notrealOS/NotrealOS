#!/bin/sh
# safe-upgrade (10-24): system update that holds back the packages our custom work lives in.
#   sudo ~/update/safe-upgrade.sh          dry run: shows what would change
#   sudo ~/update/safe-upgrade.sh --go     really upgrade
# Held back (never upgraded here):
#   linux-postmarketos-qcom-sc7280  kernel modules: the running kernel is our own build (charging /
#                                   sleep patches, boot-c4x); new stock modules + old kernel image
#                                   = mismatched modules (audio/camera/Wi-Fi could stop loading)
#   postmarketos-mkinitfs, postmarketos-initramfs, device-nothing-spacewar, firmware-nothing-spacewar
#                                   boot image / device tree / DSP firmware: boot-callfix2 + C4X are ours
#   alsa-ucm-conf-qcom-sc7280       holds our speaker/mic/call UCM edits (~/speakerfix), would be overwritten
#   libcamera*                      already pinned to our r13 build in /etc/apk/world
set -e
HOLD="linux-postmarketos-qcom-sc7280 postmarketos-mkinitfs postmarketos-initramfs device-nothing-spacewar firmware-nothing-spacewar alsa-ucm-conf-qcom-sc7280 libcamera libcamera-ipa libcamera-tools"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
apk update
if [ "$1" != "--go" ]; then
	echo; echo "== DRY RUN (nothing changed). Held back: $HOLD"; echo
	apk upgrade -s --ignore $HOLD
	echo; echo "OK? then: sudo $0 --go"
	exit 0
fi
apk upgrade --ignore $HOLD
# our own modules (imx766, haptics, LEDs, patched audio) live in extra/ + updates/: re-index them
depmod -a "$(uname -r)"
for m in imx766 pm8350b-haptics leds-aw21018; do
	modinfo "$m" >/dev/null 2>&1 && echo "module $m: ok" || echo "module $m: MISSING"
done
echo; echo "Done. Reboot, then check: speaker + mic, a call, camera, Wi-Fi, Bluetooth, charging."
