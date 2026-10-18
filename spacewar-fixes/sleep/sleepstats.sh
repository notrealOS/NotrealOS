#!/bin/sh
# Which SoC subsystems reached their low-power state between two runs (10-17).
# Run with sudo: 1st run saves a snapshot, lock the phone ~10 min, unlock, run again -> per
# subsystem: sleep entries + seconds asleep since the snapshot (+ the battery charge used).
# cxsd/aosd = the whole chip in deep sleep; they can only count when EVERY subsystem sleeps.
S=/sys/kernel/debug/qcom_stats
B=/sys/class/power_supply/qcom-battmgr-bat
SNAP=/run/sleepstats.snap
LOG="$(dirname "$(readlink -f "$0")")/sleepstats.log"
now() {
	echo "time $(date +%s) 0"
	echo "charge $(cat $B/charge_counter) 0"
	for f in aosd cxsd ddr adsp adsp_island cdsp wpss modem; do
		[ -r $S/$f ] || continue
		c=$(sed -n 's/^Count: //p' $S/$f)
		d=$(sed -n 's/^Accumulated Duration: //p' $S/$f)
		echo "$f $c $d"
	done
}
if [ ! -f $SNAP ]; then
	now > $SNAP
	echo "=== snapshot $(date '+%F %T') ${TEST:-}" >> "$LOG"
	echo "snapshot saved. Lock the phone now, leave it ~10 min, unlock, run this again."
	exit 0
fi
now > $SNAP.new
awk 'NR==FNR { c[$1]=$2; d[$1]=$3; next }
	$1=="time"   { secs=$2-c["time"]; next }
	$1=="charge" { mah=(c["charge"]-$2)/1000; next }
	{ printf "%-12s %6d entries  %8.0f s asleep\n", $1, $2-c[$1], ($3-d[$1])/19200000 }
	END { printf "over %d s; battery used %.0f mAh = %.0f mA average\n", secs, mah, mah*3600/secs }' \
	$SNAP $SNAP.new | tee -a "$LOG"
chmod a+r "$LOG"
mv $SNAP.new $SNAP
echo "(new snapshot saved: run again after the next test)"
