#!/bin/bash
# Cross-compile a board kernel natively on the x86_64 host, inside the Alpine
# cross container (Alpine's arm-none-eabi-gcc), and stage the files in the
# layout its APKBUILD packages:
#   build/kernel-staging/<flavor>/boot/{vmlinuz,System.map,config}-<flavor>
#   build/kernel-staging/<flavor>/boot/dtbs-<flavor>/*.dtb
#   build/kernel-staging/<flavor>/lib/modules/<release>/
#   build/kernel-staging/<flavor>/usr/share/kernel/<flavor>/kernel.release
# Usage: scripts/cross-build-kernel.sh <aport dir>
# Normally called by the aport's cross-build.sh, which scripts/build-package.sh
# runs before abuild. About 10 minutes, versus hours under qemu-user.
#
# The kernel is described by single-line variables in the aport's APKBUILD:
#   pkgver, pkgrel   kernel version; the release is <pkgver>-<pkgrel>-<flavor>
#   _flavor          Alpine kernel flavor (/boot/vmlinuz-<flavor>)
#   _defconfig       base config, e.g. omap2plus_defconfig
#   _fragment        config fragment in the aport dir, merged over the defconfig
#   _dtbs            DTB globs relative to arch/arm/boot/dts/
# ARM (zImage) only so far.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APORT_DIR="$(cd "${1:?usage: $0 <aport dir>}" && pwd)"
APORT_REL="${APORT_DIR#"$PROJECT_DIR"/}"
[ "$APORT_REL" != "$APORT_DIR" ] || { echo "$APORT_DIR is outside the project" >&2; exit 1; }

if [ -z "${PANDA_IN_CROSS:-}" ]; then
    source "$PROJECT_DIR/scripts/env.sh"
    podman image exists "$CROSS_IMAGE" || podman build --platform linux/amd64 \
        --build-arg ALPINE_VERSION="$ALPINE_VERSION" -t "$CROSS_IMAGE" \
        -f "$PROJECT_DIR/container/Containerfile.cross" "$PROJECT_DIR/container"
    exec podman run --rm --platform linux/amd64 -e PANDA_IN_CROSS=1 \
        -v "$PROJECT_DIR:/work:z" "$CROSS_IMAGE" \
        bash /work/scripts/cross-build-kernel.sh "/work/$APORT_REL"
fi

# --- inside the cross container (/work = project) -----------------------------
apkvar() {
    local v
    v=$(sed -n "s/^$1=//p" "$APORT_DIR/APKBUILD" | tr -d '"')
    [ -n "$v" ] || { echo "APKBUILD: $1 not set" >&2; exit 1; }
    echo "$v"
}
pkgver=$(apkvar pkgver)
pkgrel=$(apkvar pkgrel)
_flavor=$(apkvar _flavor)
_defconfig=$(apkvar _defconfig)
_fragment=$(apkvar _fragment)
_dtbs=$(apkvar _dtbs)
SRC=/work/build/src/linux-$pkgver
OUT=/work/build/kcross-out/$_flavor
STAGE=/work/build/kernel-staging/$_flavor
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
make -C "$SRC" -s O="$OUT" "$_defconfig"
"$SRC"/scripts/kconfig/merge_config.sh -O "$OUT" -m "$OUT"/.config "$APORT_DIR/$_fragment" >/dev/null
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
n=0
for pat in $_dtbs; do
    for dtb in "$OUT"/arch/arm/boot/dts/$pat; do
        [ -f "$dtb" ] || { echo "no DTB matches $pat" >&2; exit 1; }
        install -Dm644 "$dtb" "$STAGE"/boot/dtbs-$_flavor/"${dtb##*/}"
        n=$((n + 1))
    done
done
make -C "$OUT" -s modules_install INSTALL_MOD_PATH="$STAGE" INSTALL_MOD_STRIP=1
rm -f "$STAGE"/lib/modules/"$rel"/build "$STAGE"/lib/modules/"$rel"/source
install -Dm644 "$OUT"/include/config/kernel.release "$STAGE"/usr/share/kernel/$_flavor/kernel.release
echo "staged $rel ($n DTBs) in build/kernel-staging/$_flavor"
