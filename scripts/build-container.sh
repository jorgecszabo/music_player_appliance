#!/bin/bash
# Build the armv7 Alpine build container image.
set -euo pipefail
source "$(dirname "$0")/env.sh"
podman build --platform linux/arm/v7 \
    --build-arg ALPINE_VERSION="$ALPINE_VERSION" \
    --build-arg UID="$(id -u)" --build-arg GID="$(id -g)" \
    -t "$IMAGE" "$PROJECT_DIR/container"
