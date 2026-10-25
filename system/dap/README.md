# DAP mode

`nrdap on|off|toggle|status`: the phone becomes a music player. Modem, Wi-Fi and Bluetooth off,
background daemons stopped, CPU capped, notreal.player opened (offline songs). Measured 633 to
380 mA while playing through a USB DAC.

`sudo ./install.sh` installs the CPU helper (`nrdap-cpu`) with passwordless sudo.
