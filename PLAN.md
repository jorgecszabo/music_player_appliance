# Alpine Linux on PandaBoard (rev A1): network audio appliance

## Goal

A headless audio receiver ("network/Bluetooth speaker") built on a PandaBoard A1
(OMAP4430, 2x Cortex-A9 @ 1 GHz, 1 GB LPDDR2) running Alpine Linux in
**diskless mode**. The root filesystem lives in RAM, and the SD card is mounted
read-only. Pulling the plug at any time is safe.

Audio sources (all mixed by PipeWire on the board):

- **Spotify Connect** (librespot / spotifyd)
- **AirPlay** (shairport-sync). Linux desktops with PipeWire can stream to this
  through PipeWire's RAOP sink.
- **PipeWire/PulseAudio network sink** (pulse TCP or RTP/ROC), as an alternative
  for Linux hosts
- **Bluetooth A2DP sink**: phones pair and play to the Panda like a BT speaker

Output: the onboard TWL6040 3.5 mm jack. Optional later: a USB DAC or HDMI audio.
Network: wired ethernet only (onboard Wi-Fi disabled).

## Findings that shape the plan (verified 2026-11-03)

- Alpine's `linux-lts` armv7 config has **no OMAP support** (`ARCH_OMAP2PLUS`
  is not set), so a custom kernel package is required.
