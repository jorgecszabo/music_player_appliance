#!/bin/sh
# Factory reset: replace the saved settings (the lbu overlay on the boot media)
# with the factory copy, then reboot. Everything saved is lost: Bluetooth
# pairings, the Spotify API key, tone settings, radio favorites, the speaker
# name, and any other change made since the card was written.
#
#   factory-reset.sh            do it (asks nothing; callers confirm first)
#   factory-reset.sh --dry-run  only print what would happen
#
# The factory overlay: the booted OTA slot's (slots/<x>/factory.apkovl.tar.gz,
# from that version's image), else the one in factory/ (written by the image build).
# The installed version stays: only the settings are reset. Without it, nothing is done:
# deleting the overlay alone would boot a bare system with no network or SSH.
# The speaker may have been renamed (the hostname with it): the restored settings are
# saved under the hostname they carry, as lbu will name them, and every other overlay goes.
set -eu
. /etc/panda/board.conf
. /etc/panda/appliance.conf
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY=1

media=/media/"$BOOT_MEDIA"
slot=$(cat /run/panda/slot 2>/dev/null || true)
factory=$media/slots/$slot/factory.apkovl.tar.gz
[ -n "$slot" ] && [ -s "$factory" ] || factory=$(ls "$media"/factory/*.apkovl.tar.gz 2>/dev/null | head -n 1)
log() { echo "factory-reset: $*"; logger -t factory-reset "$*" 2>/dev/null || true; }

[ -s "${factory:-}" ] || { log "no factory overlay: not resetting"; exit 1; }
tar tzf "$factory" etc/hostname >/dev/null 2>&1 || { log "factory overlay is damaged: not resetting"; exit 1; }
host=$(tar xzOf "$factory" etc/hostname | head -n 1 | tr -d ' \r')
[ -n "$host" ] || { log "factory overlay has no hostname: not resetting"; exit 1; }
target=$media/$host.apkovl.tar.gz
current=$(ls "$media"/*.apkovl.tar.gz 2>/dev/null || true)

if [ -n "$DRY" ]; then
	log "dry run: would replace ${current:-<no overlay>} with $factory ($(wc -c < "$factory") bytes),"
	log "dry run: remove other overlays/backups in $media, then reboot."
	exit 0
fi

log "resetting to factory settings"
# Nothing else writes the card from here on (OTA installs, lbu commits).
mkdir -p /run/panda
exec 9>>/run/panda/media.lock
flock 9
/etc/panda/chime.sh forgot-all --wait 2>/dev/null || true
mount -o remount,rw "$media"
cp "$factory" "$target.new"
sync
for f in $current; do
	[ "$f" = "$target" ] || rm -f "$f"
done
mv "$target.new" "$target"
rm -f "$media"/*.apkovl.tar.gz.bak
sync
mount -o remount,ro "$media"
log "done; rebooting"
reboot
