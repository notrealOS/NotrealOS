#!/bin/sh
# usbreset: revive the USB controller after "xhci ... HC died" (10-20: charger out -> KA5 in
# right after kills xhci; every suspend then fails with -22 until reboot).
# Step 1: dwc3 debugfs mode flip device -> host (recreates xhci). Step 2: driver unbind/bind.
# Run as root (passwordless via /etc/sudoers.d/usbreset). Log: /var/log/usbreset.log
DEV=a600000.usb
DRV=/sys/bus/platform/drivers/dwc3-qcom
MODE=/sys/kernel/debug/usb/$DEV/mode
LOG=/var/log/usbreset.log
log() { echo "$(date '+%m-%d %H:%M:%S') $*" | tee -a $LOG; }

# OK = a device actually enumerated on the port (a dead HC can keep usb1 registered)
xhci_ok() { [ -e /sys/bus/usb/devices/1-1 ]; }

log "start: role=$(cat /sys/class/usb_role/$DEV-role-switch/role 2>/dev/null) device=$(xhci_ok && echo yes || echo no) step=${1:-auto}"

if [ "${1:-auto}" != "rebind" ] && [ -w "$MODE" ]; then
	log "mode was: $(cat $MODE)"
	echo device > $MODE; sleep 2
	echo host > $MODE; sleep 3
	log "after mode flip: mode=$(cat $MODE) device=$(xhci_ok && echo yes || echo no)"
	xhci_ok && { log "OK (mode flip)"; exit 0; }
	[ "$1" = "mode" ] && exit 1
fi

log "rebinding $DEV"
echo $DEV > $DRV/unbind; sleep 2
echo $DEV > $DRV/bind; sleep 4
log "after rebind: driver=$(basename "$(readlink /sys/bus/platform/devices/$DEV/driver)") device=$(xhci_ok && echo yes || echo no)"
xhci_ok && { log "OK (rebind)"; exit 0; }
# 10-20 test: the rebind comes back in device role (ucsi doesn't resend host) -> force host again
if [ -w "$MODE" ]; then
	echo device > $MODE; sleep 2
	echo host > $MODE; sleep 3
	log "after rebind + mode flip: mode=$(cat $MODE) device=$(xhci_ok && echo yes || echo no)"
	xhci_ok && { log "OK (rebind + mode flip)"; exit 0; }
fi
log "FAILED"; exit 1
