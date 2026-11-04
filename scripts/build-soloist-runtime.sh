#!/bin/bash
# Build a minimal glibc runtime for Spotify Soloist (a glibc binary) to run on
# musl/Alpine: Debian trixie glibc + PipeWire client library, client modules
# and SPA plugins, packed as a read-only squashfs.
# Output: build/soloist/soloist-runtime-<arch>.squashfs
# On the device it's mounted at /opt/soloist-runtime, and Soloist runs as
#   /opt/soloist-runtime/lib/ld-linux-armhf.so.3 --library-path /opt/soloist-runtime/lib soloist
# with PIPEWIRE_MODULE_DIR / SPA_PLUGIN_DIR / PIPEWIRE_CONFIG_DIR pointing into it.
# Contains only Debian open-source packages; the Soloist binary is NOT included
# (it may not be redistributed; the device downloads it from Spotify).
set -euo pipefail
source "$(dirname "$0")/env.sh"
case "$ALPINE_ARCH" in
    armv7)   DEB_PLATFORM=linux/arm/v7; DEB_TRIPLET=arm-linux-gnueabihf; LDSO=ld-linux-armhf.so.3 ;;
    aarch64) DEB_PLATFORM=linux/arm64;  DEB_TRIPLET=aarch64-linux-gnu;   LDSO=ld-linux-aarch64.so.1 ;;
    x86_64)  DEB_PLATFORM=linux/amd64;  DEB_TRIPLET=x86_64-linux-gnu;    LDSO=ld-linux-x86-64.so.2 ;;
esac
OUT="$PROJECT_DIR/build/soloist"
mkdir -p "$OUT"

podman run --rm --platform "$DEB_PLATFORM" -v "$OUT:/out:z" docker.io/library/debian:trixie bash -euc "
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
        libpipewire-0.3-0t64 libpipewire-0.3-modules libspa-0.2-modules \
        libatomic1 libgcc-s1 squashfs-tools >/dev/null
    R=/tmp/rt; L=/usr/lib/$DEB_TRIPLET
    mkdir -p \$R/lib \$R/pipewire-0.3 \$R/spa-0.2 \$R/share/pipewire
    # PipeWire client modules and the SPA plugins a client stream needs.
    for m in protocol-native client-node adapter metadata; do
        cp -L \$L/pipewire-0.3/libpipewire-module-\$m.so \$R/pipewire-0.3/
    done
    for d in support audioconvert; do cp -rL \$L/spa-0.2/\$d \$R/spa-0.2/; done
    # Minimal client config (Debian ships client.conf with the full daemon package).
    cat > \$R/share/pipewire/client.conf <<'CONF'
context.properties = { log.level = 2 }
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
    { name = libpipewire-module-metadata }
]
CONF
    # glibc + everything those objects (and Soloist's NEEDED list) link against.
    for f in $LDSO libc.so.6 libm.so.6 libpthread.so.0 libdl.so.2 librt.so.1 \
             libatomic.so.1 libgcc_s.so.1 libpipewire-0.3.so.0; do
        cp -L \$L/\$f \$R/lib/
    done
    changed=1
    while [ \$changed = 1 ]; do
        changed=0
        for so in \$(find \$R -name '*.so*' -type f); do
            for dep in \$(ldd \$so 2>/dev/null | awk '/=> \\//{print \$3}'); do
                b=\$(basename \$dep)
                [ -e \$R/lib/\$b ] || { cp -L \$dep \$R/lib/; changed=1; }
            done
        done
    done
    echo \"runtime: \$(du -sh \$R | cut -f1), \$(ls \$R/lib | wc -l) libs\"
    ls \$R/lib
    echo \"glibc: \$(\$L/libc.so.6 2>&1 | head -1)\"
    echo \"libpipewire: \$(dpkg-query -W -f='\${Version}' libpipewire-0.3-0t64)\"
    rm -f /out/soloist-runtime-$ALPINE_ARCH.squashfs
    mksquashfs \$R /out/soloist-runtime-$ALPINE_ARCH.squashfs -comp xz -all-root -quiet
"
ls -l "$OUT"/soloist-runtime-"$ALPINE_ARCH".squashfs
