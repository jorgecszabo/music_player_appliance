#!/bin/bash
# Build U-Boot for the board, as described in its board.conf:
#   UBOOT_TAG        U-Boot release (git tag), e.g. v2024.07
#   UBOOT_DEFCONFIG  e.g. omap4_panda_defconfig
#   UBOOT_FILES      the files the card writer installs, e.g. "MLO u-boot.img"
# Usage: [BOARD=<board>] scripts/build-u-boot.sh
# Output: build/u-boot-out/<board>/ (UBOOT_FILES checked)
# Patches: boards/<board>/u-boot/*.patch, applied in order (git apply). The
# source tree is shared by all boards with the same UBOOT_TAG, so it is reset
# to the tag first and only this board's patches are applied.
# Uses the host's cross compiler (Fedora gcc-arm-linux-gnu); CROSS_COMPILE
# overrides it.
set -euo pipefail
source "$(dirname "$0")/env.sh"
: "${UBOOT_TAG:?not set in boards/$BOARD/board.conf}"
: "${UBOOT_DEFCONFIG:?not set in boards/$BOARD/board.conf}"
: "${UBOOT_FILES:?not set in boards/$BOARD/board.conf}"
export ARCH=arm CROSS_COMPILE="${CROSS_COMPILE:-arm-linux-gnu-}"
SRC="$PROJECT_DIR/build/src/u-boot-${UBOOT_TAG#v}"
OUT="$PROJECT_DIR/build/u-boot-out/$BOARD"

[ -d "$SRC" ] || git clone --depth 1 --branch "$UBOOT_TAG" \
    https://source.denx.de/u-boot/u-boot.git "$SRC"
# (Out-of-tree build: the tree holds no build outputs, only our patches.)
git -C "$SRC" checkout -q -- .
for p in "$BOARD_DIR"/u-boot/*.patch; do
    [ -f "$p" ] || continue
    echo ">>> applying ${p#"$PROJECT_DIR"/}"
    git -C "$SRC" apply "$p"
done

make -C "$SRC" -s O="$OUT" "$UBOOT_DEFCONFIG"
# Host crypto tools need OpenSSL ENGINE headers (not shipped by Fedora's
# openssl-devel). Our boards (GP devices) don't need signed images.
"$SRC/scripts/config" --file "$OUT/.config" -d TOOLS_LIBCRYPTO -d TOOLS_KWBIMAGE
make -C "$SRC" -s O="$OUT" olddefconfig
make -C "$SRC" O="$OUT" -j"$(nproc)"
cd "$OUT" && ls -l $UBOOT_FILES
