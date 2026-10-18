#!/bin/sh
# Download the word-frequency lists (FrequencyWords by Hermit Dave, OpenSubtitles 2018,
# CC BY-SA 4.0) and build dict/<lang>.tsv for the swipe decoder.
set -e
HERE="$(dirname "$(readlink -f "$0")")"
mkdir -p "$HERE/dl-freq"
for l in en es fr sv; do
    [ -f "$HERE/dl-freq/${l}_50k.txt" ] || curl -fsSL -o "$HERE/dl-freq/${l}_50k.txt" \
        "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/$l/${l}_50k.txt"
done
python3 "$HERE/build_dict.py"
