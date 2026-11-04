# Alpine diskless audio appliance

Alpine Linux in diskless mode as a network/Bluetooth speaker. The root filesystem
lives in RAM and the boot media stays read-only, so the power can be cut at any
time. The first board is a PandaBoard rev A1 (OMAP4430). Board-specific parts
live in `boards/<board>/`, and everything else is board-independent.

See [PLAN.md](PLAN.md) for the design, decisions, hardware findings and status.

## Build (Fedora host, rootless podman)

```sh
scripts/build-container.sh                       # target-arch Alpine build container
scripts/setup-keys.sh                            # abuild signing key (once; back up keys/*.rsa)
boards/pandaboard/u-boot/build.sh                # U-Boot v2024.07 (needs gcc-arm-linux-gnu)
scripts/build-package.sh linux-pandaboard        # kernel, cross-compiled (~10 min)
scripts/build-image.sh                           # diskless image into build/sdimage/
sudo boards/pandaboard/write-sdcard.sh /dev/sdX build/sdimage   # erases the card
```

Then boot with the serial console (115200 8N1), set a root password for the
console (`passwd`, `lbu commit`), and log in with `ssh panda@panda.local`.

Wi-Fi out of the box: put `appliance/wifi.json` (not in git) before building,
or drop one onto the card's boot partition (imported at boot, then deleted):

```json
{"country": "DE", "networks": [{"ssid": "Home", "password": "secret123"}]}
```

Otherwise boot with Ethernet and pick a network on the web page (Network).

Other boards: `BOARD=<name>` selects `boards/<name>/`. That directory needs a
`board.conf`, an `mkimg.<name>.sh` profile and a card writer.

## Debugging

`scripts/serial-sysrq.py [keys]` sends Magic SysRq over the serial console and
saves the output to `build/serial-sysrq.log` (`w` blocked tasks, `l` CPUs,
`t` all tasks). Quit picocom first, and don't run it with sudo.

## Porting the appliance to another board

Everything board-specific lives in `boards/<board>/`. The appliance itself
(`appliance/`: services, setup, web page) reads every hardware detail from that
board's `board.conf`, installed on the device as `/etc/panda/board.conf`.

| Generic (`appliance/`, `aports/`, `scripts/`) | Per board (`boards/<board>/`) |
|---|---|
| services, setup.sh, gen-configs.sh, web page, chimes, firewall, reset | `board.conf`: arch, kernel, DTB, console, boot media, audio card/rate/quirks, volume cap control, BT controller, button, LEDs, watchdog, thermal zone |
| PipeWire/WirePlumber/bluez/shairport configs (rendered from board.conf) | `mkimg.<board>.sh`: mkimage profile (bootloader config, kernel cmdline) |
| our packages (`aports/shairport-sync`) | `write-sdcard.sh`: partitioning + bootloader install for the board's ROM |
| build scripts (container platform from `ALPINE_ARCH`) | `aports/linux-<board>/` (optional): own kernel + config fragment; `u-boot/` (optional) |

Steps:

1. `cp -r boards/_template boards/<board>` and rename `mkimg.TEMPLATE.sh` to
   `mkimg.<board>.sh` (and its functions `TEMPLATE_*` to `<board>_*`).
2. Fill in `board.conf`; every variable is documented there. Optional ones fall back to
   generic defaults (44.1/48 kHz, quantum 1024, no codec quirks, dual-mode BT, 60 s watchdog).
3. Check the rendered configs on the host, without hardware:
   `mkdir -p /tmp/c && cp boards/<board>/board.conf appliance/appliance.conf /tmp/c/ &&
   CONF_DIR=/tmp/c ROOT=/tmp/out sh appliance/rootfs/etc/panda/gen-configs.sh`
4. Kernel: use an Alpine flavor if it supports the board, or add `aports/linux-<board>`.
   Needs: the audio codec, the watchdog, the BT controller (or `btusb`), gpio-keys for the
   button, `nf_tables`, and a fast entropy source (getrandom must not block at boot).
5. Write `write-sdcard.sh` for the board's boot ROM, then
   `BOARD=<board> scripts/build-package.sh <kernel> && BOARD=<board> scripts/build-image.sh`.
6. On the device, find the real names and put them in board.conf: `cat /proc/asound/cards`
   (AUDIO_CARD), `wpctl status` (AUDIO_PW_SINK), `amixer` (AUDIO_HW_VOLUME_*),
   `cat /proc/bus/input/devices` (BUTTON_*), `ls /sys/class/leds /sys/class/watchdog`.
7. Test checklist: boot to SSH; power-cut (pull the plug, settings survive); audio
   (a quiet tone, then music: no crackle, `pw-top` shows 0 errors); AirPlay; Spotify;
   Bluetooth pairing via the button + reconnect after reboot; radio; web page; watchdog
   (`kill -STOP` the busybox watchdog: the board resets); factory reset dry run.
