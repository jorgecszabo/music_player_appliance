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
  PipeWire backend. The user has Android, so AirPlay is for the Fedora desktop
  (PipeWire RAOP sink) and for guests' iPhones. One shairport-sync 5.1+ instance
  offers **either** classic AirPlay **or** AirPlay 2 (`auto` = AirPlay 2 when
  `nqptp` runs). Default: **classic** (works with PipeWire RAOP and iOS, cheap on
  the A9). AirPlay 2 only adds multiroom/Home-app features. If wanted, run a
  second AirPlay 2 instance under another name, and check whether PipeWire RAOP
  can use it (not verified).
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
- **Headless pairing with button S2 (GPIO 121):** the DT already makes it a
  `gpio-keys` input (`BTN_0`); LEDs `pandaboard::status1/2` give feedback.
  A small Python service (`py3-evdev` + `py3-dbus`, OpenRC):
  - short press: 2-minute pairing window (discoverable + pairable,
    NoInputNoOutput "Just Works" agent); status2 blinks fast
  - pairing succeeds: `lbu commit` (pairing keys), "done" blink, window closes
  - long press (~5 s): forget all paired devices, distinct blink, commit
  - otherwise: not discoverable, not pairable; known devices can still
    reconnect
  Trade-off: anyone nearby can pair during the window (normal for BT speakers).
- **Persistence:** `/var/lib/bluetooth` (pairing keys) must be in lbu, and is
  committed after each new pairing.

## Appliance features (decided 2026-11-03)

These make it behave like a commercial speaker. Built during Phases 4–6.

**Speaker identity**
- One friendly name, **"Panda"**, from one config file (`/etc/panda/speaker.conf`).
  Spotify Connect, the Bluetooth alias, AirPlay and the mDNS/hostname all read it.

**Source handling**
- **Last source wins:** a new stream (Spotify, AirPlay, BT) pauses the active
  one where the protocol allows (Spotify/AirPlay remote control, AVRCP pause).
  Otherwise the old one is muted. WirePlumber policy or a small watcher.
- **One volume:** PipeWire sink volume, synced with BT absolute volume,
  Spotify's volume and the AirPlay volume. A configurable **maximum volume**
  cap, and a fixed **startup volume** (volume itself is not saved, so routine
  changes never write to the card).
- **Tone:** bass/treble (+ optional loudness compensation) through a PipeWire
  filter-chain (biquads; a few % CPU at 96 kHz).

**Feedback**
- **Bose-style chimes, no speech:** short tones for boot-complete, pairing mode
  on/off, BT connected, BT disconnected, factory reset. Small WAV/FLAC files
  played with `pw-play` at a fixed level relative to the volume cap.
- LEDs as in the Phase 5 button design.

**Bluetooth extras**
- **Auto-reconnect** to the most recently used phone at boot (and after BT
  restarts).

**Recovery**
- **Factory reset:** hold S2 while powering on → wipe the saved config
  (lbu overlay) back to the image defaults, chime, reboot. Checked very early in
  boot. Needs a check of how early the button can be read relative to apkovl
  loading. The fallback is a service that deletes the overlay and reboots.

**Web page** (`http://panda.local/`, technical, for the owner)
- Status: current source and stream info, PipeWire graph summary, sample rate and
  xruns, CPU/load/memory/uptime, network, chrony sync, service states, recent log
  lines.
- Control: volume, volume cap, startup volume, bass/treble, speaker name, pairing
  window (same as S2), list and forget paired devices, restart services, reboot.
- Save: config changes go through one **"save to card" (lbu) path** with a
  debounce, so the card is written rarely and deliberately.
- Implementation: Python stdlib HTTP server + one static page with vanilla JS,
  live updates via server-sent events. Data from `wpctl`/`pw-dump`, bluez D-Bus,
  `/proc` + `/sys`, `rc-status`. No framework, no build step.
- LAN only (firewall to the local subnet). Optional HTTP basic auth.
- `lbu commit` atomicity: checked. It writes `<host>.apkovl.tar.gz.new` and then
  `mv`s it over the old one, so a power cut mid-write keeps the old overlay; only
  the FAT rename itself is a short non-atomic window. Add a `sync` + ro remount
  after saving from the web page.

