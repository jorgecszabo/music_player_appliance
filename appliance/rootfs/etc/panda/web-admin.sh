#!/bin/sh
# Privileged actions for the web control panel. The panda-web user may run
# exactly this script as root through doas (/etc/doas.d/panda-web.conf), so
# every argument is validated here.
#
#   web-admin.sh restart <service>   restart one appliance service
#   web-admin.sh restart-audio       restart the whole audio stack in order
#   web-admin.sh reboot
#   web-admin.sh save                lbu commit (save the config to the card)
#   web-admin.sh lbu-status          unsaved changes (lbu status)
#   web-admin.sh chrony              time sync status
#   web-admin.sh logs <name>         last lines of a service log
#   web-admin.sh set-name <name>     rename the speaker (all services), save
#   web-admin.sh set-soloist-key     new Spotify API key from stdin (never argv),
#                                    save, restart Soloist, report if it runs
#   web-admin.sh soloist-key-status  "set <mtime>" or "missing" (never the key)
#   web-admin.sh set-audio <bass dB> <treble dB> <cap step>
#                                    tone + volume cap: audio.conf, applied live
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
. /etc/panda/board.conf
. /etc/panda/appliance.conf

die() { echo "web-admin: $*" >&2; exit 2; }
bg() { setsid "$@" </dev/null >>/var/log/panda-web-admin.log 2>&1 & }

restart_audio() {
	rc-service panda-bt stop; rc-service speaker-shairport stop
	rc-service speaker-soloist stop; rc-service speaker-wireplumber stop
	rc-service speaker-pipewire stop
	rc-service panda-audio-init restart
	rc-service speaker-pipewire start; rc-service speaker-wireplumber start
	rc-service speaker-soloist start; rc-service speaker-shairport start
	rc-service panda-bt start
}

case "${1:-}" in
restart)
	case "${2:-}" in
		speaker-soloist|speaker-shairport|panda-bt|avahi-daemon|chronyd) bg rc-service "$2" restart ;;
		bluetooth) bg sh -c 'rc-service bluetooth restart; rc-service panda-bt start' ;;
		*) die "not a restartable service: ${2:-}" ;;
	esac ;;
restart-audio)
	bg "$0" restart-audio-now ;;
restart-audio-now)
	restart_audio ;;
reboot)
	logger -t panda-web "reboot requested from the web page"
	bg sh -c 'sleep 1; reboot' ;;
save)
	lbu commit && echo saved ;;
lbu-status)
	lbu status 2>&1 || true ;;
chrony)
	chronyc -n tracking 2>&1 || true ;;
logs)
	case "${2:-}" in
		pipewire|wireplumber|soloist|shairport) f=/var/log/speaker/$2.log ;;
		panda-bt|panda-web|panda-web-admin) f=/var/log/$2.log ;;
		messages) f=/var/log/messages ;;
		*) die "unknown log: ${2:-}" ;;
	esac
	[ -f "$f" ] || { echo "(no log yet)"; exit 0; }
	# Never show the Spotify API key, even if something logged it.
	key=$(cat /etc/panda/soloist.key 2>/dev/null || true)
	if [ -n "$key" ]; then
		tail -n 80 "$f" | awk -v k="$key" '{ while ((i = index($0, k)) > 0) $0 = substr($0, 1, i - 1) "<api-key>" substr($0, i + length(k)); print }'
	else
		tail -n 80 "$f"
	fi ;;
set-name)
	name="${2:-}"
	# Letters, digits, space and . _ - ' only; 1-32 chars. It ends up in shell,
	# bluez and shairport config files, so nothing that needs quoting.
	printf '%s' "$name" | grep -Eq "^[A-Za-z0-9][A-Za-z0-9 ._'-]{0,31}\$" || die "invalid name"
	[ "$name" = "$SPEAKER_NAME" ] && { echo unchanged; exit 0; }
	tmp=/etc/panda/appliance.conf.new
	awk -v n="$name" '/^SPEAKER_NAME=/ { print "SPEAKER_NAME=\"" n "\""; next } { print }' \
		/etc/panda/appliance.conf > "$tmp" && mv "$tmp" /etc/panda/appliance.conf
	logger -t panda-web "speaker renamed to $name"
	# setup.sh regenerates every config that carries the name and saves (lbu).
	bg sh -c 'sh /etc/panda/setup.sh && rc-service speaker-soloist restart \
		&& rc-service speaker-shairport restart \
		&& rc-service bluetooth restart && rc-service panda-bt start'
	echo renaming ;;
set-soloist-key)
	# One line on stdin. Never echoed, never on a command line.
	IFS= read -r key || true
	key=$(printf '%s' "$key" | tr -d '\r')
	printf '%s' "$key" | grep -Eq '^[A-Za-z0-9_.-]{16,200}$' || die "invalid key format"
	f=/etc/panda/soloist.key
	( umask 077; printf '%s\n' "$key" > "$f.new" ) && chown root:root "$f.new" && mv "$f.new" "$f"
	unset key
	logger -t panda-web "Spotify API key replaced from the web page"
	lbu commit
	rc-service speaker-soloist restart >/dev/null 2>&1 || true
	sleep 6
	if rc-service speaker-soloist status >/dev/null 2>&1; then echo running; else echo "not running"; fi ;;
set-audio)
	b="${2:-}" t="${3:-}" m="${4:-}"
	for v in "$b" "$t"; do
		printf '%s' "$v" | grep -Eq '^-?[0-9]{1,2}$' && [ "$v" -ge -12 ] && [ "$v" -le 12 ] \
			|| die "tone must be an integer -12..12 dB"
	done
	printf '%s' "$m" | grep -Eq '^[0-9]{1,3}$' && [ "$m" -le "${AUDIO_HW_VOLUME_MAX:-0}" ] \
		|| die "cap must be 0..${AUDIO_HW_VOLUME_MAX:-0}"
	f=/etc/panda/audio.conf
	sed -e "s/^BASS_DB=.*/BASS_DB=$b/" -e "s/^TREBLE_DB=.*/TREBLE_DB=$t/" \
		-e "s/^MAX_VOLUME=.*/MAX_VOLUME=$m/" "$f" > "$f.new" && mv "$f.new" "$f"
	# Boot config, and the running filter (headroom follows the largest boost).
	sh /etc/panda/tone-conf.sh > /etc/pipewire/pipewire.conf.d/20-panda-tone.conf
	mult=$(awk -v b="$b" -v t="$t" 'BEGIN { m = b; if (t > m) m = t; if (m < 0) m = 0
		printf "%.6f", 10 ^ (-(3 + m) / 20) }')
	export PIPEWIRE_RUNTIME_DIR=/run/speaker XDG_RUNTIME_DIR=/run/speaker
	id=$(pw-cli ls Node 2>/dev/null | awk '/^\tid /{ id = $2 } /node.name = "panda-eq"/{ print id + 0; exit }')
	[ -n "$id" ] && pw-cli s "$id" Props "{ params = [ \"bass:Gain\" $b.0 \"treble:Gain\" $t.0 \"pre:Mult\" $mult ] }" >/dev/null 2>&1
	[ -n "${AUDIO_HW_VOLUME_CONTROL:-}" ] && amixer -q -c "$AUDIO_CARD" sset "$AUDIO_HW_VOLUME_CONTROL" "$m"
	echo ok ;;
soloist-key-status)
	f=/etc/panda/soloist.key
	if [ -s "$f" ]; then echo "set $(date -r "$f" -u +%Y-%m-%dT%H:%M:%SZ)"; else echo missing; fi ;;
*)
	die "unknown action: ${1:-}" ;;
esac
