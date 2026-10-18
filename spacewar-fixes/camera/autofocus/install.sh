#!/bin/sh
# Install + start afd (rear camera focus). Run with sudo.
set -e
HERE="$(dirname "$(readlink -f "$0")")"
install -m 755 "$HERE"/afd.py /usr/local/sbin/afd
install -m 755 "$HERE"/afd.initd /etc/init.d/afd
rc-update add afd default
rc-service afd restart
echo "afd installed"
cat /sys/bus/i2c/devices/19-0050/eeprom > "$HERE"/eeprom-wide.bin && chown "${SUDO_USER:-$(logname)}" "$HERE"/eeprom-wide.bin   # your module's own calibration: keep it private
