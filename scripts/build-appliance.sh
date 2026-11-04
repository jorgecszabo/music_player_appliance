#!/bin/bash
# Build the panda-appliance package (appliance/rootfs + panda-board-<board>
# subpackages for every board) into packages/main/<arch>/.
# Usage: [BOARD=<board>] scripts/build-appliance.sh
# The version is a UTC timestamp (newer builds win in the repository); the git
# revision goes into /etc/panda/version next to it.
set -euo pipefail
source "$(dirname "$0")/env.sh"
mkdir -p "$PROJECT_DIR/build"
ver=$(date -u +%Y%m%d.%H%M%S)
git=$(git -C "$PROJECT_DIR" describe --always --dirty 2>/dev/null || echo unknown)
echo "$ver $git" > "$PROJECT_DIR/build/panda-appliance.version"
echo ">>> panda-appliance $ver ($git)"
# Old builds of the package would pile up in the repository (and the image).
rm -f "$PROJECT_DIR"/packages/main/"$ALPINE_ARCH"/panda-appliance-[0-9]*.apk \
      "$PROJECT_DIR"/packages/main/"$ALPINE_ARCH"/panda-board-*.apk
"$PROJECT_DIR/scripts/build-package.sh" panda-appliance
