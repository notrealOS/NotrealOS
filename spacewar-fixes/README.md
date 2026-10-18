# Nothing Phone (1) fixes

Hardware work for the Nothing Phone (1) (`nothing-spacewar`, SM7325 / Snapdragon 778G+) on
postmarketOS with the `linux-postmarketos-qcom-sc7280` 7.2.9 kernel.

> **Read before flashing.** Test every boot image with `fastboot boot <img>` (RAM only) before
> `fastboot flash`. Keep the stock `boot` image so you can go back. Set
> `deviceinfo_flash_kernel_on_update="false"` in `/etc/deviceinfo`, or the next kernel or
> initramfs update rebuilds `boot.img` with the stock device tree and silently undoes these fixes.

## What works with these fixes

| Hardware | Status | Where |
| --- | --- | --- |
| Main camera (Sony IMX766, 50 MP, C-PHY) | Works: 4:3 photos at 12 MP, 30 fps preview, autofocus | `kernel/drivers/imx766`, `kernel/0001`, `libcamera/0014`, `camera/` |
| Ultra-wide (Samsung S5KJN1) | Works, correct exposure and colour | `kernel/0001` (24 MHz / 700 MHz), `libcamera/`, `camera/tuning` |
| Front camera (Sony IMX471) | Works, correct colours | `kernel/0002`, `camera/tuning/imx471.yaml` |
| Autofocus (DW9800 / AK7377 VCMs) | Works (tap to focus in notreal.cam) | `camera/autofocus` |
| Speaker | Works (no more robotic sound: PulseAudio forced to s16le) | `audio/` |
| Main microphone | Works | `kernel/0001` (mic bias), `audio/HiFi.conf` |
| Bottom microphone | **Not working** (ADC3 gives zeros) | |
| Voice calls (earpiece, speaker, mic) | Works | `kernel/0001` (q6voice), `audio/VoiceCall.conf`, `calls/` |
| USB-C host (DACs, keyboards) | Works | `kernel/0001` (L2B at 3.072 V) |
| Glyph lights | Works (18 channels, zones, D-Bus service in `system/glyph`) | `kernel/drivers/leds-aw21018`, `kernel/0001` |
| Vibration (PM8350B haptics) | Works | [`system/haptics`](../system/haptics) |
| Suspend | Works in `s2idle` only (deep sleep resets the SoC); ~235 mA asleep, still too high: the ADSP never sleeps | `sleep/` |
| Plugging USB-C in while asleep | Used to reboot the phone; USB wake is now disabled | `sleep/s2idle.start`, `kernel/experimental` |

## Kernel

```sh
# in the pmaports kernel source (linux-postmarketos-qcom-sc7280 7.2.9)
git apply kernel/0001-arm64-dts-qcom-sm7325-nothing-spacewar-notrealOS.patch
git apply kernel/0002-media-i2c-imx471-fix-Bayer-order.patch
```

`0001` is every device-tree change in one patch, against the stock
`sm7325-nothing-spacewar.dts`. Both patches were checked to reproduce exactly the sources the phone
runs.

Out-of-tree drivers (build against the same kernel, `make -C <kernel> M=$PWD modules`):

- `kernel/drivers/imx766`: Sony IMX766 main camera. Register tables from the vendor sensor module,
  structure from the mainline IMX800 driver (Luca Weiss).
- `kernel/drivers/leds-aw21018`: Awinic AW21018 Glyph LED controller, init sequence from
  Nothing's downstream driver.

Install them to `/lib/modules/$(uname -r)/extra/` and run `depmod`.

`kernel/experimental/` holds a USB-C/dwc3 suspend fix that is **not working yet**: images with it
did not boot. It's there for reference only.

### Boot image

Build the DTB, append it to the kernel image and repack with `mkbootimg`, reusing the arguments of
your current `boot.img` (`unpack_bootimg` shows them). Then:

```sh
fastboot boot boot-notreal.img          # test
fastboot flash boot boot-notreal.img    # only once everything works
```

## libcamera

Patches `0007`–`0015` go on top of the pmaports `libcamera` package (which already carries
`0001`–`0006`). Add them to the APKBUILD's `source=` and build with
`pmbootstrap build libcamera --arch aarch64`. They fix:

- cyan highlights under warm light (AWB gains kept ≥ 1)
- blown-out photos: exposure target, faster convergence, highlight protection, settling
- metering that only looked at the top-left corner in 1080p modes
- an exposure-compensation file (`$XDG_RUNTIME_DIR/camera-ev`) for the EV slider in notreal.cam
- the IMX766 sensor helper

After installing a new libcamera, restart WirePlumber: it keeps the old library loaded.

Colour tuning goes in `/etc/libcamera/ipa/simple/`, which libcamera reads before `/usr/share`,
so it survives updates:

```sh
sudo install -D -m 644 camera/tuning/*.yaml -t /etc/libcamera/ipa/simple/
```

## Autofocus

`camera/autofocus/afd.py` powers the focus motors and holds the lens at the position written to
`~/.config/af-position` (notreal.cam writes it for tap to focus).
`sudo camera/autofocus/install.sh` installs the OpenRC service.

The installer also saves your camera module's EEPROM (factory calibration) next to the script. It
is unique to your phone: don't publish it.

## Audio and calls

```sh
sudo install -m 644 audio/HiFi.conf audio/NP1.conf audio/VoiceCall.conf /usr/share/alsa/ucm2/Nothing/spacewar/
sudo install -D -m 644 audio/q6voiced.conf /usr/share/q6voiced/q6voiced.conf
install -D -m 644 audio/pulse-daemon.conf ~/.config/pulse/daemon.conf    # s16le: the DSP garbles S24_LE
sudo calls/install.sh                                                    # earpiece/speaker routing service
```

`calls/callmode-user` runs in your session (autostart) and keeps the voice mixers on during calls.
An `alsa-ucm-conf-qcom-sc7280` update overwrites the UCM files: install them again afterwards.

## Sleep

```sh
sudo install -m 755 sleep/s2idle.start /etc/local.d/ && sudo rc-update add local default
sudo install -D -m 644 sleep/20-s2idle.conf /etc/elogind/sleep.conf.d/20-s2idle.conf
```

- forces `s2idle` (deep sleep resets the phone)
- disables the Bluetooth UART wake line (it bounced every suspend back in 0.5 s)
- disables USB wake (a USB-C device plugged in during sleep reset the phone)

`sleep/sleepstats.sh` measures the drain and which subsystems slept: run it, lock the phone for
10 minutes, run it again.

## Misc

- `misc/glyph.start`: lets the `feedbackd` group drive the Glyph LEDs
- `misc/70-uinput-input.rules`: lets the `input` group create virtual input devices (gestures,
  the power-key handling in `shell/crt`, the keyboard)
