#!/bin/bash
# Build one of our aports with abuild in the armv7 container.
# Usage: scripts/build-package.sh <name>     (e.g. linux-pandaboard)
# Output: packages/main/armv7/*.apk + signed APKINDEX
#
# - If the aport has a cross-build.sh, it runs first on the x86_64 host (in the
#   cross container) and stages prebuilt files for the APKBUILD to package.
# - abuild runs on a copy in build/abuild/main/<name>, so build leftovers
#   (src/, pkg/) stay out of the repo, and a running build isn't wiped.
# - Runs as container root (abuild -F): setuid helpers can't work under
#   qemu-user, and in rootless podman root maps to the host user anyway.
set -euo pipefail
source "$(dirname "$0")/env.sh"
PKG="${1:?usage: $0 <aport name>}"
APORT="$PROJECT_DIR/aports/main/$PKG"
[ -f "$APORT/APKBUILD" ] || { echo "no aports/main/$PKG/APKBUILD"; exit 1; }

if [ -x "$APORT/cross-build.sh" ]; then
    echo ">>> $PKG: cross-build.sh"
    "$APORT/cross-build.sh"
fi

WORK="$PROJECT_DIR/build/abuild/main/$PKG"
rm -rf "$WORK"; mkdir -p "$WORK"
( cd "$APORT" && tar --exclude=./src --exclude=./pkg -cf - . ) | tar -C "$WORK" -xf -
mkdir -p "$PROJECT_DIR/packages" "$PROJECT_DIR/build/distfiles"

ALPINE_ROOT=1 "$PROJECT_DIR/scripts/alpine.sh" sh -euc "
    cp /root/.abuild/*.rsa.pub /etc/apk/keys/
    apk update -q
    cd /work/build/abuild/main/$PKG
    export SRCDEST=/work/build/distfiles REPODEST=/work/packages
    grep -q '^sha512sums=\"\"' APKBUILD && abuild -F checksum
    abuild -F -r
"
# Keep checksums that abuild filled in.
cmp -s "$WORK/APKBUILD" "$APORT/APKBUILD" || cp "$WORK/APKBUILD" "$APORT/APKBUILD"
ls -l "$PROJECT_DIR/packages/main/armv7/"
