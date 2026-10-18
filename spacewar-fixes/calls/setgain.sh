#!/bin/sh
# Set the call gain range and restart the service. Run as: sudo ~/callfix/callmode/setgain.sh MIN MAX
# (AMP_CFG gain field 0-255; 0 = silent, 190 = driver default.) Call volume 0% -> MIN, 100% -> MAX.
case "$1$2" in ''|*[!0-9]*) sed -n '2,3p' "$0"; exit 1 ;; esac
sed -i "s/^GAIN_MIN=.*/GAIN_MIN=$1/; s/^GAIN_MAX=.*/GAIN_MAX=$2/" /etc/conf.d/earpiece-callmode
rc-service earpiece-callmode restart
grep GAIN_ /etc/conf.d/earpiece-callmode
