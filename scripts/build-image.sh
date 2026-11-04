#!/bin/bash
# Build the diskless Alpine image for the board with aports' mkimage.sh.
# Usage: [BOARD=<board>] scripts/build-image.sh
# Uses: boards/<board>/mkimg.<board>.sh (mkimage profile "<board>") and
#       boards/<board>/board.conf, plus appliance/{appliance.conf,packages.list,
#       genapkovl.sh,authorized_keys}.
# Output: build/image/alpine-<board>-<tag>-<arch>.tar.gz, unpacked into
#         build/sdimage/ ready for the board's SD card writer, e.g.
#         sudo boards/pandaboard/write-sdcard.sh /dev/sdX build/sdimage
set -euo pipefail
source "$(dirname "$0")/env.sh"
TAG="${TAG:-$ALPINE_VERSION-panda}"
APORTS="$PROJECT_DIR/build/src/aports"
if [ ! -d "$APORTS/scripts" ]; then
    git clone -q --depth 1 --branch "$ALPINE_VERSION-stable" --filter=blob:none --sparse \
        https://gitlab.alpinelinux.org/alpine/aports.git "$APORTS"
    git -C "$APORTS" sparse-checkout set scripts
fi
ls "$PROJECT_DIR"/packages/main/"$ALPINE_ARCH"/"$KERNEL_PACKAGE"-*.apk >/dev/null 2>&1 \
    || { echo "build the kernel package first: scripts/build-package.sh $KERNEL_PACKAGE"; exit 1; }
[ -f "$BOARD_DIR/mkimg.$BOARD.sh" ] || { echo "missing $BOARD_DIR/mkimg.$BOARD.sh"; exit 1; }
mkdir -p "$PROJECT_DIR/build/image" "$PROJECT_DIR/build/mkimage-work"
# mkimage keys the apkovl cache on a checksum of the generator, looked up relative
# to its own scripts/ dir, where ours isn't, so the checksum never changes.
# Always regenerate the overlay (it's quick).
rm -rf "$PROJECT_DIR"/build/mkimage-work/apkovl_* "$PROJECT_DIR"/build/mkimage-work/image-*
# SSH keys for the admin user: this host's public keys unless already provided.
if [ ! -s "$APPLIANCE_DIR/authorized_keys" ]; then
    cat ~/.ssh/*.pub > "$APPLIANCE_DIR/authorized_keys" 2>/dev/null || true
fi
echo "authorized_keys: $(grep -c . "$APPLIANCE_DIR/authorized_keys" 2>/dev/null || echo 0) key(s)"

# Files mkimage loads from ~/.mkimage (relative to /work inside the container).
MKIMAGE_FILES="boards/$BOARD/mkimg.$BOARD.sh boards/$BOARD/board.conf
    appliance/appliance.conf appliance/packages.list appliance/genapkovl.sh"
[ -s "$APPLIANCE_DIR/authorized_keys" ] && MKIMAGE_FILES="$MKIMAGE_FILES appliance/authorized_keys"

# mkimage passes apk --usermode, which refuses root. So the root step only
# installs our signing key into the container's /etc/apk/keys, then mkimage
# runs as "builder" (= host user via keep-id).
ALPINE_ROOT=keepid "$PROJECT_DIR/scripts/alpine.sh" sh -euc "
    cp /home/builder/.abuild/*.rsa.pub /etc/apk/keys/
    su builder -s /bin/sh -c '
        set -eu
        rm -rf ~/.mkimage && mkdir -p ~/.mkimage
        cd /work && cp $(echo $MKIMAGE_FILES) ~/.mkimage/
        . ~/.abuild/abuild.conf; export PACKAGER_PRIVKEY
        cd /work/build/src/aports/scripts
        sh mkimage.sh --tag $TAG --arch $ALPINE_ARCH --profile $BOARD --hostkeys \
            --workdir /work/build/mkimage-work --outdir /work/build/image \
            --repository /work/packages/main \
            --repository https://dl-cdn.alpinelinux.org/alpine/v$ALPINE_VERSION/main \
            --repository https://dl-cdn.alpinelinux.org/alpine/v$ALPINE_VERSION/community
    '
"
IMG="$PROJECT_DIR/build/image/alpine-$BOARD-$TAG-$ALPINE_ARCH.tar.gz"
rm -rf "$PROJECT_DIR/build/sdimage"; mkdir -p "$PROJECT_DIR/build/sdimage"
tar -C "$PROJECT_DIR/build/sdimage" -xzf "$IMG"
ls -la "$PROJECT_DIR/build/sdimage" "$PROJECT_DIR/build/sdimage/boot"
du -sh "$PROJECT_DIR/build/sdimage"
