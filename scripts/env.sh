# Common settings, sourced by the build scripts.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ALPINE_VERSION=3.24
CROSS_IMAGE="localhost/panda-alpine-cross:${ALPINE_VERSION}"

# Board selection: BOARD=<name> picks boards/<name>/ (default pandaboard).
BOARD="${BOARD:-pandaboard}"
# It names the mkimage profile's shell functions and variables: letters, digits, _.
[[ "$BOARD" =~ ^[a-z][a-z0-9_]*$ ]] || { echo "bad board name '$BOARD' (use [a-z][a-z0-9_]*)" >&2; exit 1; }
BOARD_DIR="$PROJECT_DIR/boards/$BOARD"
APPLIANCE_DIR="$PROJECT_DIR/appliance"
[ -f "$BOARD_DIR/board.conf" ] || { echo "unknown board '$BOARD' (no $BOARD_DIR/board.conf)" >&2; exit 1; }
# shellcheck source=../boards/pandaboard/board.conf
. "$BOARD_DIR/board.conf"

# Target-arch build container (runs under qemu-user binfmt on x86_64).
case "$ALPINE_ARCH" in
    armv7)   CONTAINER_PLATFORM=linux/arm/v7 ;;
    aarch64) CONTAINER_PLATFORM=linux/arm64 ;;
    x86_64)  CONTAINER_PLATFORM=linux/amd64 ;;
    *) echo "no container platform for ALPINE_ARCH=$ALPINE_ARCH" >&2; exit 1 ;;
esac
IMAGE="localhost/panda-alpine-build:${ALPINE_VERSION}-${ALPINE_ARCH}"
