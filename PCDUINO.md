# pcDuino1 Lite port: plan

Port the appliance to the LinkSprite pcDuino Lite: an Allwinner A10 (one Cortex-A8 core at
1 GHz, armv7), 512 MB, microSD only (no NAND/eMMC), 10/100 Ethernet, 2 USB host ports plus
USB OTG. It has **no usable onboard audio**: no 3.5 mm jack, and mainline's A10 HDMI driver
can't carry sound. So it plays through a USB DAC only. No Wi-Fi or Bluetooth onboard: USB
dongles, as on the xM.

This plan splits the work into what runs unattended (no board, no card, no owner) and what
needs the owner and the hardware. Unattended, everything up to a bootable image gets built
and checked as far as possible without the board, including a QEMU smoke boot.

## What upstream already has (checked in `build/src`)

- U-Boot v2024.07 (our tree): `Linksprite_pcDuino_defconfig`, which builds
  `u-boot-sunxi-with-spl.bin`. The first-stage loader (SPL) detects the DRAM size by
  itself, so 512 MB needs no patch. Standard distro boot reads `extlinux/extlinux.conf`
  from mmc0, and the OTA slot switching relies on that.
- Linux 6.18: `allwinner/sun4i-a10-pcduino.dts`. There is no Lite device tree; linux-sunxi
  says the pcDuino one works for it (same 125x52 mm board, 512 MB instead of 1 GB, no NAND).
  `sunxi_defconfig` covers the Ethernet, SD, USB OTG, watchdog and temperature sensor drivers.

Names worked out from the device tree and drivers (no board needed):

