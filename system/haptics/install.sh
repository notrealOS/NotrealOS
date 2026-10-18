#!/bin/sh
U="${SUDO_USER:-$(logname)}"   # the phone user this sets things up for
HERE="$(dirname "$(readlink -f "$0")")"
# Install pm8350b-haptics permanently: module at boot + feedbackd udev tag. Undo: install.sh --undo
set -e
K=$(uname -r)
if [ "$1" = "--undo" ]; then
	rm -f /lib/modules/$K/extra/pm8350b-haptics.ko /etc/modules-load.d/pm8350b-haptics.conf \
	      /etc/modprobe.d/pm8350b-haptics.conf /etc/udev/rules.d/72-pm8350b-haptics.rules
	depmod -a "$K"
	rmmod pm8350b_haptics 2>/dev/null || true
	echo "removed"; exit 0
fi
VMAX=${VMAX:-2000}
mkdir -p /lib/modules/$K/extra
install -m 644 $HERE/pm8350b-haptics.ko /lib/modules/$K/extra/
depmod -a "$K"
mkdir -p /etc/modules-load.d /etc/modprobe.d
echo pm8350b-haptics > /etc/modules-load.d/pm8350b-haptics.conf
echo "options pm8350b-haptics vmax_mv=$VMAX" > /etc/modprobe.d/pm8350b-haptics.conf
cat > /etc/udev/rules.d/72-pm8350b-haptics.rules <<'E'
SUBSYSTEM=="input", KERNEL=="event*", ENV{ID_INPUT}=="1", SUBSYSTEMS=="input", ATTRS{name}=="pm8350b-haptics", TAG+="uaccess", ENV{FEEDBACKD_TYPE}="vibra"
E
udevadm control --reload
rmmod pm8350b_haptics 2>/dev/null || true
modprobe pm8350b-haptics
echo "$VMAX" > /sys/module/pm8350b_haptics/parameters/vmax_mv
udevadm trigger --action=change --subsystem-match=input
# feedbackd only scans for vibra devices at start: restart the session one (D-Bus restarts it on demand)
pkill -x feedbackd -U $U 2>/dev/null || true
echo "installed (vmax $VMAX mV)"
