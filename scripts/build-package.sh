#!/bin/bash
# Build one of our aports with abuild in the board's target-arch container.
# Usage: [BOARD=<board>] scripts/build-package.sh <name>   (e.g. linux-pandaboard)
# Output: packages/main/<arch>/*.apk + signed APKINDEX
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
# Board-specific aports (boards/<board>/aports/) win over generic ones (aports/).
APORT=""
for d in "$BOARD_DIR/aports/$PKG" "$PROJECT_DIR/aports/$PKG"; do
    if [ -f "$d/APKBUILD" ]; then APORT="$d"; break; fi
done
[ -n "$APORT" ] || { echo "no APKBUILD for $PKG in boards/$BOARD/aports/ or aports/"; exit 1; }

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
# Keep checksums that abuild filled in (only then: copying back otherwise
# would undo edits made to the aport while it was building).
if grep -q '^sha512sums=""' "$APORT/APKBUILD" && ! grep -q '^sha512sums=""' "$WORK/APKBUILD"; then
    cp "$WORK/APKBUILD" "$APORT/APKBUILD"
fi
ls -l "$PROJECT_DIR/packages/main/$ALPINE_ARCH/"
