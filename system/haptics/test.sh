#!/bin/sh
U="${SUDO_USER:-$(logname)}"   # the phone user this sets things up for
HERE="$(dirname "$(readlink -f "$0")")"
# First haptics test: load the module at a gentle 2 V, log what it found, buzz three times.
cd $HERE || exit 1
log=$HERE/test.log
insmod ./pm8350b-haptics.ko vmax_mv=2000 || { dmesg | tail -5 > "$log"; chmod 644 "$log"; exit 1; }
sleep 0.5
dmesg | grep -i haptics > "$log"
chmod 644 "$log"
for ms in 60 200 500; do
	su $U -c "python3 $HERE/buzz.py $ms 100"
	sleep 0.6
done
dmesg | grep -i haptics | tail -5 >> "$log"
echo "done, see $log"
