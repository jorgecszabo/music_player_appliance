#!/bin/bash
# Smoke-boot the rpi1 card image in QEMU (no hardware, no root): QEMU's
# "raspi1ap" machine emulates the BCM2835 (UART0, timers, SD host, DWC2 USB,
# 512 MB). There's no GPU firmware in QEMU, so this script plays its part:
# it applies config.txt's overlays to the DTB (fdtoverlay, as the firmware
# would) and has QEMU load u-boot.bin at 0x8000 with the DTB in r2, which is
# how the firmware starts it. From there it's the card's own boot: U-Boot,
# extlinux (the OTA slots), linux-rpi, initramfs, modloop, apkovl, OpenRC.
# What it can't tell: the overclock, the jack, the TFT/touch/buttons (no SPI
# devices), the Model B's USB Ethernet, the watchdog reset, real timing.
# Networking: a QEMU usb-net device (shows up as usb0, not eth0).
#
# Usage: scripts/qemu-rpi1.sh [seconds]   (default: until QEMU exits; Ctrl-A X quits)
#   Builds build/qemu-rpi1/card.img from build/sdimage/rpi1 with the card
#   writer's image mode (padded to 1 GiB: QEMU wants a power-of-2 SD size), and
#   logs the serial console to build/qemu-rpi1/boot.log. Forwards
#   localhost:2222 -> SSH, :8080 -> web page. KEEP=1 reuses the card.img.
set -euo pipefail
export BOARD=rpi1
source "$(dirname "$0")/env.sh"
SECS="${1:-}"
OUT="$PROJECT_DIR/build/qemu-rpi1"
SD="$PROJECT_DIR/build/sdimage/$BOARD"
IMAGE="localhost/panda-qemu:44"
mkdir -p "$OUT"
podman image exists "$IMAGE" || podman build --platform linux/amd64 -t "$IMAGE" \
    -f "$PROJECT_DIR/container/Containerfile.qemu" "$PROJECT_DIR/container"
[ -d "$SD" ] || { echo "build the image first: BOARD=$BOARD scripts/build-image.sh"; exit 1; }
if [ -z "${KEEP:-}" ] || [ ! -f "$OUT/card.img" ]; then
    "$BOARD_DIR/write-sdcard.sh" --image "$OUT/card.img" "$SD" >/dev/null
    truncate -s 1G "$OUT/card.img"
fi
cp "$PROJECT_DIR/build/u-boot-out/$BOARD/u-boot.bin" "$OUT/u-boot.bin"
# The firmware's part: the Model B rev 2 DTB with config.txt's overlays.
overlays=$(sed -n 's/^dtoverlay=\([^,]*\).*/\1/p' "$SD/config.txt")
args=()
for o in $overlays; do args+=("/sd/overlays/$o.dtbo"); done
podman run --rm -v "$SD:/sd:ro,z" -v "$OUT:/q:z" "$IMAGE" \
    fdtoverlay -i /sd/bcm2708-rpi-b.dtb -o /q/rpi.dtb "${args[@]}"
echo "device tree: bcm2708-rpi-b.dtb + $overlays"

rm -f "$OUT/boot.log"
run=(qemu-system-arm -M raspi1ap -m 512
     -kernel /q/u-boot.bin -dtb /q/rpi.dtb
     -drive if=sd,format=raw,file=/q/card.img
     -device usb-net,netdev=n
     -netdev user,id=n,hostfwd=tcp:0.0.0.0:2222-:22,hostfwd=tcp:0.0.0.0:8080-:80)
# Without a terminal (background runs), QEMU stops when stdin closes: it writes
# the serial log itself instead, with no console.
tty=(); [ -t 0 ] && tty=(-it)
if [ ${#tty[@]} -gt 0 ]; then
    run+=(-nographic)
else
    run+=(-display none -monitor none -serial file:/q/boot.log)
fi
[ -z "$SECS" ] || run=(timeout "$SECS" "${run[@]}")
# --init: timeout(1) fails (exit 125, silently) as the container's PID 1.
if [ ${#tty[@]} -gt 0 ]; then
    podman run --rm --init "${tty[@]}" -p 127.0.0.1:2222:2222 -p 127.0.0.1:8080:8080 \
        -v "$OUT:/q:z" "$IMAGE" "${run[@]}" | tee "$OUT/boot.log" || true
else
    podman run --rm --init -p 127.0.0.1:2222:2222 -p 127.0.0.1:8080:8080 \
        -v "$OUT:/q:z" "$IMAGE" "${run[@]}" </dev/null || true
fi
echo "serial log: $OUT/boot.log"
