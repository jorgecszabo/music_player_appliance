#!/bin/bash
# Build the slot's root image and the initramfs init that mounts it (SQUASHFS-ROOT.md).
# Called by scripts/build-image.sh after mkimage; runs in the target-arch container, so
# the packages' install scripts run as they would on the board.
# Usage: [BOARD=<board>] scripts/build-rootfs.sh
# Input:  build/sdimage/<board>/slots/a/apks/ (the repository mkimage made),
#         factory.apkovl.tar.gz (its etc/apk/world is what gets installed),
#         boot/initramfs-<flavor> (mkimage's)
# Output: slots/a/rootfs.squashfs; our /init appended to boot/initramfs-<flavor> (the
#         kernel unpacks concatenated archives in order: a later /init replaces the first).
set -euo pipefail
source "$(dirname "$0")/env.sh"
# The init appliance/initramfs/initramfs-init.patch applies to. Another mkinitfs version
# (an Alpine upgrade) stops the build: refresh the patch and boot-test it.
INIT_VERSION=3.14.1-r0
SLOT=build/sdimage/$BOARD/slots/a
for f in apks boot/initramfs-"$KERNEL_FLAVOR" factory.apkovl.tar.gz; do
    [ -e "$PROJECT_DIR/$SLOT/$f" ] || { echo "no $SLOT/$f (scripts/build-image.sh)"; exit 1; }
done
# File times in the image: the commit's (the same commit gives the same image).
EPOCH=$(git -C "$PROJECT_DIR" log -1 --format=%ct 2>/dev/null || date +%s)

# Container root (rootless podman: the host user outside), so apk can chown and chroot.
# The tree is built inside the container: files owned by system users would land on
# the host as sub-uids.
ALPINE_ROOT=1 "$PROJECT_DIR/scripts/alpine.sh" sh -euc '
    slot=/work/$1 arch=$2 flavor=$3 epoch=$4 init_version=$5
    R=/tmp/rootfs
    mkdir -m 0755 "$R"
    cp /root/.abuild/*.rsa.pub /etc/apk/keys/
    world=$(tar -xzOf "$slot"/factory.apkovl.tar.gz etc/apk/world)
    echo ">>> rootfs: $(echo $world)"
    # Only the slot repository: the image holds what the slot would have installed.
    # shellcheck disable=SC2086
    apk add --root "$R" --initdb --arch "$arch" --no-cache --keys-dir /etc/apk/keys \
        --repositories-file /dev/null --repository "$slot"/apks $world
    tar -xzOf "$slot"/factory.apkovl.tar.gz etc/apk/world > "$R"/etc/apk/world
    # No repositories in the image: panda-setup adds the online ones at boot.
    : > "$R"/etc/apk/repositories
    # Our key: deploy.sh installs packages signed with it.
    mkdir -p "$R"/etc/apk/keys
    cp /root/.abuild/*.rsa.pub "$R"/etc/apk/keys/
    rm -rf "$R"/var/log/apk.log "$R"/var/cache/apk/*
    find "$R"/dev -mindepth 1 -delete
    for f in sbin/init bin/busybox etc/init.d/panda-setup etc/panda/version lib/apk/db/installed; do
        [ -e "$R/$f" ] || [ -L "$R/$f" ] || { echo "rootfs: no /$f"; exit 1; }
    done
    [ -z "$(ls -A "$R"/boot 2>/dev/null)" ] || { echo "rootfs: /boot is not empty"; ls "$R"/boot; exit 1; }
    rm -f "$slot"/rootfs.squashfs
    mksquashfs "$R" "$slot"/rootfs.squashfs -comp zstd -b 128K -noappend -no-progress -xattrs-exclude "^security\.selinux$" \
        -all-time "$epoch" -mkfs-time "$epoch" >/dev/null
    echo ">>> rootfs: $(du -sh "$R" | cut -f1) installed, $(du -h "$slot"/rootfs.squashfs | cut -f1) image"

    # The init: Alpine'"'"'s, patched for panda_root=, appended as a second archive.
    src=/usr/share/mkinitfs/initramfs-init
    grep -qx "VERSION=$init_version" "$src" || {
        echo "mkinitfs init is $(sed -n "s/^VERSION=//p" "$src"), the patch is for $init_version:"
        echo "refresh appliance/initramfs/initramfs-init.patch and boot-test it"; exit 1; }
    D=/tmp/initfs
    mkdir -p "$D"
    patch -s -o "$D"/init "$src" /work/appliance/initramfs/initramfs-init.patch
    chmod 0755 "$D"/init
    sh -n "$D"/init
    (cd "$D" && echo init | cpio -o -H newc -R 0:0 --quiet | gzip -9n) >> "$slot"/boot/initramfs-"$flavor"
    echo ">>> initramfs-$flavor: panda init appended"
' sh "$SLOT" "$ALPINE_ARCH" "$KERNEL_FLAVOR" "$EPOCH" "$INIT_VERSION"
