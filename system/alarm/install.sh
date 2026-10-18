#!/bin/sh
U="${SUDO_USER:-$(logname)}"   # the phone user this sets things up for
# One-time root setup for alarmd: let the phone user set the RTC wake alarm (rtc0/wakealarm),
# so alarms can wake the phone from deep sleep. Run: sudo ~/alarm/install.sh
set -e
cat > /etc/udev/rules.d/90-alarmd-rtc.rules <<'RULE'
# alarmd (~/alarm): the user may set the RTC wake-up time
SUBSYSTEM=="rtc", KERNEL=="rtc0", RUN+="/bin/chown $U /sys%p/wakealarm"
RULE
chown $U /sys/class/rtc/rtc0/wakealarm
ls -l /sys/class/rtc/rtc0/wakealarm
echo "alarmd: RTC wake alarm enabled"
