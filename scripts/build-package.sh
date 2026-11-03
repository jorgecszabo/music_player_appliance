#!/bin/bash
# Build one of our aports in the armv7 container with abuild.
# Usage: scripts/build-package.sh <name>     (e.g. linux-pandaboard)
# Output: packages/main/armv7/*.apk + signed APKINDEX
# Runs as container root (abuild -F): setuid helpers can't work under
# qemu-user, and in rootless podman root maps to the host user anyway.
set -euo pipefail
source "$(dirname "$0")/env.sh"
PKG="${1:?usage: $0 <aport name>}"
[ -f "$PROJECT_DIR/aports/main/$PKG/APKBUILD" ] || { echo "no aports/main/$PKG/APKBUILD"; exit 1; }
mkdir -p "$PROJECT_DIR/packages" "$PROJECT_DIR/build/distfiles"
ALPINE_ROOT=1 "$PROJECT_DIR/scripts/alpine.sh" sh -euc "
    cp /root/.abuild/*.rsa.pub /etc/apk/keys/
    cd /work/aports/main/$PKG
    export SRCDEST=/work/build/distfiles REPODEST=/work/packages
    grep -q '^sha512sums=\"\"' APKBUILD && abuild -F checksum
    abuild -F -r
"
ls -l "$PROJECT_DIR/packages/main/armv7/"
