#!/bin/bash
# Partition and format an SD card for a Raspberry Pi 1 (BCM2835) and install the
# boot files, or build the same layout as an image file.
# Called by boards/<board>/write-sdcard.sh, which sets BOARD.
#
# Usage: sudo boards/<board>/write-sdcard.sh /dev/sdX [staging-dir]
#             (DESTROYS ALL DATA on the card)
#        boards/<board>/write-sdcard.sh --image <file.img> [staging-dir]
#             (no root: a card image, for QEMU or for one dd onto a card)
#   staging-dir: directory whose contents are copied to the FAT partition
#                (optional, normally build/sdimage/<board>).
#
# Layout: MBR (the Pi 1 boot ROM reads only a DOS label), p1 = FAT32 (LBA, type
# 0x0c), boot flag, 512 MiB starting at 4 MiB (sector 8192), the same partition
# as the other boards. Nothing raw: the boot ROM loads bootcode.bin from the
# FAT partition, and the staging files carry the GPU firmware, config.txt,
# the DTBs and overlays (mkimg.rpi1.sh). This script adds u-boot.bin, which
# config.txt starts as the "kernel"; U-Boot finds extlinux by distro boot.
# UBOOT_DIR=<dir> selects another U-Boot build (default build/u-boot-out/<board>).
# A report is written to build/sdcard-report.txt (card) or <file>.report.txt (image).
set -euo pipefail
: "${BOARD:?run boards/<board>/write-sdcard.sh}"
source "$(dirname "$0")/env.sh"
UBOOT_BIN=u-boot.bin
PART_SECTOR=8192       # 4 MiB
PART_MIB=512

IMAGE=""
if [ "${1:-}" = --image ]; then
    IMAGE="${2:?usage: $0 --image <file.img> [staging-dir]}"
    STAGE="${3:-}"
else
    DEV="${1:?usage: $0 /dev/sdX [staging-dir] | --image <file.img> [staging-dir]}"
    STAGE="${2:-}"
fi
UBOOT_OUT="$(realpath "${UBOOT_DIR:-$PROJECT_DIR/build/u-boot-out/$BOARD}")"
[ -f "$UBOOT_OUT/$UBOOT_BIN" ] || { echo "missing $UBOOT_OUT/$UBOOT_BIN, run BOARD=$BOARD scripts/build-u-boot.sh"; exit 1; }
[ -z "$STAGE" ] || [ -d "$STAGE" ] || { echo "$STAGE is not a directory"; exit 1; }
# Without the firmware and config.txt the card doesn't boot at all.
if [ -n "$STAGE" ]; then
    for f in bootcode.bin start.elf fixup.dat config.txt; do
        [ -f "$STAGE/$f" ] || { echo "$STAGE has no $f (build-image.sh for BOARD=$BOARD)"; exit 1; }
    done
fi
# FAT can't hold symlinks (cp would fail halfway on a card).
if [ -n "$STAGE" ] && [ -n "$(find "$STAGE" -type l -print -quit)" ]; then
    echo "$STAGE contains symlinks (FAT can't store them):"; find "$STAGE" -type l; exit 1
fi

partition() {  # <device or file>
    sfdisk -q "$1" <<SFDISK
label: dos
start=$PART_SECTOR, size=${PART_MIB}MiB, type=c, bootable
SFDISK
}
report_common() {  # <device or file>
    sfdisk -d "$1"
    grep -a -m1 -o "U-Boot 20[0-9.]*[^ ]*" "$UBOOT_OUT/$UBOOT_BIN"
}

# --- Image file (no root): sfdisk on the file, mkfs/mtools at the offset ----
if [ -n "$IMAGE" ]; then
    for t in sfdisk mkfs.vfat mcopy mdir; do
        command -v "$t" >/dev/null || { echo "needs $t (dosfstools, mtools)"; exit 1; }
    done
    [ ! -b "$IMAGE" ] || { echo "$IMAGE is a block device: use the card mode (sudo, no --image)"; exit 1; }
    rm -f "$IMAGE"
    truncate -s $(( PART_SECTOR * 512 + PART_MIB * 1024 * 1024 )) "$IMAGE"
    partition "$IMAGE"
    mkfs.vfat -F 32 -n "$BOOT_LABEL" --offset $PART_SECTOR "$IMAGE" $(( PART_MIB * 1024 )) >/dev/null
    export MTOOLS_SKIP_CHECK=1
    FAT="$IMAGE@@$(( PART_SECTOR * 512 ))"
    if [ -n "$STAGE" ]; then
        shopt -s dotglob nullglob
        entries=("$STAGE"/*)
        shopt -u dotglob nullglob
        [ ${#entries[@]} -eq 0 ] || mcopy -s -Q -i "$FAT" "${entries[@]}" ::/
    fi
    mcopy -Q -o -i "$FAT" "$UBOOT_OUT/$UBOOT_BIN" ::/"$UBOOT_BIN"
    REPORT="$IMAGE.report.txt"
    {
        echo "# $(date -Is) $IMAGE  UBOOT_DIR=$UBOOT_OUT"
        report_common "$IMAGE"
        mcopy -Q -i "$FAT" ::/"$UBOOT_BIN" - | cmp - "$UBOOT_OUT/$UBOOT_BIN" && echo "FAT $UBOOT_BIN: matches"
        minfo -i "$FAT" :: | grep -E "^(disk type|sectors per cluster|big size|volume label)" || true
        mdir -i "$FAT" -a ::/
    } > "$REPORT" 2>&1
    cat "$REPORT"
    echo "done: $IMAGE (write it with: sudo dd if=$IMAGE of=/dev/sdX bs=4M conv=fsync)"
    exit 0
fi

# --- Card (root) -------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
[ -b "$DEV" ] || { echo "$DEV is not a block device"; exit 1; }
[ "$(lsblk -dno TYPE "$DEV")" = disk ] || { echo "$DEV is not a whole disk"; exit 1; }

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
# Clear what an old layout left before p1 (another board's raw bootloader).
dd if=/dev/zero of="$DEV" bs=512 seek=1 count=$(( PART_SECTOR - 1 )) conv=fsync,notrunc status=none
partition "$DEV"
partprobe "$DEV"; udevadm settle
PART=$(lsblk -lno PATH "$DEV" | sed -n 2p)
for p in $(lsblk -lno PATH "$DEV" | tail -n +2); do umount "$p" 2>/dev/null || true; done
mkfs.vfat -F 32 -n "$BOOT_LABEL" "$PART" >/dev/null

MNT=$(mktemp -d)
trap 'umount "$MNT" 2>/dev/null; rmdir "$MNT"' EXIT
mount "$PART" "$MNT"
[ -z "$STAGE" ] || cp -r "$STAGE"/. "$MNT"/
cp "$UBOOT_OUT/$UBOOT_BIN" "$MNT/$UBOOT_BIN"
sync
umount "$MNT"

# Report: read everything back and compare.
REPORT="$PROJECT_DIR/build/sdcard-report.txt"
{
    echo "# $(date -Is) $DEV  UBOOT_DIR=$UBOOT_OUT"
    report_common "$DEV"
    blkid "$PART"
    fsck.fat -n -v "$PART" 2>&1 | head -20 || true
    mount -o ro "$PART" "$MNT"
    cmp "$UBOOT_OUT/$UBOOT_BIN" "$MNT/$UBOOT_BIN" && echo "FAT $UBOOT_BIN: matches"
    ls -la --time-style=+ "$MNT"
    umount "$MNT"
} > "$REPORT" 2>&1
[ -n "${SUDO_UID:-}" ] && chown "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$REPORT"
cat "$REPORT"
echo "done: $DEV"
