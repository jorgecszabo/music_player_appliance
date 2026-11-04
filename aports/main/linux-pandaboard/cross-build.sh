#!/bin/bash
# Cross-compile the linux-pandaboard kernel natively on the x86_64 host, inside
# the Alpine cross container (Alpine's arm-none-eabi-gcc), and stage the files
# in the layout the APKBUILD packages:
#   build/kernel-staging/boot/{vmlinuz,System.map,config}-pandaboard
#   build/kernel-staging/boot/dtbs-pandaboard/omap4-panda*.dtb
#   build/kernel-staging/lib/modules/<release>/
#   build/kernel-staging/usr/share/kernel/pandaboard/kernel.release
# Called by scripts/build-package.sh before abuild. About 10 minutes, versus
# about 2 hours compiling under qemu-user.
set -euo pipefail
APORT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$APORT_DIR/../../.." && pwd)"
source "$PROJECT_DIR/scripts/env.sh"

if [ -z "${PANDA_IN_CROSS:-}" ]; then
    podman image exists "$CROSS_IMAGE" || podman build --platform linux/amd64 \
        --build-arg ALPINE_VERSION="$ALPINE_VERSION" -t "$CROSS_IMAGE" \
        -f "$PROJECT_DIR/container/Containerfile.cross" "$PROJECT_DIR/container"
    exec podman run --rm --platform linux/amd64 -e PANDA_IN_CROSS=1 \
        -v "$PROJECT_DIR:/work:z" "$CROSS_IMAGE" \
        bash /work/aports/main/linux-pandaboard/cross-build.sh
fi

# --- inside the cross container (/work = project) -----------------------------
_flavor=pandaboard
pkgver=$(sed -n 's/^pkgver=//p' "$APORT_DIR/APKBUILD")
pkgrel=$(sed -n 's/^pkgrel=//p' "$APORT_DIR/APKBUILD")
SRC=/work/build/src/linux-$pkgver
OUT=/work/build/kcross-out
STAGE=/work/build/kernel-staging
export ARCH=arm CROSS_COMPILE=arm-none-eabi-
export KBUILD_BUILD_TIMESTAMP="$(date -u '+%Y-%m-%d %H:%M:%S')"

if [ ! -d "$SRC" ]; then
    mkdir -p /work/build/src && cd /work/build/src
    wget -q "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-$pkgver.tar.xz"
    wget -q -O sha256sums.asc https://cdn.kernel.org/pub/linux/kernel/v6.x/sha256sums.asc
    grep " linux-$pkgver.tar.xz" sha256sums.asc | sha256sum -c
    tar xf "linux-$pkgver.tar.xz"
fi

mkdir -p "$OUT"
echo "-$pkgrel-$_flavor" > "$OUT"/localversion-alpine
make -C "$SRC" -s O="$OUT" omap2plus_defconfig
"$SRC"/scripts/kconfig/merge_config.sh -O "$OUT" -m "$OUT"/.config "$APORT_DIR"/pandaboard.config >/dev/null
# The release suffix comes from localversion-alpine, not the fragment.
"$SRC"/scripts/config --file "$OUT"/.config --set-str LOCALVERSION ""
make -C "$SRC" -s O="$OUT" olddefconfig
make -C "$OUT" -j"$(nproc)" KBUILD_BUILD_VERSION="$((pkgrel + 1))-Alpine" zImage modules dtbs

rel=$(make -C "$OUT" -s kernelrelease)
[ "$rel" = "$pkgver-$pkgrel-$_flavor" ] || { echo "unexpected kernel release $rel"; exit 1; }

rm -rf "$STAGE"
install -Dm644 "$OUT"/arch/arm/boot/zImage "$STAGE"/boot/vmlinuz-$_flavor
install -Dm644 "$OUT"/System.map "$STAGE"/boot/System.map-$_flavor
install -Dm644 "$OUT"/.config "$STAGE"/boot/config-$_flavor
for dtb in "$OUT"/arch/arm/boot/dts/ti/omap/omap4-panda*.dtb; do
    install -Dm644 "$dtb" "$STAGE"/boot/dtbs-$_flavor/"${dtb##*/}"
done
make -C "$OUT" -s modules_install INSTALL_MOD_PATH="$STAGE" INSTALL_MOD_STRIP=1
rm -f "$STAGE"/lib/modules/"$rel"/build "$STAGE"/lib/modules/"$rel"/source
install -Dm644 "$OUT"/include/config/kernel.release "$STAGE"/usr/share/kernel/$_flavor/kernel.release
echo "staged $rel in build/kernel-staging"
