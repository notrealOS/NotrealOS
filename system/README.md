# System

Background daemons that make the phone usable day to day. Like the shell, each folder expects to
live in your home folder under its original name.

| Repo folder | Install as | What it does |
| --- | --- | --- |
| [`voice/`](voice) | `~/voice` | **Voice assistant.** Hold volume up and speak: speech to text (PC whisper, phone fallback), an LLM picks the action (PC Qwen 35B, phone fallback Qwen3 1.7B), then alarms, timers, reminders, notes, calls, messages, open apps, web search, or a spoken answer (Kokoro on the PC, piper on the phone). Models are not included: see below |
| [`alarm/`](alarm) | `~/alarm` | Alarms, timers and reminders that wake the phone from sleep with the RTC, ring on the speaker, pulse the Glyph lights and read the reminder aloud. `install.sh` (root) lets your user set the RTC alarm |
| [`sleepd/`](sleepd) | `~/sleepd` | Suspends 60 s after the screen goes off; respects calls, music, navigation and other sleep inhibitors |
| [`doctor/`](doctor) | `~/doctor` | **nrdoctor**: ~40 health checks every 5 minutes (daemons, audio, USB-C/DAC, modem, PC link, battery, RAM). `--auto` repairs what it can, with a crash-loop guard. Drives the face on the overview widget |
| [`backup/`](backup) | `~/backup` | Nightly `restic` backup over sftp to the PC (only on charger + Wi-Fi, never fills the PC's system disk). `install.sh` (root) adds the system-files snapshot |
| [`haptics/`](haptics) | `~/haptics` | Kernel module for the PM8350B PMIC vibration motor (missing upstream) + a feedbackd theme |
| [`ka5idle/`](ka5idle) | `~/ka5idle` | Lets a FiiO KA5 USB DAC sleep when no music is playing (saves 60–150 mA) |
| [`glyph/`](glyph) | `~/glyph` | The Glyph lights on the back: `glyph.sh` drives the AW21018 LED chip; `glyphd` is a D-Bus service for notifications, torch and ambient light |

## Configuration

- `~/.config/notreal/pc.json`: where the PC is (`host`, `matrix`, `ssh`, `backup_repo`, and
  `android` if you mirror apps from an Android phone). See [docs/pc-link.md](../docs/pc-link.md).
- Root install scripts (`install.sh`) are run with `sudo` and set things up for the user who ran
  `sudo` (`$SUDO_USER`).

## Models for the voice assistant (phone fallback)

Not in the repo (several GB). Put them in `voice/models/`:

- `ggml-base.bin` (whisper.cpp): speech to text
- `qwen3-1.7b-q4_k_m.gguf`: the on-phone LLM for llama.cpp
- `en_US-amy-medium.onnx` + `.onnx.json` (piper): the on-phone voice

## Kernel modules

`haptics/pm8350b-haptics.c` builds against the running kernel (`make`, needs its headers). The Glyph
LED driver (`leds-aw21018`) and the other Nothing Phone (1) kernel work are in
[`spacewar-fixes/`](../spacewar-fixes).
