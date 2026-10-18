#!/bin/sh
# Run as: sudo ~/crt/install.sh
# Lets the input group create uinput devices (crtd passes the power key on to Phosh
# through one). The user is already in input, which can read every keyboard anyway.
set -e
echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' > /etc/udev/rules.d/70-uinput-input.rules
chgrp input /dev/uinput
chmod 660 /dev/uinput
ls -l /dev/uinput
