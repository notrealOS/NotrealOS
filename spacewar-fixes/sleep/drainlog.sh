#!/bin/sh
# Battery drain test logger (10-16): one line every 20 s while awake (frozen during sleep).
# time charge_counter(uAh) capacity current_now(uA) backlight suspends rtc_wakes battmgr_wakes ucsi_wakes
B=/sys/class/power_supply/qcom-battmgr-bat
W=/sys/class/wakeup
wake() { for w in $W/*; do [ "$(cat $w/name)" = "$1" ] && cat $w/event_count; done; }
OUT="$HOME/sleepfix/drain-$(date +%m%d-%H%M).log"
echo "# start $(date '+%F %T')" > "$OUT"
while :; do
	bl=$(cat /sys/class/backlight/*/actual_brightness 2>/dev/null | head -1)
	echo "$(date +%s) $(cat $B/charge_counter) $(cat $B/capacity) $(cat $B/current_now) $bl $(cat /sys/power/suspend_stats/success) $(wake c440000.spmi:pmic@0:rtc@6100) $(wake qcom-battmgr-bat) $(wake ucsi-source-psy-pmic_glink.ucsi.01)" >> "$OUT"
	sleep 20
done
