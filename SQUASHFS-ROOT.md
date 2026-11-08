# Squashfs root: run the system from the card, not from RAM

Status: implemented 2026-11-08, not yet tested on a board (see "Testing on a board" below).
Applies to every board; the Raspberry Pi 1 (512 MB) gains the most.

**Cards are rewritten.** There is no update path from a card with `apks/` slots and no code
for one: such a card refuses the new bundles (`unsupported bundle format 2`). Write it again.

## Why

Before: every boot ran `apk add` from the slot's repository (`slots/<x>/apks/`) into a tmpfs
root, so the whole installed system sat in RAM and couldn't be evicted (PLAN.md, Phase 4
reboot test: 37 MB used plus 232 MB tmpfs/shared at idle on the Panda).

Now each slot carries a read-only **`rootfs.squashfs`** built on the build host. The initramfs
loop-mounts it from the FAT partition, puts an overlayfs over it with a tmpfs as the writable
layer, unpacks the settings overlay (apkovl) on top and switches root. File pages are page
cache: the kernel can drop them and read them again from the card. Also: no package install
at boot (faster, most of all on the Pi 1), the image that was built is the one that boots, and
package install scripts run once at build time.

Unchanged: the A/B slots, the bundle signature and streaming install, trial boot and rollback,
the settings overlay and `lbu`, the modloop, the Soloist runtime squashfs, factory reset,
`scripts/deploy.sh` (it installs into the writable layer; gone at reboot).

## Boot

```
U-Boot / Pi firmware  -> slots/<x>/boot/vmlinuz + initramfs (Alpine's, then our /init)
initramfs /init       -> nlplug-findfs mounts the card ro at /media/<BOOT_MEDIA> (as before)
                      -> on an OTA trial boot of this slot: arm the rollback (below)
                      -> panda_root=/slots/<x>/rootfs.squashfs:
                           squashfs (loop) on /media/root-ro, tmpfs on /media/root-rw,
                           overlay of the two on the new root
                      -> unpack <host>.apkovl.tar.gz over it (settings win)
                      -> no apk add; fstab, console, mount moves, switch_root as before
OpenRC                -> modloop, panda-ota-boot, panda-setup (merges users), ...
```

`/media/root-ro` and `/media/root-rw` are the names of Alpine's own `overlaytmpfs` mode; the
init moves every mount into the new root, so the running system sees them there too.

Card layout, changes only:

```
slots/<x>/boot/initramfs-<flavor>  mkimage's + a second archive with our /init
slots/<x>/rootfs.squashfs          the installed system (the world of the factory overlay)
slots/<x>/apks/                    gone
<file>.armed                       during an activated trial only (below)
```

Boot entry options: `modloop=/slots/<x>/boot/modloop-<flavor>
panda_root=/slots/<x>/rootfs.squashfs panda_slot=<x> panic=10`.

Sizes (Panda): 157 MB installed, **65 MB** image (zstd), against 71 MB of `apks/` before.

## The pieces

### Root image: `scripts/build-rootfs.sh`

Called by `build-image.sh` after mkimage. In the target-arch container as container root
(package scripts run under qemu-user as on the board; the tree stays inside the container,
system-user-owned files never land on the host):
- `apk add --root --initdb` from **the slot's repository only**, the world taken from the
  factory overlay's `etc/apk/world` (one source: genapkovl.sh). apk's exit status fails the
  build. The one expected warning: busybox can't create device nodes (`/dev` is emptied
  anyway; devtmpfs at boot).
- `/etc/apk/repositories` empty (setup.sh adds the online ones), our public key in
  `/etc/apk/keys` (deploy.sh installs packages signed with it), no apk log or cache, empty
  `/dev`, an empty `/boot` checked, a few files that must exist checked.
- `mksquashfs -comp zstd -b 128K`, ownership as installed, file times from the git commit,
  the container's SELinux labels left out (`-xattrs-exclude ^security.selinux$`).

zstd, not xz: xz is smaller but slow to decompress on the 1 GHz ARMv6, and each page fault
decompresses a whole block.

### Initramfs: `appliance/initramfs/initramfs-init.patch`

A patch against Alpine 3.24's mkinitfs init (`VERSION=3.14.1-r0`; `INIT_VERSION` in
build-rootfs.sh). Another version stops the build: refresh the patch and boot-test it.
build-rootfs.sh applies it to the container's `/usr/share/mkinitfs/initramfs-init` and
appends it as a second `cpio.gz` to the slot's initramfs: the kernel unpacks concatenated
archives in order and the later `/init` wins. No mkinitfs fork. `loop`, `squashfs` and
`overlay` are in mkinitfs's `base` feature already.

What the patch does, all behind `panda_root=` (without it, Alpine's diskless boot is
untouched):
- New options `panda_root` and `panda_slot`.
- `mount_panda_root` replaces the tmpfs root: finds the medium (the `/media/*` with
  `slots/`), arms the rollback, mounts image, tmpfs and overlay. A missing or broken image:
  a console message, then `recovery_shell`, which with `panic=10` is a reboot.
