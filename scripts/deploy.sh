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

# Large artifacts the setup script installs onto the boot media.
RT="$PROJECT_DIR/build/soloist/soloist-runtime-$ALPINE_ARCH.squashfs"
if [ -f "$RT" ]; then
    mkdir -p "$STAGE/tmp/panda-deploy"
    cp "$RT" "$STAGE/tmp/panda-deploy/soloist-runtime.squashfs"
fi

echo ">>> deploying to $HOST"
# Factory overlay (restored by a factory reset): the image's original apkovl.
FACTORY="$PROJECT_DIR/build/sdimage/$BOARD/$(. "$APPLIANCE_DIR/appliance.conf"; echo "$HOSTNAME").apkovl.tar.gz"
if [ -f "$FACTORY" ]; then
    mkdir -p "$STAGE/tmp/panda-deploy"; cp "$FACTORY" "$STAGE/tmp/panda-deploy/factory.apkovl.tar.gz"
fi

# Our own packages (aports/: shairport-sync, pipewire, mpv) that the appliance
# uses instead of Alpine's. All of them except kernels and -dev/-dbg/-doc;
# setup.sh installs only those whose name is already installed (upgrades).
for f in "$PROJECT_DIR"/packages/main/"$ALPINE_ARCH"/*.apk; do
    n=$(basename "$f" .apk); n=${n%-*-*}
    case "$n" in linux-*|*-dev|*-dbg|*-doc) continue ;; esac
    mkdir -p "$STAGE/tmp/panda-deploy/apks"; cp "$f" "$STAGE/tmp/panda-deploy/apks/"
done

# List deploy artifacts by file, not the tmp/ dir: a tmp/ entry would reset
# /tmp's mode (sticky bit) on extraction.
tar -C "$STAGE" --owner=0 --group=0 -czf - etc $(cd "$STAGE" && find tmp -type f 2>/dev/null) \
    | ssh -o BatchMode=yes "$HOST" 'doas tar -C / -xzof - && doas sh /etc/panda/setup.sh; rc=$?; doas rm -rf /tmp/panda-deploy; exit $rc'

if [ "$RESTART" = "--restart" ]; then
    ssh -o BatchMode=yes "$HOST" 'doas sh -c "
        # Stop dependents first, then start in order (restarting pipewire on
        # its own stops wireplumber without openrc noticing).
        rc-service panda-bt stop; rc-service speaker-arbiter stop; rc-service speaker-radio stop; rc-service speaker-shairport stop; rc-service speaker-soloist stop; rc-service speaker-wireplumber stop; rc-service speaker-pipewire stop
        rc-service panda-audio-init restart || exit 1
        rc-service speaker-pipewire start && rc-service speaker-wireplumber start || exit 1
        # Soloist fails without an API key (set on the web page): not fatal.
        rc-service speaker-soloist start || echo \"speaker-soloist did not start (no API key?); continuing\"
        rc-service speaker-shairport start || exit 1
        rc-service speaker-radio start || exit 1
        rc-service speaker-arbiter start || exit 1
        rc-service bluetooth restart; rc-service panda-bt start || exit 1
        rc-service panda-web restart || exit 1
        sleep 2; rc-status default | grep -E \"panda|speaker\""'
fi
