#!/bin/sh
# test-dap.sh: battery draw while playing, normal vs DAP mode (screen off). Results: ~/dap/test.log
# Start a DOWNLOADED album/favourites first, then run this and turn the screen off within 15 s.
# 3 min normal, 3 min DAP, then DAP mode switches itself off again.
# runs detached: a `!` command in Claude Code is killed when DAP mode drops the internet (10-21)
if [ -z "$DAP_TEST_BG" ]; then
	DAP_TEST_BG=1 setsid nohup "$0" >/dev/null 2>&1 &
	echo "DAP test running in the background (~6.5 min); turn the screen off now. Result: ~/dap/test.log"
	exit 0
fi
B=/sys/class/power_supply/qcom-battmgr-bat
L="$HOME/dap/test.log"
avg() {  # avg <seconds> -> mean current in mA (first 30 s skipped to settle)
	sleep 30; n=0; s=0; end=$(( $(date +%s) + $1 - 30 ))
	while [ "$(date +%s)" -lt "$end" ]; do
		c=$(cat $B/current_now); s=$((s + c)); n=$((n + 1)); sleep 2
	done
	echo $(( -s / n / 1000 ))
}
playing() { gdbus call --session --dest org.mpris.MediaPlayer2.notarealPlayer --object-path /org/mpris/MediaPlayer2 \
	--method org.freedesktop.DBus.Properties.Get org.mpris.MediaPlayer2.Player PlaybackStatus 2>/dev/null | grep -o "'[A-Za-z]*'"; }
echo "$(date '+%m-%d %H:%M') test start ($(cat $B/capacity)%), playing: $(playing)" >>"$L"
sleep 15
normal=$(avg 180); p1=$(playing)
"$HOME/dap/nrdap" on; sleep 5
dap=$(avg 180); p2=$(playing)
"$HOME/dap/nrdap" off
echo "  normal: ${normal} mA ($p1)   DAP: ${dap} mA ($p2)   saving: $(( (normal - dap) * 100 / normal ))%" >>"$L"
notify-send -a "DAP mode" "DAP test done" "normal ${normal} mA, DAP ${dap} mA" 2>/dev/null
