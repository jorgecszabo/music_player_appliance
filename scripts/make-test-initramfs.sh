#!/bin/bash
# Build build/testboot/: zImage, DTB, a test initramfs (Alpine userland +
# our modules + test/init) and extlinux.conf. Copy it to the SD card's FAT
# partition with: sudo scripts/write-sdcard.sh /dev/sdX build/testboot
set -euo pipefail
source "$(dirname "$0")/env.sh"
LOUT="$PROJECT_DIR/build/linux-out"
KREL=$(cat "$LOUT/include/config/kernel.release")
STAGE="$PROJECT_DIR/build/testboot"
rm -rf "$STAGE"; mkdir -p "$STAGE/boot" "$STAGE/extlinux"

ALPINE_ROOT=1 "$PROJECT_DIR/scripts/alpine.sh" sh -euc "
    R=/tmp/rootfs
    apk add -q --root \$R --initdb --keys-dir /etc/apk/keys \
        -X https://dl-cdn.alpinelinux.org/alpine/v$ALPINE_VERSION/main \
        -X https://dl-cdn.alpinelinux.org/alpine/v$ALPINE_VERSION/community \
        busybox busybox-binsh alpine-baselayout musl-utils kmod \
        alsa-utils iproute2 ethtool bluez-deprecated linux-firmware-ti-connectivity
    mkdir -p \$R/lib/modules
    cp -a /work/build/linux-modules/lib/modules/$KREL \$R/lib/modules/
    depmod -b \$R $KREL
    install -m755 /work/test/init \$R/init
    install -m755 /work/test/test-audio.sh \$R/test-audio.sh
    rm -rf \$R/var/cache/apk/* \$R/lib/apk/db
    du -sh \$R
    cd \$R && find . | cpio -o -H newc --owner 0:0 --quiet | gzip -6 > /work/build/testboot/boot/initramfs-test.gz
"
cp "$LOUT/arch/arm/boot/zImage" "$STAGE/boot/zImage"
cp "$LOUT/arch/arm/boot/dts/ti/omap/omap4-panda.dtb" "$STAGE/boot/omap4-panda.dtb"
cat > "$STAGE/extlinux/extlinux.conf" <<EXT
default test
timeout 1
label test
    kernel /boot/zImage
    fdt /boot/omap4-panda.dtb
    initrd /boot/initramfs-test.gz
    append console=ttyS2,115200n8 rdinit=/init loglevel=7
EXT
ls -lR "$STAGE"
