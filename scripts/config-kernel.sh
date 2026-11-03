#!/bin/bash
# Generate the kernel .config: omap2plus_defconfig + our fragment.
# Reports any fragment option that didn't end up as requested.
set -euo pipefail
source "$(dirname "$0")/env.sh"
KVER=6.18.60
SRC="$PROJECT_DIR/build/src/linux-$KVER"
OUT="$PROJECT_DIR/build/linux-out"
FRAG="$PROJECT_DIR/aports/main/linux-pandaboard/pandaboard.config"

if [ ! -d "$SRC" ]; then
    mkdir -p "$PROJECT_DIR/build/src"; cd "$PROJECT_DIR/build/src"
    curl -fLO "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-$KVER.tar.xz"
    curl -fLO https://cdn.kernel.org/pub/linux/kernel/v6.x/sha256sums.asc
    grep " linux-$KVER.tar.xz" sha256sums.asc | sha256sum -c
    tar xf "linux-$KVER.tar.xz"
fi
mkdir -p "$OUT"
make -C "$SRC" -s O="$OUT" omap2plus_defconfig
"$SRC/scripts/kconfig/merge_config.sh" -O "$OUT" -m "$OUT/.config" "$FRAG" >/dev/null
make -C "$SRC" -s O="$OUT" olddefconfig

python3 - "$FRAG" "$OUT/.config" <<'PY'
import re, sys
frag, cfg = (open(f).read() for f in sys.argv[1:3])
bad = 0
for line in frag.splitlines():
    m = re.match(r'^(CONFIG_\w+)=(.*)$', line) or re.match(r'^# (CONFIG_\w+) is not set', line)
    if not m: continue
    key = m.group(1); want = m.group(2) if m.lastindex == 2 else 'n'
    got = re.search(rf'^{key}=(.*)$', cfg, re.M)
    got = got.group(1) if got else 'n'
    if got != want: print(f"  {key}: wanted {want}, got {got}"); bad = 1
print("config: all fragment options applied" if not bad else "config: mismatches above")
PY
