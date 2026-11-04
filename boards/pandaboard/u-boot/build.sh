#!/bin/bash
# Build U-Boot v2024.07 for the PandaBoard (the last release with omap4_panda).
# Output: build/u-boot-out/{MLO,u-boot.img}
# Uses the host's cross compiler (Fedora gcc-arm-linux-gnu).
set -euo pipefail
BOARD="$(basename "$(cd "$(dirname "$0")/.." && pwd)")"   # this script's board
source "$(dirname "$0")/../../../scripts/env.sh"
export ARCH=arm CROSS_COMPILE="${CROSS_COMPILE:-arm-linux-gnu-}"
UBOOT_TAG=v2024.07
SRC="$PROJECT_DIR/build/src/u-boot-${UBOOT_TAG#v}"
OUT="$PROJECT_DIR/build/u-boot-out"

[ -d "$SRC" ] || git clone --depth 1 --branch "$UBOOT_TAG" \
    https://source.denx.de/u-boot/u-boot.git "$SRC"

make -C "$SRC" -s O="$OUT" omap4_panda_defconfig
# Host crypto tools need OpenSSL ENGINE headers (not shipped by Fedora's
# openssl-devel). The Panda (GP device) doesn't need signed images.
"$SRC/scripts/config" --file "$OUT/.config" -d TOOLS_LIBCRYPTO -d TOOLS_KWBIMAGE
make -C "$SRC" -s O="$OUT" olddefconfig
make -C "$SRC" O="$OUT" -j"$(nproc)"
ls -l "$OUT/MLO" "$OUT/u-boot.img"
