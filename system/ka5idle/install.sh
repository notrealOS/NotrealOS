#!/bin/sh
U="${SUDO_USER:-$(logname)}"   # the phone user this sets things up for
HERE="$(dirname "$(readlink -f "$0")")"
# run with sudo. v2: ka5hid (root-owned) + NOPASSWD sudoers entry; drops the v1 libinput rule
set -e
install -o root -g root -m 755 $HERE/ka5hid /usr/local/bin/ka5hid
echo '$U ALL=(root) NOPASSWD: /usr/local/bin/ka5hid' > /tmp/ka5hid.sudoers
visudo -c -f /tmp/ka5hid.sudoers
install -o root -g root -m 440 /tmp/ka5hid.sudoers /etc/sudoers.d/ka5hid
rm -f /tmp/ka5hid.sudoers /etc/udev/rules.d/90-ka5-libinput-ignore.rules
udevadm control --reload
echo "installed."
