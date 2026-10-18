#!/bin/sh
# Run as: sudo ~/callfix/callmode/install.sh
set -e
cd "$(dirname "$0")"
mkdir -p /usr/local/sbin
install -m 755 -o root -g root earpiece-callmode /usr/local/sbin/earpiece-callmode
install -m 755 -o root -g root earpiece-callmode.initd /etc/init.d/earpiece-callmode
CONF=/etc/conf.d/earpiece-callmode
[ -f $CONF ] || cat > $CONF <<'EOC'
# Earpiece/speaker gain range for calls (AMP_CFG gain field, 0-255; 190 = driver default).
# Call volume 0% -> GAIN_MIN, 100% -> GAIN_MAX.
GAIN_MIN=20
GAIN_MAX=190
EOC
grep -q '^VOIP_GAIN=' $CONF || printf '%s\n' '# Earpiece gain during VoIP calls (WhatsApp etc.); volume keys work on top of it.' 'VOIP_GAIN=100' >> $CONF
rc-update add earpiece-callmode default
rc-service earpiece-callmode restart
rc-service earpiece-callmode status
