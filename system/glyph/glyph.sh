#!/bin/sh
# Nothing Phone (1) Glyph prototype (AW21018 on i2c-1 @0x20, enable = TLMM GPIO 18). Run as root.
# usage: glyph.sh init | ch <1-18> [0-4095] | zone <diagonal|ring|camera|bar|dot|red> [0-4095] | all [0-4095] | sweep | off | id
set -e
BUS=1; ADDR=0x20
w() { i2cset -y -f $BUS $ADDR "$1" "$2"; }
r() { i2cget -y -f $BUS $ADDR "$1"; }
tlmm() { for c in /sys/bus/gpio/devices/gpiochip*; do [ "$(basename $(readlink -f $c/..))" = f100000.pinctrl ] && { basename $c; return; }; done; echo none; }
# Nothing T0 scaling table (lowest-current variant from the stock driver), channels 1..18
SL="0x44 0x90 0x90 0xD6 0x3E 0x00 0xB2 0x06 0x06 0x06 0x06 0x06 0x06 0x06 0x06 0x06 0x46 0x00"
setbr() { lo=$(( $2 & 0xff )); hi=$(( ($2 >> 8) & 0x0f )); reg=$(( 0x21 + ($1 - 1) * 2 )); w $reg $lo; w $((reg + 1)) $hi; }
clear() { m=1; while [ $m -le 18 ]; do setbr $m 0; m=$((m+1)); done; }
case "$1" in
id)   r 0x70 ;;
init)
	chip=$(tlmm); echo "enable GPIO 18 on $chip"
	pgrep -f "gpioset.*18=1" >/dev/null || gpioset -z -c "$chip" 18=1
	sleep 0.1
	echo "chip id: $(r 0x70) (expect 0x02)"
	w 0x70 0x00; sleep 0.01            # reset
	w 0x20 0x05                         # GCR: chip enable, 12-bit PWM, 16 MHz
	w 0x58 0x20                         # GCCR global current (stock T0 value)
	w 0x60 0x00                         # UVCR: under-voltage protection on
	n=0; for v in $SL; do w $(( 0x46 + n )) $v; n=$((n+1)); done
	clear; w 0x45 0x00; echo "init done" ;;
ch)   clear; setbr "$2" "${3:-600}"; w 0x45 0x00 ;;
all)  n=1; while [ $n -le 18 ]; do setbr $n "${2:-600}"; n=$((n+1)); done; w 0x45 0x00 ;;
off)  clear; w 0x45 0x00 ;;
sweep) rm -f /tmp/glyph-sweep.log; n=1; while [ $n -le 18 ]; do clear; setbr $n ${2:-1500}; w 0x45 0x00; echo "channel $n"; lo=$(( 0x21 + (n-1)*2 )); echo "ch $n: GCR=$(r 0x20) GCCR=$(r 0x58) BRL=$(r $lo) BRH=$(r $((lo+1))) SL=$(r $((0x45+n)))" >> /tmp/glyph-sweep.log; sleep 3; n=$((n+1)); done; clear; w 0x45 0x00; chmod 644 /tmp/glyph-sweep.log; echo done ;;
zone) case "$2" in diagonal) z="1";; ring) z="2 3 4 5";; camera) z="7";; bar) z="8 9 10 11 12 13 14 15";; dot) z="16";; red) z="17";; *) echo "zones: diagonal ring camera bar dot red"; exit 1;; esac; for c in $z; do setbr $c "${3:-1500}"; done; w 0x45 0x00 ;;
*)    echo "usage: $0 init | ch <1-18> [0-4095] | all [0-4095] | off | id" ;;
esac
