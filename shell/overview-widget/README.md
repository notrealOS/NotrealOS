# Overview widget

A tappable panel at the top of the Phosh overview (the screen with your open apps).

<p>
<img src="../../docs/screenshots/real-overview-joystick.png" width="260" alt="The widget above the open apps">
<img src="../../docs/screenshots/real-overview-ram.png" width="260" alt="The widget with the RAM view open">
</p>

*Real phone, agenda lines blacked out. Right: tapping RAM lists what uses memory, with END buttons.*

## What's on it

- **Weather**: temperature, conditions, high/low, wind ([Open-Meteo](https://open-meteo.com), no API key)
- **◐ theme picker**: switch between the six [themes](../theme) live
- **Battery and RAM**: tap RAM for a list of what uses memory, with END for apps (system parts can't be ended)
- **The face**: the phone's health from [nrdoctor](../../system/doctor) (happy / meh / sad); tap it
  for a system check that restarts anything that died
- **Now playing**: title and prev / play / next for any media player (MPRIS)
- **Agenda**: today's events, then your reminders; tap to open the [search pop-up](../phantomshell)

It also shows and hides the [joystick](../joystick) with the overview.

## How it knows the overview is open

Phosh doesn't publish that, so the widget works it out:

- `toplevel-watch.c` (wlr-foreign-toplevel) reports when no window is active: overview, lock screen
  or notification shade
- the screensaver D-Bus signal tells the lock screen apart
- Phosh's drag state tells the shade apart

`~/.config/gtk-3.0/gtk.css` pushes the Phosh overview down to make room for the widget.
