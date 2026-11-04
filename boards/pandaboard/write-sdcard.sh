#!/bin/bash
# Partition and format an SD card for the PandaBoard and install the boot files.
# DESTROYS ALL DATA on the given device.
#
# Usage: sudo boards/pandaboard/write-sdcard.sh /dev/sdX [staging-dir]
#   staging-dir: directory whose contents are copied to the FAT partition
#                after MLO/u-boot.img (optional, normally build/sdimage).
#
# Layout: MBR, p1 = FAT32 (LBA, type 0x0c), boot flag, 512 MiB starting at 1 MiB.
# The boot files go in twice, so either OMAP4 ROM boot method works:
#   raw:  MLO at sector 256 (128 KiB), u-boot.img at sector 768 (384 KiB)
#   FAT:  MLO (copied first) and u-boot.img on p1
# UBOOT_DIR=<dir> selects another MLO/u-boot.img (default build/u-boot-out/<board>).
# A report is written to build/sdcard-report.txt.
set -euo pipefail
BOARD="$(basename "$(cd "$(dirname "$0")" && pwd)")"   # this script's board
source "$(dirname "$0")/../../scripts/env.sh"
DEV="${1:?usage: $0 /dev/sdX [staging-dir]}"
STAGE="${2:-}"
UBOOT_OUT="$(realpath "${UBOOT_DIR:-$PROJECT_DIR/build/u-boot-out/$BOARD}")"

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
[ -b "$DEV" ] || { echo "$DEV is not a block device"; exit 1; }
[ "$(lsblk -dno TYPE "$DEV")" = disk ] || { echo "$DEV is not a whole disk"; exit 1; }
for f in MLO u-boot.img; do
    [ -f "$UBOOT_OUT/$f" ] || { echo "missing $UBOOT_OUT/$f, run scripts/build-u-boot.sh"; exit 1; }
done

# Safety: refuse disks that host mounted system filesystems, and non-removable
# disks unless they are on USB or a built-in SD slot (mmcblk; readers often report RM=0).
name=$(basename "$DEV")
rm=$(cat /sys/block/"$name"/removable)
tran=$(lsblk -dno TRAN "$DEV")
if [ "$rm" != 1 ] && [ "$tran" != usb ] && [[ "$name" != mmcblk* ]]; then
    echo "$DEV is neither removable nor USB ($tran), refusing"; exit 1
fi
if lsblk -no MOUNTPOINT "$DEV" | grep -qE '^/(boot|home|usr|var)?$'; then
    echo "$DEV holds a system mount, refusing"; exit 1
fi

lsblk -o NAME,SIZE,TRAN,RM,MODEL,FSTYPE,LABEL,MOUNTPOINT "$DEV"
read -r -p "ALL DATA on $DEV will be destroyed. Type the device name ($name) to continue: " ans
[ "$ans" = "$name" ] || { echo "aborted"; exit 1; }

# Unmount anything mounted from the card (desktop automount).
for p in $(lsblk -lno PATH "$DEV" | tail -n +2); do umount "$p" 2>/dev/null || true; done

wipefs -a "$DEV" >/dev/null
sfdisk -q "$DEV" <<SFDISK
label: dos
start=2048, size=512MiB, type=c, bootable
SFDISK
partprobe "$DEV"; udevadm settle
PART=$(lsblk -lno PATH "$DEV" | sed -n 2p)
for p in $(lsblk -lno PATH "$DEV" | tail -n +2); do umount "$p" 2>/dev/null || true; done
mkfs.vfat -F 32 -n "$BOOT_LABEL" "$PART" >/dev/null

MNT=$(mktemp -d)
trap 'umount "$MNT" 2>/dev/null; rmdir "$MNT"' EXIT
mount "$PART" "$MNT"
# Copy MLO first: older OMAP boot ROMs expect it early in the root directory.
cp "$UBOOT_OUT/MLO" "$MNT/MLO"; sync
cp "$UBOOT_OUT/u-boot.img" "$MNT/u-boot.img"
if [ -n "$STAGE" ]; then cp -r "$STAGE"/. "$MNT"/; fi
sync
umount "$MNT"

# Raw-mode copies in the gap before p1 (sector 2048). u-boot.img must end
# before the partition: (2048 - 768) * 512 = 640 KiB available.
[ "$(stat -c %s "$UBOOT_OUT/u-boot.img")" -le $(( (2048 - 768) * 512 )) ] \
    || { echo "u-boot.img too large for raw slot"; exit 1; }
dd if="$UBOOT_OUT/MLO" of="$DEV" bs=512 seek=256 conv=fsync,notrunc status=none
dd if="$UBOOT_OUT/u-boot.img" of="$DEV" bs=512 seek=768 conv=fsync,notrunc status=none
sync

# Report: read everything back and compare.
REPORT="$PROJECT_DIR/build/sdcard-report.txt"
{
    echo "# $(date -Is) $DEV  UBOOT_DIR=$UBOOT_OUT"
    sfdisk -d "$DEV"
    blkid "$PART"
    fsck.fat -n -v "$PART" 2>&1 | head -20 || true
    mount -o ro "$PART" "$MNT"
    ls -la --time-style=+ "$MNT"
    for f in MLO u-boot.img; do
        cmp "$UBOOT_OUT/$f" "$MNT/$f" && echo "FAT $f: matches"
    done
    umount "$MNT"
    cmp -n "$(stat -c %s "$UBOOT_OUT/MLO")" "$UBOOT_OUT/MLO" <(dd if="$DEV" bs=512 skip=256 status=none) \
        && echo "raw MLO @256: matches"
    cmp -n "$(stat -c %s "$UBOOT_OUT/u-boot.img")" "$UBOOT_OUT/u-boot.img" <(dd if="$DEV" bs=512 skip=768 status=none) \
        && echo "raw u-boot.img @768: matches"
    strings "$UBOOT_OUT/MLO" | grep -m1 "U-Boot SPL"
} > "$REPORT" 2>&1
[ -n "${SUDO_UID:-}" ] && chown "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$REPORT"
cat "$REPORT"
echo "done: $DEV"
