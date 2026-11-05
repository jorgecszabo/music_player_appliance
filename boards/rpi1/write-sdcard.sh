#!/bin/bash
# Partition and format an SD card for this board and install the boot files
# (GPU firmware, config.txt, u-boot.bin on the FAT partition). DESTROYS ALL DATA.
# Usage: sudo boards/rpi1/write-sdcard.sh /dev/sdX [staging-dir]
#        boards/rpi1/write-sdcard.sh --image <file.img> [staging-dir]   (no root)
# See scripts/write-sdcard-rpi.sh.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOARD="$(basename "$here")" exec "$here/../../scripts/write-sdcard-rpi.sh" "$@"
