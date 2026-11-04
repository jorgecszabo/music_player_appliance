#!/bin/bash
# Run a command (or a shell) inside the armv7 Alpine build container.
# The project is mounted at /work and keys/ at ~/.abuild, with the shared SELinux
# label (:z): a private label (:Z) relabels the tree for each new container
# and locks out any container that is already running.
# ALPINE_ROOT=1 runs as container root instead (sudo can't work under qemu-user;
# in rootless podman, container root maps to the host user anyway).
set -euo pipefail
source "$(dirname "$0")/env.sh"
tty_flag=(); [ -t 0 ] && tty_flag=(-it)
user_flags=(--userns=keep-id)
[ "${ALPINE_ROOT:-0}" = 1 ] && user_flags=(--user 0)
exec podman run --rm "${tty_flag[@]}" --platform linux/arm/v7 \
    "${user_flags[@]}" \
    -v "$PROJECT_DIR:/work:z" \
    -v "$PROJECT_DIR/keys:/home/builder/.abuild:z" \
    -v "$PROJECT_DIR/keys:/root/.abuild:z" \
    -e PACKAGER="${PACKAGER:-PandaBoard builder <builder@localhost>}" \
    "$IMAGE" "${@:-bash}"
