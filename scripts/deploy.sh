#!/bin/bash
# Try the current appliance code on a running device, without an update: build
# panda-appliance, install it into the running system (RAM) and run setup.sh.
# Not persistent: the next boot runs the slot's version again. To keep it, build
# and install an update (scripts/build-ota.sh, then the web page or ota-install).
# Usage: [BOARD=<board>] [PANDA_HOST=panda@panda.local] scripts/deploy.sh [--restart]
#   --restart  also restart the audio services
#
# Never copy appliance/rootfs over /etc on the device: those files belong to
# the package, and modified copies would be saved by lbu and shadow every later
# version (files in the overlay win over package files at boot).
set -euo pipefail
source "$(dirname "$0")/env.sh"
HOST="${PANDA_HOST:-panda@panda.local}"
RESTART="${1:-}"

"$PROJECT_DIR/scripts/build-appliance.sh" >/dev/null
REPO="$PROJECT_DIR/packages/main/$ALPINE_ARCH"
APKS=$(ls "$REPO"/panda-appliance-[0-9]*.apk "$REPO"/panda-board-"$BOARD"-[0-9]*.apk)
echo ">>> deploying $(basename "$(ls "$REPO"/panda-appliance-[0-9]*.apk)") to $HOST (live, not persistent)"
ssh -o BatchMode=yes "$HOST" 'rm -rf /tmp/panda-deploy && mkdir -p /tmp/panda-deploy'
scp -q $APKS "$HOST":/tmp/panda-deploy/
# apk add <file> pins the world to that file: keep the world as it was, so the
# saved world stays "panda-appliance panda-board-<board>".
ssh -o BatchMode=yes "$HOST" 'doas sh -euc "
    cp /etc/apk/world /tmp/panda-deploy/world
    apk add --no-network --quiet --force-non-repository /tmp/panda-deploy/*.apk || rc=\$?
    cp /tmp/panda-deploy/world /etc/apk/world
    [ -z \"\${rc:-}\" ] || exit \$rc
    sh /etc/panda/setup.sh
    rm -rf /tmp/panda-deploy
    cat /etc/panda/version
"'

if [ "$RESTART" = "--restart" ]; then
    ssh -o BatchMode=yes "$HOST" 'doas sh -c "
        rc-service panda-bt stop; rc-service speaker-arbiter stop; rc-service speaker-radio stop; rc-service speaker-shairport stop; rc-service speaker-soloist stop; rc-service speaker-wireplumber stop; rc-service speaker-pipewire stop
        rc-service panda-audio-init restart || exit 1
        rc-service speaker-pipewire start && rc-service speaker-wireplumber start || exit 1
        rc-service speaker-soloist start || echo \"speaker-soloist did not start (no API key?); continuing\"
        rc-service speaker-shairport start || exit 1
        rc-service speaker-radio start || exit 1
        rc-service speaker-arbiter start || exit 1
        rc-service bluetooth restart; rc-service panda-bt start || exit 1
        rc-service panda-web restart || exit 1
        sleep 2; rc-status default | grep -E \"panda|speaker\""'
fi
