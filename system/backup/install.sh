#!/bin/sh
U="${SUDO_USER:-$(logname)}"   # the phone user this sets things up for
HERE="$(dirname "$(readlink -f "$0")")"
# One-time setup for the phone backup (run with: sudo ~/backup/install.sh)
set -e
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
apk add restic
install -o root -g root -m 755 $HERE/nrbackup-system /usr/local/bin/nrbackup-system
echo '$U ALL=(root) NOPASSWD: /usr/local/bin/nrbackup-system' > /etc/sudoers.d/nrbackup
chmod 440 /etc/sudoers.d/nrbackup
command -v visudo >/dev/null && visudo -cf /etc/sudoers.d/nrbackup
echo "backup installed"
