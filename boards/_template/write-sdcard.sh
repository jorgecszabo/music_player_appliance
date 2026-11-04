#!/bin/bash
# Card writer TEMPLATE: partition the card, make the FAT boot partition labelled
# $BOOT_LABEL, copy build/sdimage/ onto it, and install the bootloader the way
# this board's ROM expects (see boards/pandaboard/write-sdcard.sh: raw-mode MLO
# and u-boot.img for OMAP4). Must refuse to touch anything that isn't a
# removable card.
echo "boards/<board>/write-sdcard.sh: not written yet for this board" >&2
exit 1
