#!/bin/sh
# run with sudo: KA5 buttons stay bound for good again (KA5 stays awake when idle)
/usr/local/bin/ka5hid on || true
rm -f /usr/local/bin/ka5hid /etc/sudoers.d/ka5hid /etc/udev/rules.d/90-ka5-libinput-ignore.rules
udevadm control --reload
pkill -f ka5idle.py || true
rm -f "$(getent passwd "${SUDO_USER:-$(logname)}" | cut -d: -f6)"/.config/autostart/ka5idle.desktop
echo "removed. Replug the KA5 if its buttons don't work."
