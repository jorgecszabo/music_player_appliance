#!/bin/bash
# Cross-build zImage, DTB and modules for quick hardware testing.
# Output: build/linux-out/{arch/arm/boot/zImage, arch/arm/boot/dts/ti/omap/omap4-panda.dtb}
#         build/linux-modules/lib/modules/<ver>/
set -euo pipefail
source "$(dirname "$0")/env.sh"
KVER=6.18.60
SRC="$PROJECT_DIR/build/src/linux-$KVER"
OUT="$PROJECT_DIR/build/linux-out"
[ -f "$OUT/.config" ] || "$PROJECT_DIR/scripts/config-kernel.sh"
make -C "$SRC" O="$OUT" -j"$(nproc)" zImage modules dtbs
rm -rf "$PROJECT_DIR/build/linux-modules"
make -C "$SRC" -s O="$OUT" INSTALL_MOD_PATH="$PROJECT_DIR/build/linux-modules" INSTALL_MOD_STRIP=1 modules_install
ls -l "$OUT/arch/arm/boot/zImage" "$OUT/arch/arm/boot/dts/ti/omap/omap4-panda.dtb"
cat "$OUT/include/config/kernel.release"