- U-Boot **removed `omap4_panda`** in July 2024 (commit "arm: Remove omap4_panda
  board", for not being converted to DM_I2C). Pin **U-Boot v2024.07**, the last
  release that has it.
- The mainline Linux DT `arch/arm/boot/dts/ti/omap/omap4-panda-common.dtsi`
  already describes the audio card (`ti,abe-twl6040`) and the Bluetooth chip
  (`ti,wl1271-st` on UART2, enable GPIO 46).
- Alpine's own `u-boot` package does not build the Panda, so we package it
  ourselves.

## Target SD layout

```
SD card (MBR)
└─ p1  FAT32, boot flag, ~512 MB, mounted read-only
   ├─ MLO, u-boot.img            ← U-Boot v2024.07 (omap4_panda_defconfig)
   ├─ extlinux/extlinux.conf
   ├─ boot/vmlinuz-panda, initramfs-panda, modloop-panda
   ├─ boot/dtbs/omap4-panda.dtb
   ├─ apks/                      ← local package cache (boots offline)
   └─ <hostname>.apkovl.tar.gz   ← config saved by lbu
```

The root filesystem is tmpfs. The SD card is written only during an explicit
`lbu commit` (or an apk cache update), which remounts rw briefly and then back
to ro.

---

## Phase 0: Host setup (Fedora)

- Cross toolchain `gcc-arm-linux-gnu` for kernel and U-Boot (bare-metal, no libc
  needed).
- A podman Alpine container for `abuild` (packaging kernel, U-Boot and the image
  profile).
- `qemu-user-static-arm` (binfmt) so we can run an armv7 Alpine container/chroot
  for native package builds and testing.
- Serial console: 115200 8N1 on the DB9 (OMAP UART3, which is `ttyS2` in Linux).
  The user must be in the `dialout` group.

## Phase 1: Bootloader

- Build U-Boot `v2024.07`, `omap4_panda_defconfig`, which gives `MLO` and
  `u-boot.img`.
- The OMAP4 ROM boots `MLO` from the first FAT partition with the boot flag set.
- Use distro boot with `extlinux.conf`. Hard-code `fdt /boot/dtbs/omap4-panda.dtb`
  (the A1 is before the A4 revision, so not `-a4` and not `-es`).
- U-Boot derives a stable MAC from the die ID and patches it into the DT ethernet
  node, which gives a stable DHCP lease. Verify this in Phase 3.
- **Milestone:** U-Boot prompt on serial.

## Phase 2: Kernel (`linux-pandaboard` aport)

Start from the current mainline LTS plus `omap2plus_defconfig`, then trim it and
ensure these options:

| Area | Options |
|---|---|
| SoC / boot | `ARCH_OMAP4`, `TI_SYSC`, `OMAP_INTERCONNECT`, TWL6030 PMIC + regulators, `MMC_OMAP_HS`/`MMC_SDHCI_OMAP` **=y**, `SERIAL_8250_OMAP` + console |
| Ethernet | `USB_EHCI_HCD_OMAP`, `NOP_USB_XCEIV`, `USB_NET_SMSC95XX` (LAN9514), built in |
| Audio (onboard) | `MFD_TWL6040`, `SND_SOC_TWL6040`, `SND_SOC_OMAP_MCPDM`, `SND_SOC_OMAP_ABE_TWL6040`, `SND_SOC_OMAP_DMIC`, `DMA_OMAP`/`TI_EDMA` |
| Audio (optional) | `SND_USB_AUDIO` (USB DAC); `DRM_OMAP` + HDMI4 + `OMAP_HDMI_AUDIO` (HDMI to an AVR) |
| Bluetooth | `BT`, `BT_HCIUART`, `BT_HCIUART_LL`, `SERIAL_DEV_BUS` (onboard WL1271); `BT_HCIBTUSB` (dongle); `BT_RFCOMM`, `BT_BNEP`, `UHID` |
| Networking extras | `IPV6`, multicast (mDNS for AirPlay/Spotify discovery) |
| Diskless needs | `SQUASHFS`, `OVERLAY_FS`, `BLK_DEV_LOOP`, `TMPFS` |
| Misc | `CPU_FREQ` + `ARM_TI_CPUFREQ`, `RTC_DRV_TWL`, `OMAP_WATCHDOG` |
| Leave out | `WLCORE*`/`WL12XX` (Wi-Fi), GPU/SGX, camera, display (unless HDMI audio) |

- Package it as an APKBUILD modelled on `main/linux-lts`, so `mkinitfs` and the
  modloop work the Alpine way.
- **Milestone:** boot to shell; `aplay -l` shows the TWL6040 card; `eth0` is up.

## Phase 3: Alpine diskless image

- Add a custom `scripts/mkimg.pandaboard.sh` profile in aports, modelled on the
  `uboot` profile. It produces a tarball with the kernel, initramfs, modloop,
  DTB, extlinux.conf, U-Boot binaries and an `apks/` repo including our
  packages.
- Kernel cmdline: `modules=loop,squashfs,sd-mod,usb-storage console=ttyS2,115200n8`
  plus the Alpine defaults.
- On the board: `setup-alpine` with disk = none (diskless), lbu media and apk
  cache on `mmcblk0p1`; the boot media stays `ro` in fstab.
- Clock: there is no RTC battery, so use `chronyd` over ethernet. Nothing may
  depend on wall-clock time before the network is up.
- Logs go to RAM (or remote syslog).
- **Milestone:** after a power yank, the board reboots to the identical
  configured state.

## Phase 4: Audio stack (PipeWire as the hub)

With several sources sharing one output, PipeWire does the mixing and
arbitration, Bluetooth integration and the network sink.

- **PipeWire + WirePlumber, run system-wide** as a dedicated `audio` user from an
  OpenRC service (no desktop login session on the appliance). `pipewire-pulse`
  provides a PulseAudio-compatible server for clients.
- **TWL6040 mixer:** expect everything muted or unrouted on first boot. Find the
  working headset/line-out route with `alsamixer`, `alsactl store`, and add
  `/var/lib/alsa/asound.state` to lbu. Possibly add a UCM profile or a WirePlumber
  rule pinning the card profile.
- **Spotify Connect:** `librespot` or `spotifyd` with its PulseAudio/PipeWire
  backend, using zeroconf discovery (no stored credentials needed). Check which
  is packaged in Alpine community/testing, or package librespot ourselves (Rust;
  build in the armv7 container).
- **AirPlay:** `shairport-sync` (Alpine community) with `avahi`, PulseAudio/
  PipeWire backend. AirPlay 1 is cheap on the A9; AirPlay 2 needs `nqptp` and
  more CPU, so test it.
  - Bonus: Fedora desktops can stream to the Panda over AirPlay through
    PipeWire's RAOP sink (`libpipewire-module-raop-discover`), with no extra
    setup on the Panda.
- **PipeWire/Pulse network sink (alternative):** enable
  `module-native-protocol-tcp` (restricted to the LAN subnet) or RTP/ROC sink,
  and use a tunnel sink on the desktop.
- **Volatile state:** PipeWire and WirePlumber state go to tmpfs. Only
  deliberately chosen config is persisted with lbu (`/etc/pipewire`,
  `/etc/wireplumber`, the shairport-sync and librespot config).
- Avahi service names make the box show up as e.g. "PandaBoard Speaker".

## Phase 5: Bluetooth speaker (A2DP sink)

- **Onboard WL1271:** BT 2.1+EDR, SBC codec only, no BLE. Needs
  `TIInit_7.2.31.bts` (linux-firmware or TI's bt-firmware repo). Brought up
  through `hci_ll` serdev. Try it first, since it's free.
- **USB dongle (likely the final choice):** a BT 5.x dongle (e.g. RTL8761B,
  `btusb` + linux-firmware `rtl_bt/`). Better range and pairing behaviour, and
  newer phones will be happier.
- **Stack:** `bluez` + WirePlumber's bluez5 monitor (A2DP sink role; optionally
  AAC via fdk-aac if packaged). Disable the onboard controller if the dongle is
  used.
- **Headless pairing:** use a small agent (bluez `simple-agent`-style
  NoInputNoOutput, or a pairing-mode timeout after boot or after a button press).
  The Panda's user button (GPIO 121) could open a pairing window.
- **Persistence:** `/var/lib/bluetooth` (pairing keys) must be in lbu, and is
  committed after each new pairing.

## Phase 6: Hardening and polish

- `rc_parallel`; only needed services; getty on serial only.
- Watchdog (`OMAP_WATCHDOG` + busybox watchdog).
- An `lbu-commit` helper: remount rw, commit, sync, remount ro. Run it only on
  config changes and new BT pairings.
- Use a good-quality SD card (cheap cards can corrupt themselves on power loss
  even under read-only use).
- Optional: a heartbeat LED or LED status for "pairing mode" and "streaming".

## Risks / verify early

1. TWL6040 routing on the mainline driver (legacy McPDM path, no ABE firmware):
   alsamixer work is expected.
2. LAN9514 hub power/reset sequencing: if `eth0` is missing, look there first.
3. Early OMAP4430 ES2.x: stay at the 1.0 GHz OPP or below.
4. CPU headroom on the A9 with PipeWire resampling plus AirPlay 2 decryption:
   measure, and keep the PipeWire quantum and resampler quality modest.
5. Packaging gaps in Alpine (librespot/spotifyd, nqptp): we may need our own
   APKBUILDs.

## Fedora host packages

Already present: git, make, gcc, bc, bison, flex, openssl-devel,
elfutils-libelf-devel, ncurses-devel, python3-devel, python3-setuptools, podman,
qemu-user-static-arm, dosfstools, parted, mtools, cpio, rsync, perl, xz.

To install:

```
sudo dnf install gcc-arm-linux-gnu binutils-arm-linux-gnu dtc swig \
    gnutls-devel libuuid-devel uboot-tools lz4 picocom
sudo usermod -aG dialout $USER   # serial port access; log out/in afterwards
```

---

## Status

- [x] **Phase 0** (2026-11-03): host tools installed; armv7 Alpine 3.24 build
  container `localhost/panda-alpine-build:3.24` (`scripts/build-container.sh`,
  run with `scripts/alpine.sh`); abuild signing key in `keys/`
  (the private key is gitignored, so back it up); host cross gcc 16.2 checked.
  Still to do: re-login for `dialout`, and connect the serial adapter.
- [x] **Phase 1** (2026-11-03): U-Boot v2024.07 builds with the host cross gcc
  (`scripts/build-uboot.sh`; `TOOLS_LIBCRYPTO` disabled because Fedora lacks the
  OpenSSL ENGINE headers). Default env: `findfdt` sets `omap4-panda.dtb`,
  console `ttyO2,115200n8`, distro boot (extlinux) on mmc0 first.
  SD card writer: `sudo scripts/write-sdcard.sh /dev/sdX`.
  Hardware findings (board is **OMAP4430-GP ES2.1**):
  - The ROM does **not** boot `MLO` from our FAT partition (probably the old
    CHS-geometry quirk). **Raw mode works**: `MLO` at sector 256, `u-boot.img`
    at sector 768, which `write-sdcard.sh` now writes. U-Boot itself reads the
    FAT partition fine, so kernel/extlinux/Alpine stay on p1.
  - Debian 12's U-Boot 2023.01 reaches the prompt. In U-Boot, `usb start` finds
    no LAN9514 hub/ethernet (bit-rot). Linux powers the hub itself from the DT,
    but U-Boot won't set a stable MAC, so pin `hwaddress` in the Alpine network
    config.
  - `ti_sysc target-module@d5000` (MMC5, Wi-Fi SDIO) errors are harmless.
  - Our own 2024.07 build boots to the prompt in raw mode, so we use it.
- [x] **Phase 2** (2026-11-03): Linux 6.18.60 LTS cross-builds
  (`scripts/config-kernel.sh` = omap2plus_defconfig + `aports/main/linux-pandaboard/pandaboard.config`,
  then `scripts/build-kernel.sh`). Notes: gzip kernel compression (Fedora has no `lzma` tool);
  USB gadget/MUSB disabled so NOP_USB_XCEIV/EHCI/SMSC95xx can be built in; zstd firmware
  loading enabled (Alpine compresses linux-firmware). Test image: `scripts/make-test-initramfs.sh`
  makes `build/testboot/` (Alpine userland + modules + `test/init`, shell on serial).
  **Verified on hardware (2026-11-03):**
  - Ethernet: Linux powers the LAN9514 hub; `eth0` gets DHCP. MAC
    `02:03:00:a0:15:e0` is the same on every boot, so no pinning needed.
  - Audio: TWL6040 card `PandaBoard` plus an `HDMI` card. **McPDM only runs at
    88.2/96 kHz, S32_LE**, so set the PipeWire rate to 88200 (44.1 kHz sources ×2)
    with 96000 allowed. **The audio-out jack is on the headset path**: set
    `Headset Left/Right Playback`=`HS DAC` and `Headset` volume (0–15). Leave
    the line-out path off (it stays silent with `AUXL/AUXR` off). The ABE
    `DL1 PDM` controls don't exist without ABE firmware.
  - Bluetooth: onboard WL1271 comes up as `hci0` (TI, HCI/LMP 2.1,
    TIInit_7.2.31 firmware loads, baud switch OK).
  - CPU scales 300–1008 MHz. 8 GB SDHC runs in high-speed mode.
  - Cleanup for the final kernel: OMAP remoteproc (DSP/IPU, 120 MB CMA, no
    firmware) and the omap4-keypad probe error. TWL6030 "vsel out of range"
    warnings are for the 1.2 GHz OPP the 4430 doesn't use, so they're harmless.
- [ ] Phase 3: Alpine diskless image

### Project layout

```
container/Containerfile   armv7 Alpine build env (abuild, mkimage deps)
scripts/env.sh            shared settings (Alpine version, CROSS_COMPILE)
scripts/alpine.sh         run a command/shell in the container (/work = repo)
scripts/setup-keys.sh     one-time abuild key generation
scripts/build-uboot.sh    build MLO + u-boot.img into build/u-boot-out/
scripts/write-sdcard.sh   partition/format SD (FAT32 boot) and install files
scripts/swap-uboot.sh     replace U-Boot files on a written card (FAT copies only)
scripts/config-kernel.sh  kernel .config from omap2plus + our fragment
scripts/build-kernel.sh   cross-build zImage, DTB, modules
scripts/make-test-initramfs.sh  Phase 2 test boot files into build/testboot/
test/                     test init + audio test script
aports/main/              our APKBUILDs (linux-pandaboard, u-boot-pandaboard, ...)
build/                    sources and outputs (gitignored)
keys/                     abuild keys (private key gitignored)
```
