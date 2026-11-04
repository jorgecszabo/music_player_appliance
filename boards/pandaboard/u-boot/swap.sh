#!/bin/bash
# Replace MLO + u-boot.img on an already-written PandaBoard SD card's FAT partition.
# Usage: sudo boards/pandaboard/u-boot/swap.sh /dev/sdX1 <dir-with-MLO-and-u-boot.img>
set -euo pipefail
PART="${1:?usage: $0 /dev/sdX1 dir}"; SRC="${2:?usage: $0 /dev/sdX1 dir}"
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
[ "$(lsblk -no TYPE "$PART")" = part ] || { echo "$PART is not a partition"; exit 1; }
[ "$(lsblk -no FSTYPE "$PART")" = vfat ] || { echo "$PART is not FAT"; exit 1; }
for f in MLO u-boot.img; do [ -f "$SRC/$f" ] || { echo "missing $SRC/$f"; exit 1; }; done
umount "$PART" 2>/dev/null || true
MNT=$(mktemp -d); trap 'umount "$MNT" 2>/dev/null; rmdir "$MNT"' EXIT
mount "$PART" "$MNT"
rm -f "$MNT/MLO" "$MNT/u-boot.img"; sync
cp "$SRC/MLO" "$MNT/MLO"; sync          # MLO first in the directory
cp "$SRC/u-boot.img" "$MNT/u-boot.img"; sync
ls -la "$MNT"; strings "$MNT/MLO" | grep -m1 "U-Boot SPL"
