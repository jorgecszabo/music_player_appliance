#!/bin/bash
# Smoke-boot the pcDuino card image in QEMU (no hardware, no root): QEMU's
# "cubieboard" machine emulates the same SoC (Allwinner A10: UART0, timers,
# EMAC, SD, DRAM controller, I2C + AXP209), so the pcDuino's own SPL, U-Boot,
# kernel and DTB boot on it. What it can't tell: audio, USB devices, Bluetooth,
# the watchdog reset, LEDs/buttons, real timing. QEMU's SID fuses are blank, so
# U-Boot finds no MAC address (Linux picks a random one); the board has one.
#
# Usage: scripts/qemu-pcduino.sh [seconds]   (default: until QEMU exits; Ctrl-A X quits)
#   Builds build/qemu-pcduino/card.img from build/sdimage/pcduino with the card
#   writer's image mode (padded to 1 GiB: QEMU wants a power-of-2 SD size),
#   runs QEMU in a container, and logs the serial console to
#   build/qemu-pcduino/boot.log. Forwards localhost:2222 -> SSH, :8080 -> web page.
#   KEEP=1 reuses the existing card.img (keeps the state of the last boot).
set -euo pipefail
export BOARD=pcduino
source "$(dirname "$0")/env.sh"
SECS="${1:-}"
OUT="$PROJECT_DIR/build/qemu-pcduino"
IMAGE="localhost/panda-qemu:44"
mkdir -p "$OUT"
podman image exists "$IMAGE" || podman build --platform linux/amd64 -t "$IMAGE" \
    -f "$PROJECT_DIR/container/Containerfile.qemu" "$PROJECT_DIR/container"
if [ -z "${KEEP:-}" ] || [ ! -f "$OUT/card.img" ]; then
    [ -d "$PROJECT_DIR/build/sdimage/$BOARD" ] || { echo "build the image first: BOARD=$BOARD scripts/build-image.sh"; exit 1; }
    "$BOARD_DIR/write-sdcard.sh" --image "$OUT/card.img" "$PROJECT_DIR/build/sdimage/$BOARD" >/dev/null
    truncate -s 1G "$OUT/card.img"
fi
rm -f "$OUT/boot.log"
run=(qemu-system-arm -M cubieboard -m 512 -nographic
     -drive if=sd,format=raw,file=/q/card.img
     -nic user,model=allwinner-emac,hostfwd=tcp:0.0.0.0:2222-:22,hostfwd=tcp:0.0.0.0:8080-:80)
# (-nographic: serial console + monitor on stdio; adding -serial mon:stdio silences it.)
[ -z "$SECS" ] || run=(timeout "$SECS" "${run[@]}")
# --init: timeout(1) fails (exit 125, silently) as the container's PID 1.
tty=(); [ -t 0 ] && tty=(-it)
podman run --rm --init "${tty[@]}" -p 127.0.0.1:2222:2222 -p 127.0.0.1:8080:8080 \
    -v "$OUT:/q:z" "$IMAGE" "${run[@]}" | tee "$OUT/boot.log" || true
echo "serial log: $OUT/boot.log"
