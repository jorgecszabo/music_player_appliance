#!/bin/sh
# Download the current Spotify Soloist build for this device and install it on
# the boot media if it changed. Builds expire 90 days after their build date,
# so this runs weekly (/etc/periodic/weekly/panda-soloist-update) and when the
# service finds no binary. Soloist may not be redistributed: the device always
# downloads it from Spotify itself.
# Usage: soloist-update.sh [--restart]   (restart speaker-soloist if updated)
set -eu
. /etc/panda/board.conf
log() { echo "soloist-update: $*"; logger -t soloist-update "$*" 2>/dev/null || true; }

case "$ALPINE_ARCH" in
	armv7)   archive=soloist_release_arm32.tar.gz ;;
	aarch64) archive=soloist_release_arm64.tar.gz ;;
	x86_64)  archive=soloist_release_x86_64.tar.gz ;;
	*) log "no Soloist build for $ALPINE_ARCH"; exit 1 ;;
esac
url="https://soloist-builds.spotifycdn.com/$archive"
dest=/media/"$BOOT_MEDIA"/soloist
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

wget -q -O "$tmp/$archive" "$url" || { log "ERROR: download failed: $url"; exit 1; }
tar -C "$tmp" -xzf "$tmp/$archive" soloist
if [ -f "$dest/soloist" ] && cmp -s "$tmp/soloist" "$dest/soloist"; then
	log "up to date"
	exit 0
fi

mount -o remount,rw /media/"$BOOT_MEDIA"
rc=0
{ mkdir -p "$dest" && cp "$tmp/soloist" "$dest/soloist.new" && sync \
	&& mv "$dest/soloist.new" "$dest/soloist" && sync; } || rc=1
mount -o remount,ro /media/"$BOOT_MEDIA"
[ "$rc" -eq 0 ] || { log "ERROR: could not install the new build"; exit 1; }
log "installed new build"

if [ "${1:-}" = "--restart" ] && rc-service -e speaker-soloist \
		&& rc-service speaker-soloist status >/dev/null 2>&1; then
	rc-service speaker-soloist restart
fi
