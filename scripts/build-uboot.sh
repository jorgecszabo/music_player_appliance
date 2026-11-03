#!/bin/bash
# Build U-Boot v2024.07 for the PandaBoard (the last release with omap4_panda).
# Output: build/u-boot-out/{MLO,u-boot.img}
set -euo pipefail
source "$(dirname "$0")/env.sh"
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
