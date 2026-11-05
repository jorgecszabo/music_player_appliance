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
#   _extmods         optional: extra out-of-tree module dirs (project-relative,
#                    each with a Kbuild), on top of DEFAULT_EXTMODS below
#   _extmods_skip    optional: DEFAULT_EXTMODS entries this kernel leaves out
# Out-of-tree modules are built against this kernel and installed in
# lib/modules/<release>/updates/. A module dir may have a kconfig.require file:
# grep -xE patterns (one per line, # comments) the kernel config must match.
# ARM (zImage) only so far.
set -euo pipefail
# Generic out-of-tree drivers (USB peripherals, not board hardware), built for
# every board kernel unless its APKBUILD lists them in _extmods_skip.
DEFAULT_EXTMODS="drivers/r92su"
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
_extmods_extra=$(sed -n 's/^_extmods=//p' "$APORT_DIR/APKBUILD" | tr -d '"')
_extmods_skip=$(sed -n 's/^_extmods_skip=//p' "$APORT_DIR/APKBUILD" | tr -d '"')
for m in $_extmods_skip; do
    case " $DEFAULT_EXTMODS " in *" $m "*) ;;
    *) echo "_extmods_skip: $m isn't in DEFAULT_EXTMODS ($DEFAULT_EXTMODS)" >&2; exit 1 ;; esac
done
_extmods=""
for m in $DEFAULT_EXTMODS $_extmods_extra; do
    case " $_extmods_skip $_extmods " in *" $m "*) continue ;; esac
    [ -f "/work/$m/Kbuild" ] || { echo "extmod $m: /work/$m/Kbuild missing" >&2; exit 1; }
    _extmods="$_extmods $m"
done
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
# Every fragment line must survive into the final config (a misspelt or
# dependency-blocked option is silently dropped otherwise). LOCALVERSION is
# cleared above on purpose.
bad=0
while IFS= read -r l; do
    case "$l" in
    CONFIG_LOCALVERSION=*) ;;
    "# CONFIG_"*" is not set")
        s=${l#\# }; s=${s%% *}
        if grep -q "^$s=" "$OUT"/.config; then echo "fragment: $l, but $(grep "^$s=" "$OUT"/.config)" >&2; bad=1; fi ;;
    CONFIG_*)
        grep -qxF "$l" "$OUT"/.config || { echo "fragment: $l, but $(grep "^${l%%=*}=\|^# ${l%%=*} " "$OUT"/.config || echo "${l%%=*} doesn't exist or its dependencies are off")" >&2; bad=1; } ;;
    esac
done < "$APORT_DIR/$_fragment"
[ "$bad" = 0 ] || { echo "fix $APORT_REL/$_fragment" >&2; exit 1; }
# Out-of-tree modules have no Kconfig: check what they need before the long
# build, so a missing option isn't a modpost error (or a module that can't load).
for m in $_extmods; do
    [ -f "/work/$m/kconfig.require" ] || continue
    while IFS= read -r p; do
        case "$p" in ''|'#'*) continue ;; esac
        grep -qxE "$p" "$OUT"/.config || { echo "$m needs $p, but $(grep "^${p%%=*}=\|^# ${p%%=*} " "$OUT"/.config || echo "${p%%=*} is off")" >&2; bad=1; }
    done < "/work/$m/kconfig.require"
done
[ "$bad" = 0 ] || { echo "fix $APORT_REL/$_fragment, or list the module in _extmods_skip" >&2; exit 1; }
make -C "$OUT" -j"$(nproc)" KBUILD_BUILD_VERSION="$((pkgrel + 1))-Alpine" zImage modules dtbs
# External modules build in a copy, so the objects stay out of the source dir.
for m in $_extmods; do
    rm -rf "$OUT/extmod/${m##*/}" && mkdir -p "$OUT/extmod"
    cp -r "/work/$m" "$OUT/extmod/${m##*/}"
    make -C "$OUT" -j"$(nproc)" M="$OUT/extmod/${m##*/}" modules
done

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
for m in $_extmods; do
    make -C "$OUT" -s M="$OUT/extmod/${m##*/}" modules_install \
        INSTALL_MOD_PATH="$STAGE" INSTALL_MOD_STRIP=1
done
rm -f "$STAGE"/lib/modules/"$rel"/build "$STAGE"/lib/modules/"$rel"/source
install -Dm644 "$OUT"/include/config/kernel.release "$STAGE"/usr/share/kernel/$_flavor/kernel.release
echo "staged $rel ($n DTBs) in build/kernel-staging/$_flavor"
