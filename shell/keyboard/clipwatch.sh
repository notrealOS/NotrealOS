#!/bin/sh
# one line per clipboard change: base64 of the text, or "S" (skipped: password-manager secret or not text)
T=$(wl-paste -l 2>/dev/null)
cat >/dev/null
case "$T" in
*x-kde-passwordManagerHint*) echo S ;;
*text/plain*|*UTF8_STRING*|*STRING*|*TEXT*) wl-paste -n -t text 2>/dev/null | base64 -w0; echo ;;
*) echo S ;;
esac
