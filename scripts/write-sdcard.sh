#!/bin/bash
# Partition and format an SD card for the PandaBoard and install the boot files.
# DESTROYS ALL DATA on the given device.
#
# Usage: sudo scripts/write-sdcard.sh /dev/sdX [staging-dir]
#   staging-dir: directory whose contents are copied to the FAT partition
#                after MLO/u-boot.img (optional, e.g. the Alpine image).
#
# Layout: MBR, p1 = FAT32 (LBA, type 0x0c), boot flag, 512 MiB starting at 1 MiB.
set -euo pipefail
source "$(dirname "$0")/env.sh"
DEV="${1:?usage: $0 /dev/sdX [staging-dir]}"
STAGE="${2:-}"
UBOOT_OUT="$PROJECT_DIR/build/u-boot-out"

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
[ -b "$DEV" ] || { echo "$DEV is not a block device"; exit 1; }
[ "$(lsblk -dno TYPE "$DEV")" = disk ] || { echo "$DEV is not a whole disk"; exit 1; }
for f in MLO u-boot.img; do
    [ -f "$UBOOT_OUT/$f" ] || { echo "missing $UBOOT_OUT/$f, run scripts/build-uboot.sh"; exit 1; }
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
mkfs.vfat -F 32 -n PANDABOOT "$PART" >/dev/null

MNT=$(mktemp -d)
trap 'umount "$MNT" 2>/dev/null; rmdir "$MNT"' EXIT
mount "$PART" "$MNT"
# Copy MLO first: older OMAP boot ROMs expect it early in the root directory.
cp "$UBOOT_OUT/MLO" "$MNT/MLO"; sync
cp "$UBOOT_OUT/u-boot.img" "$MNT/u-boot.img"
if [ -n "$STAGE" ]; then cp -r "$STAGE"/. "$MNT"/; fi
sync
ls -la "$MNT"
echo "done: $PART"
