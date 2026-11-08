#!/bin/bash
# Grow the boot partition of an already written card in place (slots, settings and
# bootloader stay as they are). Run on the PC with the card in a reader.
#
# Usage: sudo scripts/grow-sdcard.sh /dev/sdX [size-MiB]     (default 1536)
#
# Expects the layout the write-sdcard scripts make: one DOS partition p1, type 0x0c,
# starting at sector 8192, FAT32. Anything else is refused. The old partition table
# sector is saved to build/sdcard-mbr-<name>-<date>.bin first.
# Needs: sfdisk, fatresize, fsck.fat, lsblk, partprobe.
set -euo pipefail
source "$(dirname "$0")/env.sh"
DEV="${1:?usage: $0 /dev/sdX [size-MiB]}"
MIB="${2:-1536}"
START=8192

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
[ -b "$DEV" ] || { echo "$DEV is not a block device"; exit 1; }
[ "$(lsblk -dno TYPE "$DEV")" = disk ] || { echo "$DEV is not a whole disk"; exit 1; }
for t in sfdisk fatresize fsck.fat lsblk partprobe; do
    command -v "$t" >/dev/null || { echo "missing tool: $t"; exit 1; }
done

name=$(basename "$DEV")
rm=$(cat /sys/block/"$name"/removable)
tran=$(lsblk -dno TRAN "$DEV")
if [ "$rm" != 1 ] && [ "$tran" != usb ] && [[ "$name" != mmcblk* ]]; then
    echo "$DEV is neither removable nor USB ($tran), refusing"; exit 1
fi
if lsblk -no MOUNTPOINT "$DEV" | grep -qE '^/(boot|home|usr|var)?$'; then
    echo "$DEV holds a system mount, refusing"; exit 1
fi

# Unmount anything mounted from the card (desktop automount).
for p in $(lsblk -lno PATH "$DEV" | tail -n +2); do umount "$p" 2>/dev/null || true; done

# The layout must be exactly what the write scripts make.
nparts=$(lsblk -lno PATH "$DEV" | tail -n +2 | wc -l)
[ "$nparts" -eq 1 ] || { echo "expected exactly one partition, found $nparts"; exit 1; }
PART=$(lsblk -lno PATH "$DEV" | sed -n 2p)
line=$(sfdisk -d "$DEV" | grep "^$PART ")
echo "$line"
case "$line" in *"start=$START,"*"type=c"*) ;; *)
    echo "p1 is not 'start=$START, type=c', refusing"; exit 1 ;;
esac
fstype=$(lsblk -no FSTYPE "$PART")
[ "$fstype" = vfat ] || { echo "p1 is $fstype, not vfat, refusing"; exit 1; }
if ! fsck.fat -n "$PART" >/dev/null 2>&1; then
    echo "$PART has filesystem errors; fix it first (fsck.fat -a $PART)"; exit 1
fi

cur_sectors=$(blockdev --getsz "$PART")
new_sectors=$(( MIB * 2048 ))
card_sectors=$(blockdev --getsz "$DEV")
[ "$new_sectors" -gt "$cur_sectors" ] || { echo "p1 is already $(( cur_sectors / 2048 )) MiB, nothing to grow"; exit 0; }
[ $(( START + new_sectors )) -le "$card_sectors" ] || {
    echo "$MIB MiB does not fit on the card ($(( (card_sectors - START) / 2048 )) MiB available)"; exit 1; }

lsblk -o NAME,SIZE,TRAN,RM,MODEL,FSTYPE,LABEL "$DEV"
read -r -p "Grow $PART from $(( cur_sectors / 2048 )) to $MIB MiB. Type the device name ($name) to continue: " ans
[ "$ans" = "$name" ] || { echo "aborted"; exit 1; }

mkdir -p "$PROJECT_DIR/build"
BACKUP="$PROJECT_DIR/build/sdcard-mbr-$name-$(date +%Y%m%d-%H%M%S).bin"
dd if="$DEV" of="$BACKUP" bs=512 count=1 status=none
echo "old partition table saved: $BACKUP (restore: dd if=$BACKUP of=$DEV bs=512 count=1)"

# Only p1 changes (same start, bigger size). Then grow the filesystem to fill it.
echo "start=$START, size=${MIB}MiB, type=c, bootable" | sfdisk -q -N 1 --no-reread "$DEV"
partprobe "$DEV"; udevadm settle
for p in $(lsblk -lno PATH "$DEV" | tail -n +2); do umount "$p" 2>/dev/null || true; done
fatresize "$PART"
sync

echo "--- check"
fsck.fat -n -v "$PART" 2>&1 | tail -8
MNT=$(mktemp -d)
trap 'umount "$MNT" 2>/dev/null; rmdir "$MNT"' EXIT
mount -o ro "$PART" "$MNT"
df -h "$MNT"
ls "$MNT" "$MNT/slots" 2>/dev/null
umount "$MNT"
echo "done"
