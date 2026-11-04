#!/bin/bash
# Partition and format an SD card for this board and install the boot files
# (MLO + u-boot.img in raw mode and on the FAT partition). DESTROYS ALL DATA.
# Usage: sudo boards/pandaboard/write-sdcard.sh /dev/sdX [staging-dir]
# See scripts/write-sdcard-omap.sh.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOARD="$(basename "$here")" exec "$here/../../scripts/write-sdcard-omap.sh" "$@"
