# Screenshots

`all.sh` photographs every app into `docs/screenshots/` without touching the phone you run it on:

- each app runs in a **headless phoc** at the Nothing Phone (1)'s size (1080×2400 at 2×)
- with a **throwaway home folder**, its own **D-Bus session** and **no system bus** (so no modem,
  network or power settings), a private silent **PulseAudio**, no portals and no GVFS
- `demo/<app>.py` fills the app with **made-up data** before it starts: fake mail, a chat database,
  a fake music server with generated covers, a fake Google Maps backend on OpenStreetMap tiles,
  fake feed posts, a fake call history, generated photos, fake calendars and contacts

```sh
tools/shots/all.sh                 # everything
tools/shots/all.sh chat player     # just these
THEME=nasa tools/shots/all.sh mail # another theme
```

One app by hand: `shoot.sh OUT.png DELAY_S SCRIPT [ARGS]`, with `SNAP_PRELOAD`, `SNAP_ACTION`
(Python, `w` = the window) and `SNAP_ACTION_AT` (seconds) to set the scene.
