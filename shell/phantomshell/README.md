# notarealShell: search, reminders and notes

The layer behind every app: the home-screen wallpaper (drawn per theme), and a **search pop-up**
that holds your **reminders** and **notes**.

<p><img src="../../docs/screenshots/real-search-reminders.png" width="300" alt="The search pop-up with reminders and notes"></p>

*Real phone. The reminder texts and the agenda are blacked out.*

## Open it

- **swipe up from the left edge**, tap the wallpaper, or tap the agenda on the
  [overview widget](../overview-widget)
- or say it: hold **volume up** and talk to the [voice assistant](../../system/voice)

## What you can type

| Type | Does |
| --- | --- |
| anything + Enter | a web search |
| an app's name | app chips appear while you type: tap one to open it |
| `remind 10m call back` | a reminder in 10 minutes |
| `remind 17:30 pay for parking` | a reminder at a time (`tomorrow 9:00` works too) |
| `timer 5m` | a timer |
| `note buy milk` | a note |

Below the box: your upcoming **reminders** and latest **notes**, each with a delete button on the
left. The voice assistant writes to the same lists ("remind me tomorrow at 7 to pay for parking",
"note: buy milk").

## Reminders that ring even when the phone sleeps

Reminders are fired by [alarmd](../../system/alarm), not by this window: it sets the RTC wake alarm,
so the phone wakes from sleep, chimes, vibrates, pulses the Glyph lights and reads the reminder
aloud.

## Notes

Notes are stored in [Iotas](https://gitlab.gnome.org/World/iotas) (category *notarealShell*), so you can
also read and edit them there. Without Iotas they're kept in a JSON file.

## D-Bus

`org.notarealinux.PhantomShell` on the session bus: `Summon()`, `Dismiss()`, `Toggle()`,
`ShowApps()` (the joystick's ALL slot), `Open(query)`, signal `SummonedChanged(b)`.
