#!/bin/bash
# Partition and format an SD card for this board and install the boot files
# (u-boot-sunxi-with-spl.bin raw at 8 KiB). DESTROYS ALL DATA.
# Usage: sudo boards/pcduino/write-sdcard.sh /dev/sdX [staging-dir]
#        boards/pcduino/write-sdcard.sh --image <file.img> [staging-dir]   (no root)
# See scripts/write-sdcard-sunxi.sh.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOARD="$(basename "$here")" exec "$here/../../scripts/write-sdcard-sunxi.sh" "$@"
