#!/bin/sh
# Shoot every app with made-up data into docs/screenshots/ (see shoot.sh: fully sandboxed).
# Usage: tools/shots/all.sh [name...]     THEME=micro by default
set -u
HERE="$(dirname "$(readlink -f "$0")")"
OUT="$(readlink -f "$HERE/../..")/docs/screenshots"
SRC="${NOTREAL_SRC:-$HOME}"     # where the apps live (the author's layout: ~/chat, ~/spin, ...)
D="$HERE/demo"
mkdir -p "$OUT"
shot() {   # name delay script [args] (env set by the caller)
    n=$1; shift
    case " ${ONLY:-} " in "  ") ;; *" $n "*) ;; *) return 0 ;; esac
    "$HERE/shoot.sh" "$OUT/$n.png" "$@" >"${TMPDIR:-/tmp}/shot-$n.log" 2>&1 && echo "ok   $n" || echo "FAIL $n (log ${TMPDIR:-/tmp}/shot-$n.log)"
}
ONLY="$*"
SNAP_PRELOAD=$D/mail.py shot mail 10 "$SRC/vapormail/vapormail.py"
SNAP_PRELOAD=$D/chat.py EXTRA_ENV=NOTAREALCHAT_LOCKED=1 shot chat 9 "$SRC/chat/chat.py"
SNAP_PRELOAD=$D/chat.py EXTRA_ENV=NOTAREALCHAT_LOCKED=1 SNAP_ACTION='w.get_application().open_chat("!anna")' SNAP_ACTION_AT=6 \
    shot chat-conversation 9 "$SRC/chat/chat.py"
SNAP_PRELOAD=$D/player.py SHOT_TIMEOUT=90 shot player 14 "$SRC/spin/spin.py"
SNAP_PRELOAD=$D/player.py SHOT_TIMEOUT=90 SNAP_ACTION_AT=8 \
    SNAP_ACTION='(w.player.play_tracks(w.store.album("al0")["song"], start=2), GLib.timeout_add(1500, lambda: w.open_now_playing()))' \
    shot player-now-playing 16 "$SRC/spin/spin.py"
SNAP_PRELOAD=$D/player.py SHOT_TIMEOUT=90 SNAP_ACTION_AT=8 THEME=nasa \
    SNAP_ACTION='(w.player.play_tracks(w.store.album("al3")["song"], start=1), w.np.set_style("cassette"), GLib.timeout_add(1500, lambda: w.open_now_playing()))' \
    shot player-cassette 16 "$SRC/spin/spin.py"
for sc in place routes nav; do
    SNAP_PRELOAD=$D/maps.py EXTRA_ENV="MAPS_SCENE=$sc" shot maps-$sc $([ $sc = nav ] && echo 12 || echo 9) "$SRC/maps/maps.py"
done
SNAP_PRELOAD=$D/feed.py shot feed 10 "$SRC/reddit/notarealreddit.py"
SNAP_PRELOAD=$D/phone.py shot phone 8 "$SRC/calls/nrcalls.py"
SNAP_PRELOAD=$D/gallery.py shot gallery 18 "$SRC/gallery/gallery.py"
shot sound 7 "$SRC/nrvolume/nrvolume.py"
SNAP_PRELOAD=$D/calendar.py shot calendar 10 "$SRC/calendar/nrcalendar.py"
SNAP_PRELOAD=$D/contacts.py shot contacts 10 "$SRC/contacts/nrcontacts.py"
SAMPLE="${TMPDIR:-/tmp}/notreal-sample.jpg"
[ -s "$SAMPLE" ] || python3 "$D/sample.py" "$SAMPLE"
shot edit 7 "$SRC/edit/nredit.py" "$SAMPLE"
shot link 12 "$SRC/link/nrlink.py" "https://example.com"
for t in vapor vaporlight micro microdark alien nasa; do   # one app in every theme, for shell/theme
    THEME=$t SNAP_PRELOAD=$D/chat.py EXTRA_ENV=NOTAREALCHAT_LOCKED=1 shot theme-$t 9 "$SRC/chat/chat.py"
done
