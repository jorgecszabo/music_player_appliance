#!/bin/sh
# Spotify Soloist on the boot media: download, check, install, keep current.
# Soloist may not be redistributed, so the device always downloads it from
# Spotify itself. Builds expire 90 days after their build date.
#
# Usage: soloist-update.sh [update]  download now; stage it if it changed
#        soloist-update.sh due       the 15-minute cron job: update if due,
#                                    restart Soloist onto a staged build when idle
#        soloist-update.sh activate  swap a staged build in (Soloist not running)
#        soloist-update.sh check     exit 0 if the installed binary runs
#        soloist-update.sh mount     mount the glibc runtime
#
# Files on the boot media (<boot>/soloist/):
#   soloist       the installed build
#   soloist.next  a downloaded, checked build waiting for Soloist to restart
#   soloist.part  being written; a leftover one is from a power cut: deleted
# A new build is never written over the running one: the running process keeps
# the old file open, and FAT can't go read-only again while a deleted file is
# open. So it's staged as soloist.next and renamed over soloist when Soloist
# starts (speaker-soloist's start_pre runs "activate").
#
# "due" means: the first run since boot, a week since the last successful
# check, the installed binary missing or not running, or its build older than
# EXPIRE_SOON_DAYS. A failed download is retried on the next run (15 min), so
# a board that is off at night or boots without network still catches up.
# Soloist is restarted onto a staged build only while Spotify isn't playing.
#
# The boot partition is FAT (no journal), so each read-write window is short,
# and every staged file is checked (it must run --version through the
# runtime) before it is used.
set -eu
. /etc/panda/board.conf
log() { echo "soloist-update: $*"; logger -t soloist-update "$*" 2>/dev/null || true; }

RT=/opt/soloist-runtime
MEDIA=/media/"$BOOT_MEDIA"
DEST="$MEDIA"/soloist
BIN="$DEST"/soloist
NEXT="$DEST"/soloist.next
PART="$DEST"/soloist.part
STAMP=/run/soloist-update.last      # uptime (s) of the last successful check: RAM, gone at reboot
NOWPLAYING=/run/speaker/nowplaying.json
CHECK_EVERY_S=$((7 * 86400))
EXPIRE_SOON_DAYS=80

case "$ALPINE_ARCH" in
	armv7)   archive=soloist_release_arm32.tar.gz ;;
	aarch64) archive=soloist_release_arm64.tar.gz ;;
	x86_64)  archive=soloist_release_x86_64.tar.gz ;;
	*) log "no Soloist build for $ALPINE_ARCH"; exit 1 ;;
esac
URL="https://soloist-builds.spotifycdn.com/$archive"

uptime_s() { cut -d. -f1 /proc/uptime; }

mount_runtime() {
	mountpoint -q "$RT" && return 0
	mkdir -p "$RT"
	mount -o loop,ro "$DEST"/soloist-runtime.squashfs "$RT"
}

# Print the version line if the binary runs through the runtime (as user speaker).
version_of() {
	[ -f "$1" ] && mount_runtime || return 1
	su -s /bin/sh -c "$RT/lib/ld.so --library-path $RT/lib $1 --version" speaker 2>/dev/null \
		| grep '^soloist '
}

# Days since the build date in the version line, e.g. "... build 1793793704 (20261104) ...".
# Empty if unknown, or if the clock isn't set yet (before the build date).
build_age_days() {
	d=$(echo "$1" | sed -n 's/.*(\([0-9]\{8\}\)).*/\1/p')
	[ -n "$d" ] || return 0
	t=$(date -u -D %Y%m%d -d "$d" +%s 2>/dev/null) || return 0
	now=$(date -u +%s)
	[ "$now" -ge "$t" ] && echo $(( (now - t) / 86400 ))
	return 0
}

# Run a command with the boot media read-write. Going back to read-only can
# fail for a moment ("busy") while something has a replaced file open (the
# arbiter's "soloist ctl"), so retry; a card left read-write is an error.
# Under the boot-media lock (an OTA install or an lbu commit may be writing).
with_media_rw() {
	mkdir -p /run/panda
	exec 9>>/run/panda/media.lock
	flock 9
	_with_media_rw "$@"; r=$?
	flock -u 9
	exec 9>&-
	return $r
}
_with_media_rw() {
	mount -o remount,rw "$MEDIA" || { log "ERROR: can't remount $MEDIA read-write"; return 1; }
	rc=0; "$@" || rc=1
	sync
	n=0
	until mount -o remount,ro "$MEDIA" 2>/dev/null; do
		n=$((n + 1))
		[ "$n" -lt 15 ] || { log "ERROR: $MEDIA stays read-write (busy)"; return 1; }
		sleep 1
	done
	return $rc
}

