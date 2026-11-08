#!/bin/bash
# Build a signed OTA update bundle from the built image (scripts/build-image.sh).
# Usage: [BOARD=<board>] scripts/build-ota.sh [--no-build]
#   --no-build  bundle build/sdimage/<board> as it is (don't rebuild the image)
# Output: build/ota/panda-<board>-<version>.pota, uploaded on the web page
# (System Update) or installed over SSH:
#   ssh panda@<board> 'doas /etc/panda/web-admin.sh ota-install' < bundle.pota
#
# Format (see /etc/panda/panda-ota.py): an uncompressed tar of manifest.json,
# manifest.sig (RSA-SHA256 over manifest.json with the abuild key; the device
# trusts the keys panda-appliance ships in /etc/panda/ota-keys/), then the
# slot's files: boot/ rootfs.squashfs extlinux.label chimes/ factory.apkovl.tar.gz.
set -euo pipefail
source "$(dirname "$0")/env.sh"
[ "${1:-}" = "--no-build" ] || "$PROJECT_DIR/scripts/build-image.sh"
SLOT="$PROJECT_DIR/build/sdimage/$BOARD/slots/a"
[ -f "$SLOT/slot.json" ] || { echo "no built slot in $SLOT (scripts/build-image.sh)"; exit 1; }
KEY=$(sed -n 's/^PACKAGER_PRIVKEY="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$PROJECT_DIR/keys/abuild.conf")
KEY="$PROJECT_DIR/keys/$(basename "$KEY")"
[ -f "$KEY" ] || { echo "no signing key $KEY"; exit 1; }
OUT="$PROJECT_DIR/build/ota"
mkdir -p "$OUT"
VERSION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$SLOT/slot.json")
BUNDLE="$OUT/panda-$BOARD-$VERSION.pota"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

python3 -I - "$SLOT" "$WORK/manifest.json" <<'PY'
import hashlib, json, os, sys
slot, out = sys.argv[1], sys.argv[2]
info = json.load(open(os.path.join(slot, "slot.json")))
files = []
for root, dirs, names in os.walk(slot):
    dirs.sort()
    for n in sorted(names):
        full = os.path.join(root, n)
        rel = os.path.relpath(full, slot)
        # Per-device files that a slot gets on the card, never from a bundle.
        # cmdline.txt: written by the device for the slot it lands in (rpi scheme).
        if rel in ("slot.json", "settings.apkovl.tar.gz", "cmdline.txt") or rel.startswith("."):
            continue
        h = hashlib.sha256()
        with open(full, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        files.append({"path": rel, "size": os.path.getsize(full), "sha256": h.hexdigest()})
man = {"format": 2, **{k: info.get(k) for k in ("version", "git", "built", "board", "arch", "kernel")},
       "files": files}
json.dump(man, open(out, "w"), indent=1)
print(f"{len(files)} files, {sum(f['size'] for f in files) >> 20} MiB")
PY
openssl dgst -sha256 -sign "$KEY" -out "$WORK/manifest.sig" "$WORK/manifest.json"
openssl dgst -sha256 -verify "$KEY.pub" -signature "$WORK/manifest.sig" "$WORK/manifest.json" >/dev/null

# The file order of the manifest, after the manifest and its signature.
python3 -I -c 'import json,sys; [print(f["path"]) for f in json.load(open(sys.argv[1]))["files"]]' \
    "$WORK/manifest.json" > "$WORK/list"
tar -cf "$BUNDLE.part" --format=gnu --owner=0 --group=0 --numeric-owner \
    -C "$WORK" manifest.json manifest.sig
tar -rf "$BUNDLE.part" --format=gnu --owner=0 --group=0 --numeric-owner \
    --no-recursion -C "$SLOT" -T "$WORK/list"
mv "$BUNDLE.part" "$BUNDLE"
ls -la "$BUNDLE"