**Not now** (revisit later): USB volume knob (none on hand), line-in
passthrough, amplifier auto-power / HDMI-CEC, network updates, Home Assistant,
UPnP/DLNA local files, multiroom (Snapcast), IR remote / rotary encoder,
display, radio presets / alarm.
USB Wi-Fi as an ethernet alternative: needs a kernel rebuild with `CFG80211`,
`MAC80211` + the dongle's mainline driver, plus its firmware in the modloop. Pick
MediaTek MT7612U (`mt76x2u`) or MT7921AU (`mt7921u`); Realtek 88x1CU (`rtw88`) is
OK. Avoid out-of-tree drivers.

## Users and privileges (decided 2026-11-03)

| User | Purpose | Notes |
|---|---|---|
| `root` | boot services, bluetoothd, sshd | no SSH login; password only for the serial console |
| `panda` | admin login | SSH key only; `doas` nopass (the key is the gate) |
| `speaker` | system user: PipeWire, WirePlumber, librespot, shairport-sync | no login; audio group; owns the PipeWire socket |
| `panda-web` | system user: web page | no login; PipeWire + bluez access; doas allow-list only for `lbu commit`, `reboot`, `rc-service <audio services> restart` |

Network-facing daemons never run as root. The firewall limits services to the
LAN subnet.

## Side quest: McPDM driver root cause (recorded 2026-11-03, not started)

The Phase 4 crackle fix is a **workaround** (keep the sink fed with silence, IRQ scheduling).
Driver findings (`sound/soc/ti/omap-mcpdm.c`, 6.18):
- The McPDM link starts in `prepare` and stops only at `shutdown` (close). The DAI has **no
  `trigger` callback**, so on stop/pause/underrun the DMA stops feeding the DN FIFO while the
  link to the TWL6040 keeps running. That matches every crackle case (idle-open, the suspend
  timeout, underruns). The DN FIFO-empty IRQ is enabled but only `dev_dbg`s.
- Legacy mode is rarely exercised: in TI's original stack the ABE DSP always kept the FIFO fed.
- The tsched underruns are **not** explained by DMA position granularity: OMAP4 sDMA reports at
  burst granularity (DN maxburst = (16-2) x channels = 28 words). Candidates: the 32 kHz
  clocksource/clockevent (`32k_counter`, gptimer 32768 Hz; ~30 us timestamps for PipeWire's
  DLL), and/or no `.delay` reporting for the FIFO. Unverified.

Plan:
1. Confirm: enable `omap_mcpdm` dynamic debug (check `CONFIG_DYNAMIC_DEBUG`; otherwise rebuild
   r3) and watch for "DN (playback) FIFO Empty" while the PCM is open and idle.
2. Patch: add a DAI `trigger` that stops/mutes the DN link (or keeps the FIFO zero-filled) on
   STOP/PAUSE_PUSH/SUSPEND and restarts on START/PAUSE_RELEASE; check the TWL6040 for pops.
3. Retest with stock PipeWire settings (suspend on idle, tsched); investigate tsched separately
   if needed. If it works, send it upstream (linux-sound / linux-omap).
Gain: the device can close when idle (about 1% CPU, codec powered down), plus an upstream fix.

## Portability (design rule, decided 2026-11-03)

The Panda is 16 years old; the repo should survive it.
- After Phase 3 boots, move board-specific parts into `boards/pandaboard/`:
  U-Boot build, kernel aport and fragment, mkimage profile bits, and a
  **`board.conf`** (ALSA card + mixer setup, sample rate, BT controller and
  firmware, button input device, LED names, serial console, lbu media,
  Alpine arch).
- The Phase 4–6 userspace (audio services, pairing, chimes, web page, reset)
  reads `/etc/panda/board.conf` and never hardcodes `ttyS2`, `TWL6040`, GPIO 121
  or LED names.
