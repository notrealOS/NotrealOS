#!/bin/sh
# Set the media (non-call) gain of both speaker amps and reinstall the service.
# Run as: sudo ~/callfix/callmode/mediagain.sh [GAIN]   (AMP_CFG gain 0-255; driver default 190)
set -e
G=${1:-120}
case $G in ''|*[!0-9]*) echo "gain must be a number 0-255"; exit 1 ;; esac
[ "$G" -le 255 ] || { echo "gain must be 0-255"; exit 1; }
CONF=/etc/conf.d/earpiece-callmode
[ -f $CONF ] && cp -n $CONF $CONF.before-mediagain
"$(dirname "$0")/install.sh"
if grep -q '^MEDIA_GAIN=' $CONF; then
	sed -i "s/^MEDIA_GAIN=.*/MEDIA_GAIN=$G/" $CONF
else
	printf '%s\n' '# Both amps outside calls (music, video). Lower = louder PA volume for the same loudness = cleaner sound.' "MEDIA_GAIN=$G" >> $CONF
fi
rc-service earpiece-callmode restart
sleep 1
"$(dirname "$0")/../amp.sh" dump
