#!/bin/sh
# Factory reset: replace the saved settings (the lbu overlay on the boot media)
# with the factory copy, then reboot. Everything saved is lost: Bluetooth
# pairings, the Spotify API key, tone settings, radio favorites, the speaker
# name, and any other change made since the card was written.
#
#   factory-reset.sh            do it (asks nothing; callers confirm first)
#   factory-reset.sh --dry-run  only print what would happen
#
# The factory overlay lives on the boot media at factory/<hostname>.apkovl.tar.gz
# (written there by the image build / deploy). Without it, nothing is done:
# deleting the overlay alone would boot a bare system with no network or SSH.
set -eu
. /etc/panda/board.conf
. /etc/panda/appliance.conf
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY=1

media=/media/"$BOOT_MEDIA"
factory=$media/factory/"$HOSTNAME".apkovl.tar.gz
log() { echo "factory-reset: $*"; logger -t factory-reset "$*" 2>/dev/null || true; }

[ -s "$factory" ] || { log "no factory overlay at $factory: not resetting"; exit 1; }
tar tzf "$factory" etc/hostname >/dev/null 2>&1 || { log "factory overlay is damaged: not resetting"; exit 1; }
current=$(ls "$media"/*.apkovl.tar.gz 2>/dev/null || true)

if [ -n "$DRY" ]; then
	log "dry run: would replace ${current:-<no overlay>} with $factory ($(wc -c < "$factory") bytes),"
	log "dry run: remove other overlays/backups in $media, then reboot."
	exit 0
fi

log "resetting to factory settings"
/etc/panda/chime.sh forgot-all --wait 2>/dev/null || true
mount -o remount,rw "$media"
cp "$factory" "$media/$HOSTNAME.apkovl.tar.gz.new"
sync
for f in $current; do
	[ "$f" = "$media/$HOSTNAME.apkovl.tar.gz" ] || rm -f "$f"
done
mv "$media/$HOSTNAME.apkovl.tar.gz.new" "$media/$HOSTNAME.apkovl.tar.gz"
rm -f "$media"/*.apkovl.tar.gz.bak
sync
mount -o remount,ro "$media"
log "done; rebooting"
reboot
