#!/bin/bash
# Build the diskless Alpine image (profile "pandaboard") with aports' mkimage.sh.
# Output: build/image/alpine-pandaboard-<tag>-armv7.tar.gz, unpacked into
#         build/sdimage/ ready for: sudo scripts/write-sdcard.sh /dev/sdX build/sdimage
set -euo pipefail
source "$(dirname "$0")/env.sh"
TAG="${TAG:-$ALPINE_VERSION-panda}"
APORTS="$PROJECT_DIR/build/src/aports"
if [ ! -d "$APORTS/scripts" ]; then
    git clone -q --depth 1 --branch "$ALPINE_VERSION-stable" --filter=blob:none --sparse \
        https://gitlab.alpinelinux.org/alpine/aports.git "$APORTS"
    git -C "$APORTS" sparse-checkout set scripts
fi
ls "$PROJECT_DIR"/packages/main/armv7/linux-pandaboard-*.apk >/dev/null \
    || { echo "build the kernel package first: scripts/build-package.sh linux-pandaboard"; exit 1; }
mkdir -p "$PROJECT_DIR/build/image" "$PROJECT_DIR/build/mkimage-work"
# Root's SSH keys for the image: this host's public keys unless already provided.
if [ ! -s "$PROJECT_DIR/image/authorized_keys" ]; then
    cat ~/.ssh/*.pub > "$PROJECT_DIR/image/authorized_keys" 2>/dev/null || true
fi
echo "authorized_keys: $(grep -c . "$PROJECT_DIR/image/authorized_keys" 2>/dev/null || echo 0) key(s)"

# mkimage passes apk --usermode, which refuses root. So the root step only
# installs our signing key into the container's /etc/apk/keys, then mkimage
# runs as "builder" (= host user via keep-id).
ALPINE_ROOT=keepid "$PROJECT_DIR/scripts/alpine.sh" sh -euc "
    cp /home/builder/.abuild/*.rsa.pub /etc/apk/keys/
    su builder -s /bin/sh -c '
        set -eu
        mkdir -p ~/.mkimage && cp /work/image/* ~/.mkimage/
        . ~/.abuild/abuild.conf; export PACKAGER_PRIVKEY
        cd /work/build/src/aports/scripts
        sh mkimage.sh --tag $TAG --arch armv7 --profile pandaboard --hostkeys \
            --workdir /work/build/mkimage-work --outdir /work/build/image \
            --repository /work/packages/main \
            --repository https://dl-cdn.alpinelinux.org/alpine/v$ALPINE_VERSION/main \
            --repository https://dl-cdn.alpinelinux.org/alpine/v$ALPINE_VERSION/community
    '
"
IMG="$PROJECT_DIR/build/image/alpine-pandaboard-$TAG-armv7.tar.gz"
rm -rf "$PROJECT_DIR/build/sdimage"; mkdir -p "$PROJECT_DIR/build/sdimage"
tar -C "$PROJECT_DIR/build/sdimage" -xzf "$IMG"
ls -la "$PROJECT_DIR/build/sdimage" "$PROJECT_DIR/build/sdimage/boot"
du -sh "$PROJECT_DIR/build/sdimage"
