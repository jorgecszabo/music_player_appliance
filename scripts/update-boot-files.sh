#!/bin/bash
# Copy a staging directory onto the SD card's FAT boot partition (label
# $BOOT_LABEL from board.conf) without reformatting or sudo (mounts via udisks).
# Raw-mode U-Boot copies are not touched.
# Usage: scripts/update-boot-files.sh [staging-dir]   (default build/sdimage/<board>)
set -euo pipefail
source "$(dirname "$0")/env.sh"
STAGE="${1:-$PROJECT_DIR/build/sdimage/$BOARD}"
PART=$(blkid -L "$BOOT_LABEL" 2>/dev/null || lsblk -lnpo PATH,LABEL | awk -v l="$BOOT_LABEL" '$2==l{print $1; exit}')
[ -n "$PART" ] || { echo "no partition labelled $BOOT_LABEL found (is the card in the reader?)"; exit 1; }
MNT=$(lsblk -no MOUNTPOINT "$PART")
if [ -z "$MNT" ]; then
    udisksctl mount -b "$PART" >/dev/null
    MNT=$(lsblk -no MOUNTPOINT "$PART")
fi
cp -rv --no-preserve=mode,ownership "$STAGE"/. "$MNT"/
sync
ls -lR "$MNT"
udisksctl unmount -b "$PART"
echo "updated $PART from $STAGE; card can be removed"
