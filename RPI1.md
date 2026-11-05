# Raspberry Pi 1 Model B + 2.8" TFT port: plan

Port the appliance to an original Raspberry Pi Model B (rev 2, 512 MB): a BCM2835 with one
ARM1176JZF-S core (**ARMv6**, VFPv2, **no NEON**), overclocked from 700 MHz to 1 GHz. It has
10/100 Ethernet and 2 USB ports, both behind the same USB 2.0 controller (`dwc_otg`). Audio
goes to the onboard 3.5 mm jack (PWM) or a USB DAC, chosen on the web page. Ethernet only for
now. Bluetooth needs a USB dongle, as on the xM and the pcDuino.

On top of it sits an Adafruit 2.8" 320x240 TFT (ILI9341 display and STMPE610 resistive
touch, both on SPI0, plus 4 tactile buttons). It gets a native front-panel UI in the web
page's Win98 style. Mockup: https://claude.ai/artifact/9eBr1H8cdibYzhANuQigxy

Decided (2026-11-05):
- Board name `rpi1`. Alpine arch **`armhf`** (ARMv6 hard-float), a new arch for this repo.
- **Spotify becomes optional in board.conf**: on by default, off for `rpi1` (Soloist and its
  Debian armhf runtime are ARMv7).
- Overclock to 1 GHz with the firmware's "Turbo" preset.
- The spectrum FFT is low precision (fixed-point int16, in C). There's no NEON on this core.
- Bluetooth pairing from the panel's Start menu only. The 4 buttons are media keys and
  nothing else, so `BUTTON_INPUT_NAME`/`BUTTON_KEY` stay empty. That also means no
  factory reset by button on this board: factory reset is on the web page, or rewrite the card.
- The onboard jack is offered as an output (`AUDIO_CARD` set), with USB audio as the
  alternative.
- No Wi-Fi dongle for now: Ethernet only, no `r92su-rpi` aport.
- Fonts as in the mockup: Tahoma for the UI and VT323 for the green display (task 6).

## What upstream already has (checked 2026-11-05)

Alpine v3.24 `armhf` on the mirror has everything the appliance needs except our forks:
- `linux-rpi` 6.18.60: the Raspberry Pi downstream kernel, the same version as our own
  kernels. It ships the `pitft28-resistive` and `gpio-key` overlays, the `dwc_otg` FIQ fix
  for USB audio, `snd_bcm2835` for the jack and the cpufreq and thermal drivers.
- `raspberrypi-bootloader` (GPU firmware: `bootcode.bin`, `start*.elf`, `fixup*.dat`).
- `pipewire` 1.6.8, `wireplumber`, `shairport-sync` 5.5.1, `mpv`, `py3-pillow`, `kissfft`.
- Alpine's own Raspberry Pi images are diskless with lbu on the FAT partition, the same
  model as ours. A stock image is enough for the spikes (below).

**Kernel: Alpine's `linux-rpi`, not our own.** It already has the drivers, overlays and the USB
fix that mainline lacks for this board, and PLAN.md's portability note expected a Pi to use
the stock kernel. The cost comes later: a Wi-Fi dongle that needs r92su would need its own
small aport built against `linux-rpi-dev`, rebuilt whenever `linux-rpi` changes. If the spikes show
the downstream kernel is a problem, fall back to our own `linux-rpi1` on mainline 6.18.60
(`bcm2835_defconfig` plus a device tree with the TFT nodes).

Values from the downstream device trees and drivers (all **unverified** until B1–B6):

| board.conf | value | source |
|---|---|---|
| `ALPINE_ARCH` | `armhf` | ARMv6 |
| `KERNEL_PACKAGE` / `KERNEL_FLAVOR` | `linux-rpi` / `rpi` | Alpine |
| `DTB` | `bcm2708-rpi-b.dtb` | downstream name for the B rev 2 |
| `CONSOLE` | `ttyAMA0,115200n8` | PL011 on GPIO 14/15 |
| `BOOT_MEDIA` | `mmcblk0p1` | |
| `UBOOT_DEFCONFIG` / `UBOOT_FILES` | `rpi_defconfig` / `u-boot.bin` | v2024.07 |
| `WATCHDOG_DEV` | `/dev/watchdog0` | `bcm2835_wdt` |
| `THERMAL_ZONE_TYPE` | `cpu-thermal` | `bcm2835_thermal` |
| `LED_STATUS` | `ACT` | the only CPU-controlled LED on the B |
| `AUDIO_CARD` / `AUDIO_DEVICE` | `Headphones` / `hw:Headphones,0` | `snd_bcm2835`, the jack |
| `AUDIO_HW_VOLUME_CONTROL` | `PCM` (range and step from `amixer`, check) | `snd_bcm2835` |
| `AUDIO_PW_SINK` | from `wpctl status` on the board | |
| `BUTTON_INPUT_NAME` / `BUTTON_KEY` | empty | pairing is in the Start menu |
| `WIFI_*` | empty | Ethernet only for now |
| `SPOTIFY` | `no` | new variable (task 1) |
| `LCD_FB` | `/dev/fb1` | fbtft `fb_ili9341` (fb0 is HDMI) |
| `LCD_TOUCH_NAME` | `stmpe-ts` | evdev name |
| `LCD_KEYS_NAME` | `gpio-keys` (one device per overlay instance, check) | evdev name |
| `LCD_BACKLIGHT` | `soc:backlight` | STMPE GPIO 2 |