- Build scripts take the arch/board as a parameter (no hardcoded `armv7`).
- Likely successor (Raspberry Pi or x86 mini PC, plus a USB DAC): uses Alpine's
  stock kernel and boot, so the board directory is only a profile and
  `board.conf`.

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
  (`boards/pandaboard/u-boot/build.sh`; `TOOLS_LIBCRYPTO` disabled because Fedora lacks the
  OpenSSL ENGINE headers). Default env: `findfdt` sets `omap4-panda.dtb`,
  console `ttyO2,115200n8`, distro boot (extlinux) on mmc0 first.
  SD card writer: `sudo boards/pandaboard/write-sdcard.sh /dev/sdX build/sdimage`.
  Hardware findings (board is **OMAP4430-GP ES2.1**):
  - The ROM does **not** boot `MLO` from our FAT partition (probably the old
    CHS-geometry quirk). **Raw mode works**: `MLO` at sector 256, `u-boot.img`
    at sector 768, which `boards/pandaboard/write-sdcard.sh` now writes. U-Boot itself reads the
    FAT partition fine, so kernel/extlinux/Alpine stay on p1.
  - Debian 12's U-Boot 2023.01 reaches the prompt. In U-Boot, `usb start` finds
    no LAN9514 hub/ethernet (bit-rot). Linux powers the hub itself from the DT,
    but U-Boot won't set a stable MAC, so pin `hwaddress` in the Alpine network
    config.
  - `ti_sysc target-module@d5000` (MMC5, Wi-Fi SDIO) errors are harmless.
  - Our own 2024.07 build boots to the prompt in raw mode, so we use it.