short() { echo "${1%% (g*}"; }   # "soloist 1.3.10.43 build 1793815337 (20261104)"

soloist_running() { rc-service speaker-soloist status >/dev/null 2>&1; }
spotify_playing() { [ -r "$NOWPLAYING" ] && grep -q '"Spotify"' "$NOWPLAYING"; }

# Swap a staged build in. Only while Soloist isn't running (start_pre, or the service is down).
activate() {
	[ -e "$NEXT" ] || return 0
	if v=$(version_of "$NEXT"); then
		with_media_rw mv "$NEXT" "$BIN" || return 1
		log "activated $(short "$v")"
	else
		with_media_rw rm -f "$NEXT" || return 1
		log "ERROR: the staged build doesn't run (power cut?); removed it"
	fi
}

# Restart Soloist onto a staged build, unless Spotify is playing (then a later "due" run does it).
apply_staged() {
	[ -e "$NEXT" ] || return 0
	if ! soloist_running; then
		activate
	elif spotify_playing; then
		[ "${1:-}" = quiet ] || log "Spotify is playing: Soloist restarts onto the new build later"
	else
		log "restarting Soloist onto the new build"
		rc-service speaker-soloist restart >/dev/null 2>&1 || log "WARNING: restart failed"
	fi
}

# Download; stage the build if it differs from the installed (or staged) one.
update() {
	tmp="$(mktemp -d)"
	trap 'rm -rf "$tmp"' EXIT
	chmod 0755 "$tmp"   # version_of runs it as speaker
	wget -q -O "$tmp/$archive" "$URL" || { log "ERROR: download failed: $URL"; return 1; }
	tar -C "$tmp" -xzf "$tmp/$archive" soloist && rm -f "$tmp/$archive" \
		|| { log "ERROR: bad archive from $URL"; return 1; }
	echo "$(uptime_s)" > "$STAMP"
	if [ -e "$NEXT" ] && cmp -s "$tmp/soloist" "$NEXT"; then
		log "newest build already staged"; return 0
	fi
	if [ ! -e "$NEXT" ] && [ -f "$BIN" ] && cmp -s "$tmp/soloist" "$BIN"; then
		log "up to date"; return 0
	fi
	v=$(version_of "$tmp/soloist") \
		|| { log "ERROR: the downloaded build doesn't run here; keeping the installed one"; return 1; }
	with_media_rw sh -c 'mkdir -p "$1" && cp "$2" "$1"/soloist.part && sync && mv "$1"/soloist.part "$1"/soloist.next' \
		sh "$DEST" "$tmp/soloist" \
		|| { log "ERROR: could not stage the new build"; return 1; }
	log "downloaded $(short "$v")"
}

# --- the cron job --------------------------------------------------------------
due() {
	if [ -e "$PART" ]; then
		with_media_rw rm -f "$PART" && log "removed a partly written build (interrupted download)"
	fi
	apply_staged quiet
	reason=""
	if ! v=$(version_of "$BIN"); then
		reason="installed build missing or not running"
	elif age=$(build_age_days "$v") && [ -n "$age" ] && [ "$age" -ge "$EXPIRE_SOON_DAYS" ]; then
		reason="build is $age days old"
	elif [ ! -s "$STAMP" ]; then
		reason="first check since boot"
	elif [ $(( $(uptime_s) - $(cat "$STAMP") )) -ge "$CHECK_EVERY_S" ]; then
		reason="weekly check"
	fi
	[ -n "$reason" ] || return 0
	log "checking for a new build ($reason)"
	update || return 1
	apply_staged
}

case "${1:-update}" in
	update)   update && { soloist_running || activate; } ;;
	due)      due ;;
	activate) activate ;;
	check)    version_of "$BIN" >/dev/null ;;
	mount)    mount_runtime ;;
	*) echo "usage: $0 [update|due|activate|check|mount]" >&2; exit 2 ;;
esac
