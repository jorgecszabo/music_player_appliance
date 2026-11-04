# Alpine diskless audio appliance

Alpine Linux in diskless mode as a network/Bluetooth speaker. The root filesystem
lives in RAM and the boot media stays read-only, so the power can be cut at any
time. Board-specific parts live in `boards/<board>/`, and everything else is
board-independent.

See [PLAN.md](PLAN.md) for the design, decisions, hardware findings and status.

## Supported boards

| `BOARD=` | Board | SoC / Alpine arch | Kernel package | Audio out | Wi-Fi / Bluetooth |
|---|---|---|---|---|---|
| `pandaboard` (default) | PandaBoard rev A1 | OMAP4430, armv7 | `linux-pandaboard` | TWL6040 3.5 mm jack (96 kHz), or USB audio | onboard TI WL1271 (Wi-Fi doesn't connect yet), or USB |
| `beaglexm` | BeagleBoard-xM | DM3730 (OMAP3), armv7 | `linux-beaglexm` | TWL4030 3.5 mm jack (48 kHz), or USB audio | none onboard: USB dongles |

Both boot from an SD card through U-Boot v2024.07 (MLO + `u-boot.img`), with a
serial console on `ttyS2` at 115200 8N1. Each board's `board.conf` describes its
hardware in detail. To add a board, see [Porting](#porting-the-appliance-to-another-board).

## Build an image (Fedora host, rootless podman)

`BOARD=<name>` selects the board (default `pandaboard`). Every script reads it,
so set it once for the whole build:

```sh
export BOARD=beaglexm                            # or pandaboard

# Once per host (the container is per Alpine arch)
scripts/build-container.sh                       # target-arch Alpine build container
scripts/setup-keys.sh                            # abuild signing key (back up keys/*.rsa)

# Once per Alpine arch (armv7 is shared by both boards), and again when a fork changes
scripts/build-package.sh pipewire                # our headless forks (see below)
scripts/build-package.sh mpv
scripts/build-package.sh shairport-sync
scripts/build-soloist-runtime.sh                 # glibc runtime for Spotify Soloist

# Per board
scripts/build-u-boot.sh                          # needs gcc-arm-linux-gnu
scripts/build-package.sh linux-$BOARD            # kernel, cross-compiled (~10 min)
scripts/build-image.sh                           # -> build/sdimage/$BOARD/
sudo boards/$BOARD/write-sdcard.sh /dev/sdX build/sdimage/$BOARD   # erases the card
```

Packages go to `packages/main/<arch>/`; `build-image.sh` takes our packages from
there, and everything else from Alpine's mirrors. Without the Soloist runtime
the image still builds, but Spotify won't work. The image never contains the
Soloist binary or an API key: the device downloads Soloist from Spotify, and you
enter the key on the web page.

Then boot with the serial console, set a root password for the console
(`passwd`, `lbu commit`), and log in with `ssh panda@panda.local`. SSH accepts
the keys in `appliance/authorized_keys`, or this host's `~/.ssh/*.pub` if that
file is missing.

Wi-Fi out of the box: put `appliance/wifi.json` (not in git) before building,
or drop one onto the card's boot partition (imported at boot, then deleted):

```json
{"country": "DE", "networks": [{"ssid": "Home", "password": "secret123"}]}
```

Otherwise boot with Ethernet and pick a network on the web page (Network).

A board directory needs a
`board.conf` (including the U-Boot defconfig), an `mkimg.<name>.sh` profile
and a card writer. A board kernel is an aport with a `cross-build.sh` that calls
`scripts/cross-build-kernel.sh`; see `boards/pandaboard/aports/linux-pandaboard/`.

## Updates (OTA)

The card holds two versions (slots A and B, `slots/<x>/` on the boot partition:
kernel, initramfs, modules, the package repository). An update goes into the slot
that isn't running; the other one stays untouched. Design: PLAN.md, "OTA updates".

```sh
scripts/build-ota.sh            # rebuilds the image, -> build/ota/panda-$BOARD-<version>.pota
```

Install it on the web page (System Update: Browse..., Install, restart), or over SSH:

```sh
ssh panda@panda.local 'doas /etc/panda/web-admin.sh ota-install' < build/ota/panda-$BOARD-*.pota
ssh panda@panda.local 'doas /etc/panda/web-admin.sh ota-activate b'    # the slot it reported; reboots
```

The new version starts on trial: early in its boot the boot menu goes back to the old
slot, so a crash or a hang (watchdog) returns to the old version. Once the services and
the web page are up (after a minute), it is kept; unhealthy for 5 minutes, it reboots
into the old version and restores the settings from before the update. Bundles are signed
with the abuild key (`keys/`) and only install on the board they were built for.
`scripts/test-ota.sh` tests the device side on this host.

The code is the `panda-appliance` package (`aports/panda-appliance`, from
`appliance/rootfs`); the lbu overlay holds settings only. To try code without an
update: `scripts/deploy.sh [--restart]` (`PANDA_HOST=user@host` if it isn't
`panda@panda.local`) installs the package into the running system; the next boot runs
the slot's version again.

Recovery without the web page: the boot menu on the serial console lists both slots,
or edit `extlinux/extlinux.conf` (`DEFAULT a` / `DEFAULT b`) on a PC.

## Our forks of Alpine packages

`aports/` holds forks of Alpine packages: shairport-sync (native PipeWire
backend), and pipewire and mpv without the GPU stack (Alpine's builds pull in
mesa and llvm, ~170 MB of RAM on a diskless board). They win over Alpine's
builds (pkgrel 100) and setup.sh keeps `!mesa` and `!libcamera` in the world,
so upstream fixes only arrive when we rebuild. `scripts/check-forks.sh` lists
the forks and board kernels that are behind Alpine or kernel.org (exit 1).
To update a fork: take Alpine's new APKBUILD, reapply the changes listed in
the fork's header, set `_upstream_base`, keep `pkgrel=100`, and rebuild with
`scripts/build-package.sh <name>`.

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
   Needs: the audio codec, `SND_USB_AUDIO` (USB audio output; the kernel APKBUILDs check it),
   the watchdog, the BT controller (or `btusb`), gpio-keys for the
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
