# Joystick launcher

The Phosh app grid is replaced by one pulsing circle. Hold it, and your most-used apps fan out
right under your thumb: slide to one and let go to open it.

<p><img src="../../docs/screenshots/real-overview-joystick.png" width="300" alt="The overview with the joystick held: apps fanned out around the thumb"></p>

*The real phone: the overview widget at the top, an open app card in the middle, the joystick held at the
bottom with apps around it. (Personal agenda lines blacked out.)*

## How it works

- **Hold** the circle: your **top 7 apps** plus an **ALL** slot fan out around it, with the **next 10**
  in a fainter outer ring
- **Slide** toward an app: it grows and glows and its name shows; **let go** to open it
- **Let go near the middle** to cancel
- A **quick tap** leaves the fan open, so you can tap instead; tap the middle to close
- **ALL** (the 3×3 dots, upper left for the left hand) swaps the joystick for a searchable grid of
  every app
- The ranking learns from what you actually open (window activations, 7-day half-life)

Two styles (`joymode`):

- `float` (default): the joystick appears wherever your thumb lands; the balls bunch up against the
  screen edges instead of going off-screen
- `ring`: the classic fixed ring

```sh
~/arc/joymode ring     # or float; applies the next time the overview opens
```

## Files

- `joystick.py`: the launcher (GTK 4 + layer shell), shown and hidden together with the overview
  by [overview-widget](../overview-widget)
- `arc.py`: usage ranking and the older arc launcher
- `arc-loop`: keeps it running (autostart)
- `ftouch.py`: reads the touchscreen directly for the float mode
