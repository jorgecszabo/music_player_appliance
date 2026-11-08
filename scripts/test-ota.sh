#!/bin/bash
# Test /etc/panda/panda-ota.py on the build host against a copy of the built
# card (build/sdimage/<board>) and an OTA bundle (build/ota/), in test mode:
# no remounts, reboots or lbu. Covers: install into slot b, bad bundles
# (signature, checksum, board, truncation), activate, trial boot (armed),
# confirm, a failed trial (rollback + settings restore), revert.
# Usage: [BOARD=<board>] scripts/test-ota.sh [bundle.pota]
set -euo pipefail
source "$(dirname "$0")/env.sh"
BUNDLE="${1:-$(ls -t "$PROJECT_DIR"/build/ota/panda-"$BOARD"-*.pota | head -1)}"
OTA="$APPLIANCE_DIR/rootfs/etc/panda/panda-ota.py"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
cp -a "$PROJECT_DIR/build/sdimage/$BOARD" "$T/media"
echo "panda settings v1" > "$T/media/panda.apkovl.tar.gz"
export PANDA_OTA_TEST=1 PANDA_OTA_MEDIA="$T/media" PANDA_OTA_BOARD_CONF="$BOARD_DIR/board.conf" \
    PANDA_OTA_KEYS="$PROJECT_DIR/keys" PANDA_OTA_CMDLINE_FILE="$T/cmdline" PANDA_OTA_CONFIRM_TIMEOUT=3
ota() { python3 -I "$OTA" "$@"; }
boot_slot() { echo "console=${CONSOLE%%,*} panda_slot=$1" > "$T/cmdline"; }
pass=0; fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*"; fail=$((fail + 1)); }
check() { local what="$1"; shift; if "$@"; then ok "$what"; else bad "$what"; fi; }
state() { tr '\n' ' ' < "$T/media/slots/state"; }
# The slot the bootloader would boot, and the boot entry of slot $1 (per scheme).
if [ "${OTA_BOOT_SCHEME:-extlinux}" = rpi ]; then
    default() { sed -n 's|^os_prefix=slots/\(.\)/$|\1|p' "$T/media/slot.txt"; }
    entry() { cat "$T/media/slots/$1/cmdline.txt" 2>/dev/null; }
else
    default() { sed -n 's/^DEFAULT //p' "$T/media/extlinux/extlinux.conf"; }
    entry() { sed -n "/^LABEL $1\$/,/^LABEL /p" "$T/media/extlinux/extlinux.conf"; }
fi
# Keys for the test: only the abuild public key (not the private one).
mkdir -p "$T/keys"; cp "$PROJECT_DIR"/keys/*.rsa.pub "$T/keys/"; export PANDA_OTA_KEYS="$T/keys"

boot_slot a
ota boot 2>/dev/null
check "boot: the booted slot is recorded" grep -qx a "$T/media/.run/slot"

# --- bad bundles -------------------------------------------------------------------
mk() { python3 -I - "$BUNDLE" "$T/$1" "$1" <<'PY'
import sys, tarfile, io, json
src, dst, kind = sys.argv[1], sys.argv[2], sys.argv[3]
tin = tarfile.open(src, "r:")
tout = tarfile.open(dst, "w", format=tarfile.GNU_FORMAT)
n = 0
for m in tin:
    data = tin.extractfile(m).read() if m.isfile() else None
    if kind == "badsig" and m.name == "manifest.sig":
        data = bytes([data[0] ^ 1]) + data[1:]
    if kind == "badfile" and m.name.startswith("boot/vmlinuz"):
        data = data[:100] + bytes([data[100] ^ 1]) + data[101:]
    if kind == "truncated" and n > 6:
        break
    if data is not None:
        m.size = len(data)
        tout.addfile(m, io.BytesIO(data))
    else:
        tout.addfile(m)
    n += 1
tout.close()
PY
}
for k in badsig badfile truncated; do
    mk $k
    if ota install < "$T/$k" >"$T/out" 2>&1; then bad "$k bundle accepted"; else ok "$k bundle rejected: $(grep -o '"error": "[^"]*' "$T/out" | cut -c11-)"; fi
    [ -f "$T/media/slots/b/slot.json" ] && bad "$k left a slot.json" || true
done
sed "s/^BOARD=.*/BOARD=otherboard/" "$BOARD_DIR/board.conf" > "$T/other.conf"
if PANDA_OTA_BOARD_CONF="$T/other.conf" ota install < "$BUNDLE" >"$T/out" 2>&1; then bad "other board accepted"; else ok "bundle for another board rejected"; fi

# --- install + activate + trial + confirm ------------------------------------------
ota install < "$BUNDLE" > "$T/out" 2>&1 || { cat "$T/out"; bad install; }
check "install: slot b complete" test -f "$T/media/slots/b/slot.json"
check "install: default stays a" test "$(default)" = a
check "install: slot b has a boot entry" test -n "$(entry b)"
check "install: slot b has its root image" test -s "$T/media/slots/b/rootfs.squashfs"
check "install: slot b boots its own modloop/root image" sh -c 'echo "$1" | grep -q "modloop=/slots/b/boot/modloop-.* panda_root=/slots/b/rootfs.squashfs panda_slot=b"' _ "$(entry b)"
ota activate > /dev/null 2>&1
check "activate: default b, state trial=b" test "$(default) $(state)" = "b confirmed=a trial=b "
check "activate: the rollback config is armed for the initramfs" test -n "$(find "$T/media" -name '*.armed')"
check "activate: settings snapshot in slot a" cmp -s "$T/media/panda.apkovl.tar.gz" "$T/media/slots/a/settings.apkovl.tar.gz"
check "install refused during a trial" sh -c '! python3 -I "$0" install < "$1" >/dev/null 2>&1' "$OTA" "$BUNDLE"
boot_slot b
ota boot 2>/dev/null
check "trial boot: rollback armed (default a)" test "$(default)" = a
check "trial boot: no armed configs left" test -z "$(find "$T/media" -name '*.armed')"
ota confirm 2>/dev/null
check "confirm: default b, confirmed=b" test "$(default) $(state)" = "b confirmed=b "

# --- a failed trial: back to b with b's settings ---------------------------------
ota activate a > /dev/null 2>&1          # "revert" to a, on trial
echo "panda settings changed by a" > "$T/media/panda.apkovl.tar.gz"
boot_slot a; ota boot 2>/dev/null
touch "$T/media/slots/a/FAIL_HEALTH"
ota confirm 2>/dev/null                  # unhealthy -> "reboot"
check "unhealthy trial: default stays b" test "$(default)" = b
boot_slot b; ota boot 2>"$T/err"
check "failed trial noticed" grep -q "failed" "$T/err"
check "failed trial: settings restored from slot b's snapshot" cmp -s "$T/media/panda.apkovl.tar.gz" "$T/media/slots/b/settings.apkovl.tar.gz"
check "failed trial: state confirmed=b failed=a" test "$(state)" = "confirmed=b failed=a "
check "rollback flag for the web page" test -f "$T/media/.run/ota-rollback"
ota status > "$T/status"
check "status is JSON with both slots" python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["slots"]["a"] and d["slots"]["b"] and d["booted"]=="b" and d["rolled_back"]=="a"' "$T/status"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
