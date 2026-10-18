#!/bin/sh
# Run as: sudo ~/speakerfix/micgain.sh
# Raises the main mic gain in the HiFi profile (video recording, VoIP): ADC1 16 -> 20
# (+6 dB analog, the max) and TX_DEC0/1 84 -> 96 (0 -> +12 dB digital). Measured
# 2026-10-09: speech -50 -> -34 dBFS. Also sets TX0 MODE ADC_NORMAL: ADC1 is TX0 on the
# WCD938x (upstream codecs/wcd938x/AnalogMic1EnableSeq.conf); the file only set TX1, so
# ADC1 ran in ADC_INVALID mode. The Voice Call profile is left alone.
# Backup of the previous file: ~/speakerfix/HiFi.conf.installed-20261009
set -e
F=/usr/share/alsa/ucm2/Nothing/spacewar/HiFi.conf
sed -i -e "s/cset \"name='ADC1 Volume' 16\"/cset \"name='ADC1 Volume' 20\"/" \
       -e "s/cset \"name='TX_DEC0 Volume' 84\"/cset \"name='TX_DEC0 Volume' 96\"/" \
       -e "s/cset \"name='TX_DEC1 Volume' 84\"/cset \"name='TX_DEC1 Volume' 96\"/" $F
grep -q "TX0 MODE" $F || sed -i "s/\(\t*\)cset \"name='TX1 MODE' ADC_LP\"/&\n\1cset \"name='TX0 MODE' ADC_NORMAL\"/" $F
grep -n "ADC1 Volume\|TX_DEC[01] Volume\|TX[01] MODE" $F