- [x] **Phase 2** (2026-11-03): Linux 6.18.60 LTS cross-builds
  (omap2plus_defconfig + `boards/pandaboard/aports/linux-pandaboard/pandaboard.config`). Notes: gzip kernel compression (Fedora has no `lzma` tool);
  USB gadget/MUSB disabled so NOP_USB_XCEIV/EHCI/SMSC95xx can be built in; zstd firmware
  loading enabled (Alpine compresses linux-firmware). The Phase 2 test initramfs tooling was removed in the restructure (it's in git history).
  **Verified on hardware (2026-11-03):**
  - Ethernet: Linux powers the LAN9514 hub; `eth0` gets DHCP. MAC
    `02:03:00:a0:15:e0` is the same on every boot, so no pinning needed.
  - Audio: TWL6040 card `PandaBoard` plus an `HDMI` card. **McPDM only runs at
    88.2/96 kHz, S32_LE** (no ABE DSP in mainline, so nothing converts rates in
    hardware). The codec PLL narrows this further: `PLL Selection`=`High-Performance`
    (HPPLL, from the 38.4 MHz mclk) allows **96 kHz only**; `Low-Power` (LPPLL, from
    the 32 kHz clock) allows 88.2 and 96 kHz. Plan: fixed 96 kHz on HPPLL, with
    PipeWire resampling everything (44.1 kHz sources → 96 kHz). 88.2 kHz would need
    LPPLL, which is worth an A/B listening test but is likely the jitterier clock. **The audio-out jack is on the headset path**: set
    `Headset Left/Right Playback`=`HS DAC` and `Headset` volume (0–15). Leave
    the line-out path off (it stays silent with `AUXL/AUXR` off). The ABE
    `DL1 PDM` controls don't exist without ABE firmware.
  - Bluetooth: onboard WL1271 comes up as `hci0` (TI, HCI/LMP 2.1,
    TIInit_7.2.31 firmware loads, baud switch OK).
  - CPU scales 300–1008 MHz. 8 GB SDHC runs in high-speed mode.
  - Cleanup for the final kernel: OMAP remoteproc (DSP/IPU, 120 MB CMA, no
    firmware) and the omap4-keypad probe error. TWL6030 "vsel out of range"
    warnings are for the 1.2 GHz OPP the 4430 doesn't use, so they're harmless.
- [x] **Phase 3** (2026-11-03): Alpine diskless image.
  - `boards/pandaboard/aports/linux-pandaboard` (release `6.18.60-0-pandaboard`): the kernel is
    **cross-compiled** by `cross-build.sh` in an x86_64 Alpine container
    (`container/Containerfile.cross`, Alpine `arm-none-eabi-gcc` 16.1, about 10 min) into
    `build/kernel-staging/`. The APKBUILD only packages those files in the armv7 container.
    `scripts/build-package.sh linux-pandaboard` does both (output in `packages/main/armv7/`).
    Building natively under qemu-user took about 2 hours, so we dropped it. abuild runs on a copy
    in `build/abuild/` as container root (`abuild -F`), because setuid/sudo don't work under
    qemu-user.
  - `boards/pandaboard/mkimg.pandaboard.sh` profile + `appliance/genapkovl.sh` (hostname
    `panda`, eth0 DHCP, chronyd, `LBU_MEDIA=mmcblk0p1`; **SSH only as user `panda`**, key-only,
    `PermitRootLogin no`; `doas` `permit nopass panda`; root keeps a password for the serial
    console). The `panda` user is created by `/etc/local.d/panda-firstboot.start` (keys from
    the build host's `~/.ssh/*.pub` via `appliance/authorized_keys`, gitignored; password field
    `*`, because busybox's `!` would make OpenSSH refuse keys; `/home/panda/.ssh` added to lbu;
    commits once). When running `setup-alpine`, skip its user-creation step. Built by
    `scripts/build-image.sh` into `build/sdimage/`. Serial getty comes from
    `console=ttyS2` (initramfs adds it). The modloop gets TIInit explicitly
    (`modloopfw`).
  - First boot hung in OpenRC's dependency scan: `sshd` blocked in `getrandom()`. The
    A9 has no fast cycle counter (jitter entropy runs off the 32 kHz clock) and
    `omap_rng` was a module, not loaded before the modloop. **Fix: `HW_RANDOM` and
    `HW_RANDOM_OMAP` built in** (kernel r1). Diagnosed with `scripts/serial-sysrq.py t`.
  - Added avahi + dbus so `panda.local` resolves (also needed for Spotify/AirPlay discovery).
  - **Boots to login (2026-11-03).** crng ready at 3.1 s; boot media `ro`; modloop mounted;
    all services up; chrony synced; 26 MB RAM used; `cpu_thermal` reads (57 °C).
    `thermal_zone1` is a phantom bq27000 "battery" from the HDQ/1-wire driver (harmless;
    could drop `W1`/`HDQ_MASTER_OMAP`/`BATTERY_BQ27XXX` later).
  - lbu had no `LBU_MEDIA` (an earlier generator edit lost the `lbu.conf` block), so the
    first-boot commit failed. Now the first-boot script runs `setup-lbu mmcblk0p1` and
    `setup-apkcache /media/mmcblk0p1/cache` itself. The live board was fixed by hand
    and committed.
  - **Power-yank test passed:** overlay reloads, SSH host key is unchanged, root
    password and `panda` are kept, first-boot doesn't rerun, no FAT dirty/fsck
    warnings, `lbu status` is clean.
- [x] **Repo restructure** (2026-11-03): `boards/pandaboard/` + `board.conf`, `appliance/`,
  board-selectable scripts (`BOARD=`), container arch from `ALPINE_ARCH`. Verified: the rebuilt
  image is identical apart from the intended overlay changes (`/etc/panda/*.conf`, first-boot
  script reading them). Both conf files were installed on the live board and committed.
- [~] **Phase 4** (2026-11-03): audio.
  - Deploy model: `appliance/rootfs/` (files under /etc) + `/etc/panda/setup.sh` (idempotent:
    repos, packages, eudev, users, generated configs, services, lbu), pushed by
    `scripts/deploy.sh [--restart]`.
  - **eudev instead of mdev**: PipeWire finds cards via libudev, and eudev's libudev reports
    devices as uninitialized without udevd. (Kernel r2 also enables UEVENT_HELPER like Alpine;
    not yet installed on the board.)
  - PipeWire + WirePlumber (profile `panda` = `main-embedded` + no BT/video/dbus) run as user
    `speaker` under supervise-daemon; the data loop runs SCHED_FIFO 88 via `rc_ulimit -r 95 -e 40`.
    Fixed 96 kHz S32LE, quantum 2048; HDMI card disabled by a WirePlumber rule.
  - **TWL6040 crackle findings:** the codec crackles whenever the PCM is open without data
    (idle-open, or during a suspend timeout). It also underran about 3x/s under PipeWire's
    timer-based scheduling (coarse McPDM/sDMA position). Fix: never suspend,
    `node.pause-on-idle=false`, `node.always-process=true` (feed silence from boot),
    `api.alsa.disable-tsched=true`, period 2048 x 4. Verified: 0 errors and clean audio idle
    and playing. Cost: PipeWire about 1% CPU at idle.
  - **Spotify Connect, librespot-based clients: blocked.** spotifyd 0.4.2 (and per reports
    librespot 0.7/0.8 and go-librespot) get `audio key error 0 1`: since about Nov 2025 Spotify
    refuses the legacy AES audio-key request on a growing set of accounts, in favor of its newer
    PlayPlay DRM, which open-source clients don't implement (librespot #1649,
    go-librespot #279/#317). Bitrate doesn't matter.
  - **Spotify Soloist** (Spotify's official headless Connect client, `soloist`) instead: a glibc
    ARMv7 binary (needs glibc >= 2.38; links libc/libm/libpthread/libdl/librt/libatomic, and
    dlopens libpipewire-0.3 or libpulse). gcompat is too old (missing `__isoc23_*`,
    `__fcntl_time64`, ...). Works with a **Debian trixie glibc runtime squashfs** (2.4 MB:
    glibc 2.41, libpipewire 1.4.2 client, client modules, SPA support/audioconvert; built by
    `scripts/build-soloist-runtime.sh`), run as `<rt>/lib/ld-linux-armhf.so.3 --library-path
    <rt>/lib soloist`. `--version` OK on the board. Constraints: a personal API key from the
    Spotify for Developers dashboard (Premium); **builds expire after 90 days** (exit code 10),
    so the device must re-download periodically; **no redistribution**, so the device downloads
    from soloist-builds.spotifycdn.com itself and the binary is never in git or images.
  - **Soloist works (2026-11-04):** plays from the Spotify app, volume via its own PipeWire stream
    volume, 44.1 kHz F32 stream resampled to 96 kHz, 0 errors, about 11% CPU (verbose), 63 C.
    Service `speaker-soloist` (user speaker): the binary on the boot media
    (`/media/<boot>/soloist/soloist`, downloaded by the device with `/etc/panda/soloist-update.sh`,
    plus a weekly cron job `/etc/periodic/weekly/panda-soloist-update`); the runtime squashfs on the
    boot media (installed by setup.sh from deploy.sh), mounted at /opt/soloist-runtime; API key in
    `/etc/panda/soloist.key` (0640 root:speaker, lbu). The key is a CLI argument, so `/proc` is
    remounted `hidepid=invisible` (verified hidden from `panda`). Session/cache in RAM.
  - mDNS: spotifyd's built-in responder claimed the hostname, so avahi renamed itself
    `panda-2.local`. Soloist's responder doesn't (avahi keeps `panda.local`). spotifyd removed.
  - **lbu bug found by an accidental power cut:** apk's built-in protected paths have
    `@etc/init.d` (symlinks only), so `lbu commit` saved the runlevel links but not our init
    scripts, and every speaker-*/panda-* service vanished on reboot. setup.sh now
    `lbu include`s each of them. (Earlier checks passed only because the board hadn't rebooted.)
  - **AirPlay (classic):** shairport-sync 5.5.1 as `speaker-shairport` (user speaker), name
    from SPEAKER_NAME, `allow_session_interruption` on. Alpine's build has **no native
    PipeWire backend** despite `--with-pw` in the APKBUILD (`shairport-sync -h`: alsa, pipe,
    stdout), so it plays through the ALSA `pipewire` PCM. Advertised as `<id>@Panda` on
    `_raop._tcp`. Tested with PipeWire's `module-raop-sink` (what Fedora would use) from a
    throwaway container: stream linked into the graph, about 13% CPU. pyatv is not a usable test
    sender: it announces L16 PCM, which shairport decodes as ALAC and crashes on (it
    respawns; malformed input from the LAN can crash it). D-Bus/MPRIS name warnings are harmless.
  - **Firewall:** nftables table `inet panda` (`/etc/panda/firewall.nft`, service
    `panda-firewall` in the boot runlevel): drop by default, accept private IPv4, IPv6 link-local and
    ULA, the on-link IPv6 prefixes (set `lan6_ra`, filled from the router-ad routes by
    `/etc/panda/firewall.sh update` shortly after boot and every 15 min, since this LAN has a
    global /64), ICMP/ICMPv6, DHCP. By source, not port (Soloist's zeroconf port is random).
  - **Startup volume:** the sink comes up at 1.0 (0 dB) every boot (WirePlumber's default was
    0.40 = -24 dB); the hardware gain from board.conf is the cap, each source sets its own
    stream volume.
  - **Reboot test passed** (clean reboot, after the accidental power cut): all services up,
    volume 1.0, firewall loaded. Idle: 37 MB used (plus 232 MB tmpfs/shared), 63.5 C,
    about 0-1% CPU besides PipeWire's feed.
  - Image integration is deferred until Phases 5-6 are done, so the image is built once
    (rootfs + setup.sh at first boot, packages in the apk cache, Soloist runtime on the card,
    kernel r2). A fresh card should then need only the Soloist API key.
- [~] **Phase 5** (2026-11-04): Bluetooth speaker.
  - Onboard WL1271 is enough: fixed public address 64:9C:8E:AE:00:1F, BR/EDR only.
  - WirePlumber `hardware.bluetooth = required` (`optional` means "only if something wants
    it", so the monitor never loaded), roles `[ a2dp_sink ]`, codecs SBC/SBC-XQ/AAC, no HFP/HSP,
    `hw-volume` for AVRCP absolute volume, autoswitch and persistent storage off.
  - bluez main.conf generated by setup.sh: name from SPEAKER_NAME, loudspeaker class,
    `ControllerMode=bredr`, not pairable/discoverable by default, `JustWorksRepairing=confirm`,
    reconnect policy. `/var/lib/bluetooth` is in lbu.
  - `panda-bt` (root, `/etc/panda/panda-bt.py`, python3 + py3-dbus-next + py3-evdev, no
    GObject): NoInputNoOutput agent that accepts pairing only in the window; S2 short press =
    120 s window, hold 5 s = forget all; LED_BT (status2): fast blink window, solid connected,
    slow blink forgetting; trusts + `lbu commit` after a pairing; reconnects paired devices at
    start; event hook for the Phase 6 chimes.
  - **Works (2026-11-04):** POCO X8 Pro (Android) paired with the button, plays with **AAC**,
    phone volume buttons control the stream, 0 errors, about 6% CPU total, 63.5 C.
  - Next: reboot test (pairing persists, auto-reconnect, same controller address).

### Project layout

```
boards/<board>/            everything board-specific (only pandaboard so far)
  board.conf               hardware description: arch, kernel flavor, DTB, console,
                           boot media, audio card/rate/mixer, BT, button, LEDs.
                           Also installed on the device as /etc/panda/board.conf
  mkimg.<board>.sh         mkimage profile (reads board.conf + appliance files)
  write-sdcard.sh          SD card writer (pandaboard: raw-mode U-Boot + FAT32)
  u-boot/build.sh, swap.sh U-Boot v2024.07 build / swap U-Boot on a written card
  aports/<pkg>/            board packages (linux-pandaboard: APKBUILD,
                           cross-build.sh, pandaboard.config fragment)
appliance/                 board-independent
  appliance.conf           hostname, speaker name, admin user (/etc/panda/appliance.conf)
  packages.list            packages for the card's local repository
  genapkovl.sh             first-boot overlay (users, SSH, doas, lbu, services)
  authorized_keys          admin SSH keys, generated from ~/.ssh/*.pub (gitignored)
aports/<pkg>/              generic packages (none yet)
container/                 Containerfile (target-arch abuild/mkimage env),
                           Containerfile.cross (x86_64 + arm-none-eabi-gcc)
scripts/                   env.sh (BOARD selection), alpine.sh, build-container.sh,
                           setup-keys.sh, build-package.sh, build-image.sh,
                           update-boot-files.sh, serial-sysrq.py
build/, packages/          outputs (gitignored)
keys/                      abuild keys (private key gitignored)
```
