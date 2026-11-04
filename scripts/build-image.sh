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

# Optional factory Wi-Fi: appliance/wifi.json (not in git), e.g.
#   {"country": "DE", "networks": [{"ssid": "Home", "password": "..."}]}
# goes into the overlay (live and factory copy) as /etc/panda/wifi.json with
# the passwords hashed (WPA2 PSK; a "wpa3": true network keeps its password).
WIFI_ROOT="$PROJECT_DIR/build/wifi-rootfs"
rm -rf "$WIFI_ROOT"
if [ -f "$APPLIANCE_DIR/wifi.json" ]; then
    mkdir -p "$WIFI_ROOT/etc/panda"
    python3 -I "$APPLIANCE_DIR/rootfs/etc/panda/panda-wifi.py" --convert \
        "$APPLIANCE_DIR/wifi.json" "$WIFI_ROOT/etc/panda/wifi.json"
fi

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
        tar -C /work/appliance/rootfs --owner=0 --group=0 -cf ~/.mkimage/rootfs.tar etc
        if [ -f /work/build/wifi-rootfs/etc/panda/wifi.json ]; then
            tar -C /work/build/wifi-rootfs --owner=0 --group=0 --mode=0600 \
                -rf ~/.mkimage/rootfs.tar etc/panda/wifi.json
        fi
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
SD="$PROJECT_DIR/build/sdimage"
rm -rf "$SD"; mkdir -p "$SD"
tar -C "$SD" -xzf "$IMG"

# Extras on the boot media, next to what mkimage made:
HOST=$(. "$APPLIANCE_DIR/appliance.conf"; echo "$HOSTNAME")
# - factory overlay: a factory reset restores it (lbu overwrites the original)
mkdir -p "$SD/factory"; cp "$SD/$HOST.apkovl.tar.gz" "$SD/factory/$HOST.apkovl.tar.gz"
# - the Soloist glibc runtime (no Soloist binary: the device downloads it)
RT="$PROJECT_DIR/build/soloist/soloist-runtime-$ALPINE_ARCH.squashfs"
if [ -f "$RT" ]; then
    mkdir -p "$SD/soloist"; cp "$RT" "$SD/soloist/soloist-runtime.squashfs"
else
    echo "WARNING: no $RT (scripts/build-soloist-runtime.sh): Spotify won't work" >&2
fi
# - the chimes, synthesized here (slow on the board); .source = md5 of the generator
CH="$APPLIANCE_DIR/rootfs/etc/panda/chimes.py"
python3 -I "$CH" "$SD/chimes" && md5sum "$CH" | cut -d" " -f1 > "$SD/chimes/.source"
ls -la "$PROJECT_DIR/build/sdimage" "$PROJECT_DIR/build/sdimage/boot"
du -sh "$PROJECT_DIR/build/sdimage"
