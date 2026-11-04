#!/bin/bash
# Deploy the appliance files to a running device and run its setup script.
# Usage: [BOARD=<board>] [PANDA_HOST=panda@panda.local] scripts/deploy.sh [--restart]
#   Copies appliance/rootfs/ (everything under /etc) plus board.conf and
#   appliance.conf, runs /etc/panda/setup.sh (packages, users, generated
#   configs, services, lbu commit), and with --restart restarts the audio
#   services.
set -euo pipefail
source "$(dirname "$0")/env.sh"
HOST="${PANDA_HOST:-panda@panda.local}"
RESTART="${1:-}"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -a "$APPLIANCE_DIR/rootfs/." "$STAGE/"
mkdir -p "$STAGE/etc/panda"
cp "$BOARD_DIR/board.conf" "$STAGE/etc/panda/board.conf"
cp "$APPLIANCE_DIR/appliance.conf" "$STAGE/etc/panda/appliance.conf"

echo ">>> deploying to $HOST"
tar -C "$STAGE" --owner=0 --group=0 -czf - etc \
    | ssh -o BatchMode=yes "$HOST" 'doas tar -C / -xzof - && doas sh /etc/panda/setup.sh'

if [ "$RESTART" = "--restart" ]; then
    ssh -o BatchMode=yes "$HOST" 'doas sh -c "
        for s in panda-audio-init speaker-pipewire speaker-wireplumber; do
            rc-service \$s restart || exit 1
        done
        sleep 2; rc-status default | grep -E \"panda|speaker\""'
fi
