#!/bin/sh
# Install VoiceCall.conf without the voice-mixer lines in its DisableSequence (sudo).
set -e
DST=/usr/share/alsa/ucm2/Nothing/spacewar/VoiceCall.conf
[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"
cp "$DST" "$DST.before-keepvoice"
install -m 644 "$(dirname "$0")/VoiceCall.conf" "$DST"
alsaucm -c hw:0 list _verbs >/dev/null 2>&1 && echo "UCM parses OK" || echo "WARNING: alsaucm could not parse the UCM"
echo "done (backup: $DST.before-keepvoice)"
