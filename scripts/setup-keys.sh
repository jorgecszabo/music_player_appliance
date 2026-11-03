#!/bin/bash
# Generate the abuild signing key (once). The public key goes into the image
# so the board trusts our locally built packages.
set -euo pipefail
source "$(dirname "$0")/env.sh"
if ls "$PROJECT_DIR"/keys/*.rsa >/dev/null 2>&1; then
    echo "abuild key already exists:"; ls "$PROJECT_DIR"/keys/*.rsa.pub; exit 0
fi
"$PROJECT_DIR/scripts/alpine.sh" sh -c 'abuild-keygen -n -a && cat ~/.abuild/abuild.conf'