- Skips `apk add --initdb` before the apkovl and the package install block. A tmpfs size in
  the overlay's fstab applies to the writable layer.

### Rollback armed in the initramfs

Before, a trial boot was armed by `panda-ota-boot`, in userspace. A failure in the initramfs
(now: mounting the image) would have rebooted into the same trial slot forever. Now:
- `panda-ota.py activate` writes the boot config that boots the confirmed slot next to the
  live one: `extlinux/extlinux.conf.armed` and `boot/extlinux/extlinux.conf.armed`, or
  `slot.txt.armed` (rpi). Every other `write_boot()` removes stale `*.armed` files.
- The init, when `slots/state` says `trial=<panda_slot>`, remounts the card rw, renames the
  `*.armed` files over the live ones, syncs, remounts ro. Before it touches the image.
- `panda-ota-boot` still arms too (and cleans up). `panda-ota-confirm` makes the new slot the
  default once healthy, as before.

A kernel that dies before the initramfs remains PLAN.md's Level 2 (U-Boot bootcount).

### Users and groups: the image's

The settings overlay carries saved copies of `passwd`, `group` and `shadow`, which replace
the image's at boot. The image's uids are fixed at build time (install order), and its files
are owned by them. So the image wins, and the overlay keeps what is the owner's:
- `/etc/panda/merge-users.py /media/root-ro/etc /etc`, first thing in `setup.sh` (panda-setup,
  boot runlevel, before the services that use those users): passwd = the image's entries, then
  saved-only ones (the admin user); group = the image's with saved extra members, then
  saved-only groups; shadow = the saved entry where there is one (passwords), else the image's.
  Writes only on a change. A saved user sharing a uid with an image user is logged.
- The service users (`speaker`, `panda-web`, `panda-lcd`) are created by panda-appliance's
  pre-install, so they are in the image (before: setup.sh at every boot, with whatever uid was
  free on the board). setup.sh `chown -R`s their state directories, since saved files come
  back with the uid they were saved with.

### `panda-ota.py`

Bundle `FORMAT = 2`, the only one accepted; a bundle needs `boot/` and `rootfs.squashfs`
(`apks/` is no longer an allowed path). Boot entries get `panda_root=` instead of
`alpine_repo=`. `cmd_boot` no longer points `/etc/apk/repositories` at the slot.

## Tests (host)

- `tests/unit/test_ota.py`: format 2 bundles, `panda_root=` entries, the armed files (written by
  activate, removed by the trial boot and by clearing a trial; rpi `slot.txt.armed`).
- `tests/unit/test_merge_users.py`: the merge rules, file modes, shadow never readable.
- `scripts/test-ota.sh`: against a built image (root image installed, `panda_root=`, armed).
- QEMU (`scripts/qemu-pcduino.sh`, `scripts/qemu-rpi1.sh`): see "Results" below.

## Testing on a board

- Boots; `findmnt /` is the overlay; `/media/root-ro`, `/media/root-rw` mounted; `free`:
  shared memory far below the old ~230 MB.
- `lbu status` lists settings only; `lbu commit`, reboot, settings kept.
- Modules and firmware load (modloop); Bluetooth, Wi-Fi, audio, the web page.
- `scripts/deploy.sh`, then a reboot: the slot's version is back.
- OTA: install, activate (`*.armed` appear), trial boot (they are gone, `DEFAULT` = old),
  confirm; a failed trial (`touch slots/<x>/FAIL_HEALTH`) rolls back; Revert; factory reset.
- A trial slot with a truncated `rootfs.squashfs` comes back on the old slot by itself.

## Results

2026-11-08, pcDuino image in QEMU (`scripts/qemu-pcduino.sh`), 491 MB guest:
- Boots to login and every service starts (Soloist waits for its key). Root: the overlay;
  `/media/root-ro` the image on `loop0`; modloop on `loop1` with modules and firmware. The
  initramfs goes from mounting the image to the settings in under a second.
- Idle, second boot: **Shmem 2.3 MB** (diskless: ~230 MB), MemAvailable 428 MB.
- Second boot over saved settings: the image's service users (speaker 103, panda-web 104,
  panda-lcd 105) and the saved admin (1000) in `/etc/passwd`; `lbu status` lists only the
  generated configs and `inittab`.
- Trial boot (`trial=a` plus `*.armed` written on the card): the init logged "Trial boot of
  slot a: arming the rollback: ok" before mounting the image; `panda-ota-confirm` then found
  it healthy and cleared the trial.
- Image renamed away: "Mounting root image ...: failed. not found on the boot medium", kernel
  panic, reboot after 10 s (`panic=10`).
- `scripts/test-ota.sh` with a Panda bundle: 23 passed. `scripts/check.sh`: all passed.

Image sizes: Panda and pcDuino 157 MB installed, 65 MB image; Pi 1 199 MB, 71 MB.
