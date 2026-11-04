# Repo review: idiomatic or hacky?

A review of the repo as of 2026-11-04 (after the BeagleBoard-xM port), to come
back to later. Nothing here has been acted on yet.

**Short answer:** the foundation is idiomatic Alpine. The non-standard parts are
mostly glue around build speed and the OMAP boot ROMs. A few things are really
hacky.

## Idiomatic / fine

- **Diskless Alpine with lbu, an apkovl, a modloop and an apk cache on the
  boot media.** This is Alpine's documented model (the official Raspberry Pi
  images work this way). Building the card with `mkimage.sh` and a custom
  profile is how Alpine builds its own images.
- **Kernel package layout** copies `main/linux-lts`: release
  `<ver>-<rel>-<flavor>`, `/boot/vmlinuz-<flavor>`, `dtbs-<flavor>/`. So
  mkinitfs, update-kernel and the modloop service work unchanged.
- **Kernel config:** a fragment over `omap2plus_defconfig`, merged with
  `merge_config.sh`. Standard embedded practice. Building `omap_rng` in to
  avoid the entropy hang at boot is normal too.
- **Boot:** U-Boot distro boot reading `extlinux.conf`, with `FDTDIR` and
  `findfdt` picking the device tree for the board revision. Intended use.
- **OpenRC services** with `supervise-daemon`, separate service users,
  nftables, doas, `hidepid`. All idiomatic.
- **`boards/<board>/` with a `board.conf`.** Buildroot `board/` directories and
  Yocto BSP layers split board files out the same way. Keeping hardware details
  out of the appliance code is the right call.
- **A local signed package repository** built with abuild. Standard.

## Pragmatic but non-standard

- **The kernel APKBUILD doesn't build the kernel.** `cross-build-kernel.sh`
  compiles it on the host, and the APKBUILD packages a staging directory from a
  hardcoded `/work/...` path, with `!tracedeps`. The apk's checksums cover only
  the config fragment, not the kernel source, so nobody can rebuild it from the
  aport alone. It saves about 2 hours per build under qemu, so it's worth
  keeping, but it isn't how Alpine packages kernels.
- **U-Boot is built unlike everything else:** with Fedora's host cross
  compiler, outside any container, so it depends on the host. One source tree
  is shared by all boards and patched in place after a `git checkout -- .`
  reset. It's pinned to v2024.07 because later releases dropped `omap4_panda`.
- **`write-sdcard-omap.sh`:** `sfdisk` and `dd` at fixed sectors, U-Boot
  written twice (raw and on FAT). Normal for hand-rolled embedded tooling;
  Buildroot would use `genimage` to produce an image file instead.
- **One mkimage profile per board**, near-duplicates. mkimage builds shell
  function names from the profile name, which is also why board names must be
  shell identifiers (`beaglexm`, not `beagleboard-xm`).
- **`build-image.sh` glue:** deletes mkimage's overlay cache (its checksum
  never changes), copies files into `~/.mkimage`, runs mkimage as `su builder`
  inside the container. Works, but brittle.
- **Forking PipeWire and mpv** to drop the GPU stack (mesa + llvm, ~170 MB of
  RAM on a diskless board). A valid goal. The Alpine way would be an aports
  merge request that splits the libcamera SPA plugin out of `pipewire-libs`,
  fixing it at the source. Forks are packages to maintain indefinitely.

## Hacky

1. **Overriding Alpine packages by version.** `pkgrel=100` plus `!mesa` /
   `!libcamera` in the world is a trick. apk's mechanism for this is a tagged
   repository (`@local /media/.../apks`) with pins like `pipewire-libs@local`.
   As it stands, ours wins only until Alpine releases a newer `pkgver`; after
   that, the conflicts silently freeze the boards on old versions.
   `scripts/check-forks.sh` helps, but only when someone runs it. It compares
   versions with `sort -V`, which only approximates apk's ordering
   (`apk version -t` is the real comparison).
2. **First boot installs the appliance on the device.** A `local.d` script
   creates the user and commits with lbu, then `setup.sh` installs ~200
   packages into RAM, generates configs and enables services, all
   imperatively. Its "done" check is "the admin user exists", so a failed
   setup is never retried (the BeagleBoard-xM got stuck exactly like this). In
   Alpine's diskless model, the services, configs and world would be baked into
   the apkovl by `genapkovl.sh` at build time (the packages already reinstall
   from the cache on every boot). `setup.sh` is really a second, on-device
   build system.
3. **Soloist:** a glibc runtime squashfs on musl, a binary downloaded on the
   device, and the API key on its command line, where it has leaked twice. The
   least defensible part of the repo, for security and for maintenance.
4. **"panda" everywhere.** Hostname, speaker name and admin user are all
   `panda`; paths are `/etc/panda`; services are `panda-*`. With two boards
   this is a real bug: both announce `panda.local` and the same speaker name
   over AirPlay, Spotify Connect and Bluetooth, so they conflict on one network.
5. **`deploy.sh`** pipes a tarball of `/etc` over SSH and runs `setup.sh` as
   root. Fine as a development tool, but nothing records what's actually on a
   device.
6. **Not reproducible:** build timestamps in the kernel and U-Boot, U-Boot
   reports `-dirty` (local patch), and the kernel tarball is checked against
   `sha256sums.asc` without verifying its GPG signature.

## From the BeagleBoard-xM session

- **The U-Boot SDRAM patch**
  (`boards/beaglexm/u-boot/0001-beagle-xm-spl-sdram-size.patch`) works around
  a real upstream bug. It should go to the U-Boot mailing list, though OMAP3
  Beagle support is barely maintained there.
- **The fragment check in `cross-build-kernel.sh`** reimplements
  `merge_config.sh`'s own warnings with grep. Not sending them to `/dev/null`
  would have done most of the job.
- **The deploy rule** (install only packages that are already installed) is a
  heuristic, not a dependency calculation.

## If fixing only three things

1. Get the Soloist API key off its command line.
2. Generate the configs and service setup at build time in `genapkovl.sh`, so
   first boot just boots (and can't get stuck half set up).
3. Replace the version override with a tagged repository, and give each board
   its own hostname and speaker name.
