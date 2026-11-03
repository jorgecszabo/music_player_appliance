# Common settings, sourced by the other scripts.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ALPINE_VERSION=3.24
IMAGE="localhost/panda-alpine-build:${ALPINE_VERSION}"
# Host cross toolchain (Fedora gcc-arm-linux-gnu) for fast kernel/U-Boot iteration.
export CROSS_COMPILE=arm-linux-gnu-
export ARCH=arm
