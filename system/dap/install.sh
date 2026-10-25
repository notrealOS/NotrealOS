#!/bin/sh
# sudo ~/dap/install.sh : installs the DAP-mode CPU helper (passwordless, like ka5hid/usbreset)
set -e
U=${SUDO_USER:-$(logname)}
install -m 755 -o root -g root "$(dirname "$0")/nrdap-cpu" /usr/local/bin/nrdap-cpu
echo "$U ALL=(root) NOPASSWD: /usr/local/bin/nrdap-cpu" >/etc/sudoers.d/nrdap
chmod 440 /etc/sudoers.d/nrdap
visudo -c -f /etc/sudoers.d/nrdap
echo "nrdap-cpu installed"
