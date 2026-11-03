#!/bin/bash
# Run a command (or a shell) inside the armv7 Alpine build container.
# The project is mounted at /work and keys/ at ~/.abuild.
set -euo pipefail
source "$(dirname "$0")/env.sh"
tty_flag=(); [ -t 0 ] && tty_flag=(-it)
exec podman run --rm "${tty_flag[@]}" --platform linux/arm/v7 \
    --userns=keep-id \
    -v "$PROJECT_DIR:/work:Z" \
    -v "$PROJECT_DIR/keys:/home/builder/.abuild:Z" \
    -e PACKAGER="${PACKAGER:-PandaBoard builder <builder@localhost>}" \
    "$IMAGE" "${@:-bash}"