| board.conf | value | source |
|---|---|---|
| `CONSOLE` | `ttyS0,115200n8` | `uart0` = `serial0`, the only UART enabled |
| `BOOT_MEDIA` | `mmcblk0p1` | only `mmc0` is enabled |
| `DTB` | `sun4i-a10-pcduino.dtb` | upstream (Debian's installer lists the same file) |
| `WATCHDOG_DEV` | `/dev/watchdog0` | `sunxi_wdt`, the only watchdog (the AXP209 driver doesn't register one) |
| `THERMAL_ZONE_TYPE` | `cpu-thermal` | `sun4i-a10.dtsi`, sensor = touchscreen block (`TOUCHSCREEN_SUN4I`) |
| `LED_STATUS` / `LED_BT` | `pcduino:green:tx` / `pcduino:green:rx` | LED labels |
| `BUTTON_INPUT_NAME` | `gpio-keys` | the node name, used as the input device name (hyphen, unlike the Panda) |
| `BUTTON_KEY` | `KEY_HOME` | Back/Home/Menu keys in the device tree; **the Lite's buttons are unconfirmed** |
| `AUDIO_CARD` / `AUDIO_PW_SINK` | empty | no onboard output: the first USB card |

## Ground rules for the unattended part

- No SD card writes, no `sudo`, no Fedora package or config changes. Throwaway podman
  containers are fine (QEMU runs in one).
- The PandaBoard and xM builds must stay byte-for-byte the same: every generic change gets
  checked by rebuilding the `beaglexm` image and comparing it with the current build (and
  `scripts/test-ota.sh` still passes).
- One commit per task, straight to main. PLAN.md's board table and README get updated at
  the end.
- Results are logged at the end of this file.

## Unattended tasks, in order

### 1. Board directory: `boards/pcduino/`

- `board.conf` from the template, with the values above. `ALPINE_ARCH=armv7`,
  `KERNEL_FLAVOR=pcduino`, `BOOT_LABEL=PCDUINOBOOT` (11 characters: the FAT maximum),
  `UBOOT_TAG=v2024.07`, `UBOOT_DEFCONFIG=Linksprite_pcDuino_defconfig`,
  `UBOOT_FILES=u-boot-sunxi-with-spl.bin`, `BOARD_BADGE="A10"`. Values not checked on the
  board yet are marked "unverified", as in the xM's file.
- **Button: off in the first image** (`BUTTON_*` empty). The device tree declares the keys
  active-low without a pull-up. If the Lite doesn't have the buttons, a floating line can read
  as held at power-on, and on this appliance that means **a factory reset**. They get turned
  on in task B2, after checking them on the board.
- `mkimg.pcduino.sh`: the Panda's profile with the board name changed (`FDT` from `DTB`).
- `panda-appliance`'s APKBUILD picks up the new board as `panda-board-pcduino` on its own.

Check: `gen-configs.sh` renders on the host (README "Porting", step 3), and the USB-only
audio config looks right (no onboard sink, the first USB card used).

### 2. Kernel aport: `boards/pcduino/aports/linux-pcduino/`

- Same structure as `linux-beaglexm`: `_defconfig=omap2plus_defconfig` (as the generic
  baseline, platform swapped to the A10: see the log), `_fragment=pcduino.config`,
  `_dtbs="allwinner/sun4i-a10-pcduino.dtb"`, and `_kconfig_check="CONFIG_MACH_SUN4I=y
  CONFIG_SND_USB_AUDIO=[ym]"`.
- Fragment: the A10 platform and drivers, plus what the appliance needs, taken from the
  xM fragment's generic part: `SND_USB_AUDIO`, Bluetooth + `btusb` (+ BT USB audio), USB Wi-Fi
  drivers as modules, `nf_tables`, squashfs/loop for the modloop, `SUNXI_WATCHDOG`,
  `TOUCHSCREEN_SUN4I`, `USB_MUSB_SUNXI` (OTG port as a third host port), cpufreq
  (the AXP209's `dcdc2` and `cpufreq-dt`). Switch off what a speaker doesn't need, as the xM
  does (display, camera, video decoder).
- Build with `BOARD=pcduino scripts/build-package.sh linux-pcduino`.

Check: the package builds, the config check passes, the DTB is in the package, and
`modinfo` lists `sunxi_wdt`, `sun4i-ts`, `snd-usb-audio` and `btusb` (built in or as modules).

### 3. U-Boot

- `BOARD=pcduino scripts/build-u-boot.sh`. It should work unchanged (the source tree is shared
  and reset per board).
- Check that the SPL has DRAM size detection enabled, and that the boot order is mmc0 first.

Check: `u-boot-sunxi-with-spl.bin` exists; `strings` shows `U-Boot SPL 2024.07` and `sun4i-a10-pcduino`.

### 4. Card writer for Allwinner: `scripts/write-sdcard-sunxi.sh`

- Same safety checks and layout as `write-sdcard-omap.sh` (a DOS label, a 512 MiB FAT32
  partition from sector 8192 = 4 MiB, the boot flag, and the staging directory copied onto
  it). The difference is the bootloader: `u-boot-sunxi-with-spl.bin` goes raw at **8 KiB**
  (`dd seek=16 bs=512`), and nothing goes onto the FAT partition. The boot ROM reads it from
  there, and U-Boot reads the environment and extlinux from the FAT partition.
- The partition table has to be written first and U-Boot second. The bootloader (under
  1 MiB) ends well before 4 MiB, but never `wipefs` after the `dd`.
- `boards/pcduino/write-sdcard.sh`: a one-line wrapper, like the xM's.
- **Also an image-file mode** (`--image out.img`, no root): `sfdisk` on a file,
  `mkfs.vfat --offset`, `mcopy -i img@@4M`, then the `dd`. Used for QEMU (task 6) and lets the
  owner write the whole card with one `dd`. Check what's available first.

Check: build an image file; `sfdisk -d` shows the layout; the bytes at 8 KiB match the U-Boot
file; `mdir` lists the slots and extlinux; nothing overlaps.

### 5. Image + OTA bundle

- `BOARD=pcduino scripts/build-image.sh`, then `scripts/build-ota.sh`.
- Run `scripts/test-ota.sh` with `BOARD=pcduino` (it has `console=ttyS2` hardcoded in its fake
  cmdline: switch it to `$CONSOLE` from `board.conf`, a test-only fix).
- Size check: the slot has to fit twice in the 512 MiB partition (the xM's is ~110 MB).

Check: the image builds; `extlinux.conf` lists the pcDuino kernel, its DTB and `console=ttyS0`;
the OTA tests pass.

### 6. QEMU smoke boot (best effort)

QEMU's `cubieboard` machine emulates an A10: UART, timers, EMAC, SD, and since QEMU 8 the
DRAM controller, I2C and AXP209, so it can boot SPL from an SD image. Run it in a throwaway
podman container (Fedora 44 or Alpine, with `qemu-system-arm` installed in the container).

- Boot the task 4 image file: `-M cubieboard -sd pcduino.img -nographic` with user
  networking (`-nic user,hostfwd=tcp::2222-:22,hostfwd=tcp::8080-:80`).
- Goals, in order: SPL + U-Boot start and find extlinux; the kernel boots with the pcDuino
  DTB (the emulated board isn't a pcDuino, but it's the same SoC and Ethernet controller);
  the initramfs mounts the slot's modloop; OpenRC starts; SSH and the web page answer on the
  forwarded ports; `panda-ota status` works.
- If the pcDuino U-Boot doesn't run in QEMU (the emulated DRAM or PMIC isn't what it
  expects), try `Cubieboard_defconfig` only to boot the rest, and say so. The real board's
  bootloader is only checked on the board (task B1).
- No audio, Bluetooth or watchdog-reset conclusions from QEMU: it emulates none of them.

Check: a serial log in `build/qemu-pcduino/boot.log` (passwords and keys left out), and the
point the boot reached.

### 7. Docs

- README: a row in the board table, `BOARD=pcduino` in the build steps, the Allwinner card
  writer. PLAN.md: the board, what's verified (QEMU) and what isn't.
- A "bring-up checklist" for the owner (task list B below) at the end of this file.

## Needs the owner and the board

These can't be done unattended: they need the card, serial wiring, a USB DAC and ears.

- **B1. First boot (no serial adapter).** Write the card (`sudo boards/pcduino/write-sdcard.sh
  /dev/sdX build/sdimage/pcduino`, or `--image card.img` and one `dd`). Plug in Ethernet and
  an HDMI monitor/TV *before* power-on (U-Boot only sets up HDMI if a monitor is there).
  On screen: U-Boot (including "DRAM: 512 MiB"), then the kernel and OpenRC messages (the
  kernel has simplefb + fbcon; `console=tty0` is the last console, so OpenRC writes there).
  Then over the network: `ssh panda@panda.local` (or the address from the router's DHCP
  list). Over SSH: U-Boot found a MAC address (from the SID; QEMU has none) and it's stable
  across reboots; `dmesg | grep crng` (24 s in QEMU, which doesn't block the boot; real timing
  differs); `dmesg | grep simplefb`.
  If the screen stays black and there's no SSH: the boot ROM didn't find the SPL, or U-Boot
  saw no monitor. Then a 3.3 V USB-TTL adapter (CP2102/CH340/FTDI) on the debug header is the
  way in. **Not the Panda/xM's DB9 cable**: that's RS-232 (±12 V), and it can damage the A10.
- **B2. Buttons.** Check whether the Back/Home/Menu buttons are fitted (look, or
  `evtest`/`cat /sys/kernel/debug/gpio` with and without pressing). If they are: set
  `BUTTON_INPUT_NAME=gpio-keys`, `BUTTON_KEY=KEY_HOME`. If not: a device tree overlay or patch
  that disables `gpio-keys`, and no pairing button (or a GPIO button on the header, later).
- **B3. Names.** LEDs (`ls /sys/class/leds`), watchdog, thermal zone (`cat
  /sys/class/thermal/*/type`, and the temperature is plausible). Then remove the
  "unverified" marks.
- **B4. USB audio.** Plug in the DAC; `wpctl status` for the sink. Check that the DAC's own
  mixer control works as the volume cap (only if `board.conf` gets one). Then the README's
  test checklist: tone, music, `pw-top` 0 errors, AirPlay, Spotify (CPU load on one A8 core,
  compared with the xM), radio, Bluetooth with a USB dongle. Unplug and replug the DAC
  during playback: there's no onboard output to fall back to, so check that playback comes
  back on the DAC when it reappears.
- **B5. Power cut, watchdog reset (`kill -STOP` the watchdog daemon), factory reset dry run,
  and an OTA update + revert on the real card.**
- **B6. Ports.** The DAC, a BT dongle and possibly Wi-Fi: 2 host ports plus OTG (in host mode,
  through an adapter) or a powered hub. Check the OTG port works as a host: `dmesg | grep
  musb` (QEMU can't tell: it has no model of the A10's OTG controller, so the driver fails
  there reading the FIFO config), and whether the port powers a device (VBUS: the pcDuino DT
  has no supply for it).

## Stop conditions (unattended)

- A generic change alters the Panda or xM builds beyond the expected (version stamps):
  revert it, write down why, and carry on with the board-only tasks.
- Something needs `sudo`, a Fedora package or a card: stop that task and note it for the owner.
- QEMU doesn't get past U-Boot after a reasonable try: record the log and move on. It's a
  bonus, not a gate.

## Open questions

- Whether the Lite has the three buttons (B2), and what the UART header pinout is on this
  revision (the full pcDuino's is documented; assumed the same).
- Whether to keep `cpufreq` at its device tree limits (1 GHz at 1.4 V on `dcdc2`) or cap it
  for heat. Decide after B4 (temperature under Spotify load).

## Log

**2026-11-04/05, unattended: tasks 1-7 done.** Commits `26110e1`..`e9e1577` and after.

- **Board directory** (`boards/pcduino/`): `board.conf` (USB audio only, button off until B2),
  mkimage profile, card writer wrapper. The configs render: no onboard sink, USB cards enabled,
  the tone filter routed by `audio-route.py`.
- **Kernel** `linux-pcduino-6.18.60-r1`. Base: **`omap2plus_defconfig` with the platform
  swapped** (all OMAPs off, `MACH_SUN4I` on), not `sunxi_defconfig`. `sunxi_defconfig` lacks
  ~1100 generic options the xM runs with (SYSVIPC, POSIX mqueue, namespaces, cgroups,
  netfilter, security modules). This way the generic part is the one already verified on two
  boards, and the fragment only adds the A10 drivers. Compared with the xM's final config,
  only SoC-specific options are missing (SATA, HDMI, OMAP sound glue). Notes:
  `TOUCHSCREEN_SUN4I` (the temperature sensor) is a module (it needs HWMON, a module in
  omap2plus). MUSB is dual-role: r0 was host-only and failed in QEMU with "Invalid or missing
  'dr_mode'" (the DT says `otg`, which only a dual-role build accepts). zImage 7 MB (U-Boot
  loads it below the DTB at +16 MB).
- **U-Boot** `Linksprite_pcDuino_defconfig`, one patch: sunxi builds
  `u-boot-sunxi-with-spl.bin` with binman, which needs pylibfdt, and v2024.07's SWIG wrapper
  doesn't compile with Fedora's SWIG 4.4 (`SWIG_Python_AppendOutput` got a third argument).
  `boards/pcduino/u-boot/0001-pylibfdt-swig-4.3.patch` uses `SWIG_AppendOutput`, as upstream
  dtc does. OMAP builds don't use binman and don't get the patch. Checked: eGON boot header,
  DRAM size detection, `fdtfile=sun4i-a10-pcduino.dtb`, boot order fel, mmc0, usb0, pxe, dhcp.
- **Card writer** `scripts/write-sdcard-sunxi.sh`: the OMAP layout (DOS label, FAT32 512 MiB at
  4 MiB), bootloader raw at 8 KiB. `--image` builds the card as a file without root
  (sfdisk + `mkfs.vfat --offset` + mtools); read back byte-identical, table intact after the
  bootloader write. The card mode (sudo, a real card) is tested too.
- **Image + OTA**: 111 MB per slot (fits twice). OTA bundle builds; `test-ota.sh`: 20 passed
  (its fake cmdline now uses the board's console).
- **QEMU** (`scripts/qemu-pcduino.sh`, QEMU 10.2 `cubieboard` in a container): the pcDuino's own
  SPL + U-Boot + kernel + DTB boot from the card image to a login prompt in ~4 min, first try.
  Up: modloop, OpenRC, all appliance services except Soloist (no API key, as expected),
  firewall, Ethernet (100 Mbit/s, DHCP), chrony synced, the web page (HTTP 200, `/api/status`
  shows the board, 503 MB RAM with 267 MB available, the tone sink, OTA booted=confirmed=a),
  sshd (accepts the owner's keys; the session itself wasn't tested because the keys have
  passphrases). `gpio-keys` is the input device's name, as derived. No conclusions from QEMU
  about the temperature (QEMU has no touchscreen/sensor block: `temp` is null), the OTG port,
  audio, BT, the watchdog reset, or timing.
- **No change to the other boards**: the xM image rebuilt after all this is identical except
  for version stamps (same files and packages, byte-identical kernel, modloop and DTBs).
- **No serial adapter for this board** (2026-11-05): HDMI boot console added
  (`linux-pcduino` r2: `FB_SIMPLE` on U-Boot's simple-framebuffer, fbcon; cmdline
  `console=ttyS0,115200n8 console=tty0 consoleblank=0`, tty0 last so OpenRC writes there; a
  serial adapter, if one is used later, then gets only the kernel's messages). r2 checked in
  QEMU: boots to login, Ethernet up, all services except Soloist, OTA booted=confirmed=a. QEMU
  has no HDMI; the screen output is tested on the board.
- Seen and left alone: the DT reserves 96 MiB of CMA (for display/video, below 256 MiB).
  It's reusable (movable pages use it), so it's not lost; trim it only if B4 shows memory
  pressure. `sysctl: kernel.unprivileged_bpf_disabled unknown`: same on the xM (no BPF syscall
  in the baseline).
