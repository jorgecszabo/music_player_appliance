#!/bin/bash
# Build the target-arch Alpine build container image (arch from board.conf).
set -euo pipefail
source "$(dirname "$0")/env.sh"
podman build --platform "$CONTAINER_PLATFORM" \
    --build-arg ALPINE_VERSION="$ALPINE_VERSION" \
    --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" \
    -t "$IMAGE" "$PROJECT_DIR/container"