TFT wiring as Adafruit documents it for the 26-pin version: LCD on SPI0 CE0, touch on CE1,
D/C on GPIO 25, touch IRQ on GPIO 24, buttons on GPIO 23, 22, 27 (21 on a rev 1) and 18.

## Spikes first (need the board, no repo changes)

These can change the plan, so they run before the unattended work. Use a stock Alpine armhf
Raspberry Pi image on a **spare** card, with no TFT for S1 and S2.

- **S1 Overclock.** `config.txt`: `arm_freq=1000`, `core_freq=500`, `sdram_freq=600`,
  `over_voltage=6`, no `force_turbo` (`force_turbo` with overvolting sets the warranty bit,
  and dynamic clocking keeps idle at 700 MHz). Run all cores busy (`stress` or a `yes` loop)
  for 2 hours. Watch the temperature, `scaling_cur_freq` and the firmware's throttle flags.
  The Turbo preset is known to corrupt SD cards on some boards because of `core_freq=500`.
  Our card is read-only except for lbu commits, which limits the damage, but still run 50 `lbu
  commit`s under load and fsck the card afterwards. If that fails, fall back to the "High"
  preset (950/450/450/6).
- **S2 Audio CPU and USB.** shairport-sync on a USB DAC with an AirPlay stream while Ethernet
  carries traffic (iperf). Measure CPU use and count xruns. Our shairport-sync is built with
  soxr, which may be too heavy for the ARM1176: try `interpolation = "basic"` too. Repeat for
  A2DP (SBC decode) and for radio (mpv).
- **S3 TFT.** Add `dtoverlay=pitft28-resistive,rotate=90,speed=32000000,fps=25` plus one
  `dtoverlay=gpio-key,...` per button. Check `/dev/fb1` (`cat /dev/urandom > /dev/fb1`),
  `evtest` on the touch and the buttons, and the backlight on/off.

Result: S1 sets the clocks, S2 sets the audio settings and how much CPU is left for the UI,
and S3 confirms the names in the table. If S2 leaves less than about 25% of the CPU idle while
AirPlay plays, the spectrum is cut from v1.

## Unattended tasks, in order

### 1. Spotify as an optional service (all boards)

`SPOTIFY=yes|no` in board.conf, `yes` when unset, so the existing boards don't change.
With `no`:
- `appliance/genapkovl.sh` and `setup.sh`: don't add or enable `speaker-soloist`.
- `scripts/build-image.sh`: skip the Soloist runtime with no warning; the OTA bundle doesn't
  carry it.
