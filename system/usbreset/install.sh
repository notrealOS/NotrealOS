#!/bin/sh
# run with sudo: installs /usr/local/sbin/usbreset + passwordless sudo for the calling user
set -e
U=${SUDO_USER:-$(logname)}
install -m 755 "$(dirname "$0")/usbreset.sh" /usr/local/sbin/usbreset
echo "$U ALL=(root) NOPASSWD: /usr/local/sbin/usbreset" > /etc/sudoers.d/usbreset
chmod 440 /etc/sudoers.d/usbreset
visudo -cf /etc/sudoers.d/usbreset
ls /sys/kernel/debug/usb/a600000.usb/ 2>&1
echo installed