- `soloist-update.sh due`: the cron job exits at once (or isn't installed).
- `panda-arbiter.py`: no Spotify metadata loop or Spotify pause.
- `panda-web.py`: `/api/spotify-key` answers 404 and `/api/status` reports `spotify: false`.
  `index.html`/`app.js`: hide the Spotify Connect fieldset and drop "Spotify" from the
  "Pick Panda in ..." text.
- `web-admin.sh`: refuse the soloist commands.
- Check on an armv7 board that an image built with `SPOTIFY` unset matches today's.

### 2. The armhf build arch

- `scripts/env.sh`: `armhf) CONTAINER_PLATFORM=linux/arm/v6 ;;`. Check every
  `case "$ALPINE_ARCH"` (`build-soloist-runtime.sh` can refuse armhf now).
- `build-container.sh` for armhf; rebuild `pipewire`, `mpv` and `shairport-sync` there. Under
  QEMU this takes a while, which is fine unattended. PipeWire picks SIMD at runtime, so
  the C fallbacks run on ARMv6 by themselves. Check the build flags don't add `-mfpu=neon`.
- `check-forks.sh` for armhf.
- No r92su module for now (Ethernet only). If a dongle that needs it comes later: an
  `r92su-rpi` aport against `linux-rpi-dev`, with `depends="linux-rpi=<ver>"`.

### 3. Board directory: `boards/rpi1/`

- `board.conf` from the table above, with "unverified" markers, as the pcDuino's has.
- `mkimg.rpi1.sh`: the profile; `raspberrypi-bootloader` and `linux-rpi`.
- `config.txt` (installed on the FAT partition): `kernel=u-boot.bin`, the overclock from S1,
  `gpu_mem=16` with `start_cd.elf`/`fixup_cd.dat` (the screen is SPI, so the GPU barely needs
  memory), `enable_uart=1`, `dtparam=spi=on`.
- `rpi1-tft.dts`: our own overlay, compiled by the build. It is `pitft28-resistive` with
  rotation, speed and fps fixed (FDTOVERLAYS can't pass parameters), plus the 4 buttons as one
  `gpio-keys` node: `KEY_PLAYPAUSE`, `KEY_NEXTSONG`, `KEY_VOLUMEDOWN`, `KEY_VOLUMEUP`. The
  screen is part of this board, so it belongs in the board's device tree (no runtime
  detection).
- U-Boot: `rpi_defconfig` plus a fragment for `CONFIG_OF_LIBFDT_OVERLAY` if it's missing.
  `scripts/build-u-boot.sh` needs an ARMv6 target (`gcc-arm-linux-gnu` handles it).
- `write-sdcard.sh`: MBR with one FAT32 partition (`0x0c`), no raw offsets. Firmware
  files, `config.txt`, `u-boot.bin`, then the same slot layout as the other boards.

### 4. Image + OTA

- extlinux labels: `FDT bcm2708-rpi-b.dtb` and `FDTOVERLAYS rpi1-tft.dtbo` from the slot's
  `boot/`, so the device tree changes with the slot. `panda-ota.py` already parses both keys;
  check it rewrites the overlay path per slot.
- The GPU firmware and `config.txt` stay in the FAT root, "unchanged" as MLO is on the OMAP
  boards. Overclock changes don't go through OTA.
- Check that U-Boot keeps the firmware's fixups (memory size, Ethernet MAC) when it loads its
  own DTB. U-Boot's `rpi` board code fixes the memory node and sets `ethaddr` from the
  mailbox. If the MAC comes up random, set `ethaddr` in the boot script.

### 5. Transport controls: `/api/transport` (all boards)

The panel needs play/pause/next/previous for whichever source is playing. The web page will
use the same calls.
- `panda-web.py` `POST /api/transport {"action": "toggle|next|prev"}` → the arbiter picks the
  current source.
- Radio: mpv IPC (`cycle pause`, the next or previous preset). The socket helper already exists.
- Bluetooth: AVRCP `Play`/`Pause`/`Next`/`Previous` on `org.bluez.MediaPlayer1`, next to the
  existing `bt_pause`.
- AirPlay: the sender owns playback. shairport-sync can send DACP commands only with
  `--with-dbus-interface`, which means a fork rebuild on every arch. For v1, AirPlay supports
  pause only (the arbiter's existing stop), and next/prev are greyed out on the panel.
- Volume: the existing `/api/volume`.

### 6. Front panel UI: `panda-lcd` (Python + Pillow)

Starts only if `LCD_FB` is set in board.conf. Everything else is generic.
- **Display.** `mmap` the framebuffer (RGB565). Draw into a Pillow image and copy only the
  changed rectangles. fbtft's deferred I/O sends only the touched lines over SPI, so a
  small redraw stays cheap. Budget: clock once a minute, elapsed time once a second, marquee at
  4 fps, nothing while idle.
- **Widgets.** Win98 primitives in one module: raised/sunken bevels, window with title bar,
  button, slider, listbox, radio button, status bar, taskbar, menu. The palette is the same
  as `web/style.css`.
- **Fonts, as in the mockup.** UI: Tahoma at 11 px. The mockup rendered Microsoft's Tahoma
  from the workstation's msttcore fonts, which can't go into the image. Ship **Wine's
  Tahoma** instead (LGPL 2.1+). It's drawn to match Tahoma's metrics and look; build it from
  Wine's `fonts/tahoma.sfd` (and `tahomabd.sfd`) with fontforge on the host and ship the
  license text with it. Green display: **VT323** (OFL), as in the mockup. Render both with
  Pillow `fontmode = "1"` (no antialiasing), which is crisper on a 320x240 panel and is how
  Win9x drew text. Both TTFs ship in `panda-appliance` under `/usr/share/panda/fonts/`.
- **Bluetooth pairing.** Start → Bluetooth → "Pair a device…" opens the existing pairing
  window (`/api/bt`). The panel shows the pairing dialog while it's open, then a countdown.
- **Data.** `/api/events` (the web page's event stream) for now playing, volume and source;
  `/api/status` for Wi-Fi, Bluetooth and the service states. Both come from 127.0.0.1, so the
  panel and the web page always show the same thing.
- **Input.** The service grabs the gpio-keys device (EVIOCGRAB), so nothing else acts on a
  wake press. The touch device is not grabbed. Calibration: tap 3 crosshairs, fit an affine
  transform, save it to `/etc/panda/lcd-cal.json`, `lbu commit`.
- **Screens** (as in the mockup): Now Playing, Start menu, Radio presets, Network (QR for
  `http://panda.local/`), Bluetooth pairing, Wi-Fi lost, Shut Down, Display settings,
  screensaver, crash. The QR code comes from `py3-qrcode` if Alpine has it for armhf;
  otherwise use a vendored encoder, since the URL is fixed.
- **Boot and shutdown.** fbcon on fb1 (`fbcon=map:10` on the kernel command line) shows the
  kernel log as the "POST" screen. `panda-lcd` takes over when it starts. Its `stop()` draws
  "It's now safe to turn off your speaker" and leaves the backlight on. The framebuffer keeps
  the last frame after the process exits, so the message stays up after poweroff.
- **Idle.** Screensaver after 2 minutes, backlight off after 10. Wake on any input. Pause
  everything except the clock while the screen is off.
- **Crash screen.** When `/api/status` shows a supervised service restarting, show it for 5
  seconds, then go back to Now Playing.
- Runs as its own user, in groups `video` and `input`, and `nice 10`. It never shows a
  process list (the Soloist key lesson applies even without Spotify).

### 7. Spectrum: `panda-spectrum` (C)

- Input: `pw-cat --record` from the output sink's monitor, S16, native rate. The C program
  downmixes to mono and decimates by 2 (integer averaging) to keep the C side small and
  avoid a PipeWire resampler.
- FFT: vendored `kiss_fft.c` built with `FIXED_POINT=16`, 256 points, Hann window from a
  precomputed int16 table, about 8 frames a second. Output: 24 log-spaced bands, one byte
  each, over a Unix socket to `panda-lcd`.
- Build: `-O2 -march=armv6kz -mfpu=vfp -mfloat-abi=hard`. The packaging check fails if
  `objdump -d` shows float instructions (`vmul.f32`, `vadd.f32`, ...) in the FFT. Optional:
  the ARMv6 dual-16-bit instructions (`__smlad` from `arm_acle.h`) in the butterfly, but only
  if a measurement shows it matters.
- Started by `panda-lcd` only while Now Playing is visible and something plays. A kill switch
  stops it and shows the bars flat if the pw-top xrun count goes up while it runs.
- Budget: under 1% of the CPU for the FFT. Measure `pw-cat` plus the program together; that
  pair is the real cost.

### 8. Docs

README's board table and build notes, PLAN.md "Still to do", and this file's log.

## Needs the owner and the board

- B1: S1–S3 (above).
- B2: boot the built image: serial console first, with the TFT off (the TFT covers the
  26-pin header). Then Ethernet, mDNS, web page, watchdog, temperature.
- B3: AirPlay, Bluetooth dongle + A2DP, and radio on the jack and then on a USB DAC. Count the
  xruns. On the jack: fill in the `PCM` range and dB step, set a volume cap that keeps the
  PWM from clipping, and check the switch between outputs on the web page.
- B4: TFT on: panel screens, touch calibration, the 4 buttons, backlight, poweroff screen.
- B5: spectrum on while AirPlay plays: CPU and xruns compared with B3.
- B6: OTA update and rollback on this board.

## Open questions

None open. Answered 2026-11-05: pairing from the Start menu only, offer the onboard jack, no
Wi-Fi dongle for now, fonts as in the mockup (see "Decided").

To check while building: whether panda-bt, `panda-reset-button` and the web page all cope
with an empty `BUTTON_INPUT_NAME` (no button at all).

## Log

- 2026-11-05: plan written; mirror check of Alpine v3.24 armhf (`linux-rpi` 6.18.60,
  `raspberrypi-bootloader`, PipeWire 1.6.8, shairport-sync 5.5.1, Pillow, kissfft).
- 2026-11-05: open questions answered. The mockup's UI font was Microsoft Tahoma
  (msttcore on the workstation, not redistributable), so the device ships Wine's Tahoma.
