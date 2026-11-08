#!/bin/sh
# Privileged actions for the web control panel. The panda-web user may run
# exactly this script as root through doas (/etc/doas.d/panda-web.conf), so
# every argument is validated here.
#
#   web-admin.sh restart <service>   restart one appliance service
#   web-admin.sh restart-audio       restart the whole audio stack in order
#   web-admin.sh bt-reset            power-cycle a combo chip's Bluetooth half (reload its
#                                    UART driver, board.conf BT_DRIVER_AFTER_WIFI); panda-bt
#                                    calls it when the controller stops answering
#   web-admin.sh reboot
#   web-admin.sh poweroff            halt (the front panel's Shut Down)
#   web-admin.sh save                lbu commit (save the config to the card)
#   web-admin.sh lbu-status          unsaved changes (lbu status)
#   web-admin.sh chrony              time sync status
#   web-admin.sh logs <name>         last lines of a service log
#   web-admin.sh set-name <name>     rename the speaker (all services), save
#   web-admin.sh set-soloist-key     new Spotify API key from stdin (never argv),
#                                    save, restart Soloist, report if it runs
#   web-admin.sh soloist-key-status  "set <mtime>" or "missing" (never the key)
#   web-admin.sh factory-reset       restore the factory overlay and reboot
#   web-admin.sh factory-reset-dry   only show what a factory reset would do
#   web-admin.sh set-master <0..1>   remember the master volume (audio.conf; restored at
#                                    boot by audio-route.py --master; not applied here)
#   web-admin.sh ota-status          update slots (JSON, panda-ota.py status)
#   web-admin.sh ota-install         an update bundle on stdin -> the standby slot
#                                    (JSON lines; verified by panda-ota.py)
#   web-admin.sh ota-activate <a|b>  boot that slot on trial, then reboot
#   web-admin.sh set-audio <bass dB> <treble dB> <cap step> <onboard|usb> <usb cap dB> <usb mode>
#                                    tone, volume caps, output: audio.conf, applied live
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
LC_ALL=C; export LC_ALL        # [A-Za-z] means ASCII letters, whatever the locale
. /etc/panda/board.conf
. /etc/panda/appliance.conf

die() { echo "web-admin: $*" >&2; exit 2; }
# match <ERE> <value>: the value is one line and matches. (grep tests line by line, so a value
# like "ok<newline>reboot" would pass a plain  grep -Eq '^ok$'  and end up in files root sources.)
match() { [ "$(printf '%s' "$2" | wc -l)" -eq 0 ] && printf '%s' "$2" | grep -Eq "$1"; }
# board.conf SPOTIFY=no: no speaker-soloist service to start or restart.
spotify() { [ "${SPOTIFY:-yes}" != no ]; }
soloist() { spotify || return 0; rc-service speaker-soloist "$@"; }
bg() { setsid "$@" </dev/null >>/var/log/panda-web-admin.log 2>&1 & }

restart_audio() {
	rc-service panda-bt stop; rc-service speaker-shairport stop
	soloist stop; rc-service speaker-wireplumber stop
	rc-service speaker-pipewire stop
	rc-service panda-audio-init restart
	rc-service speaker-pipewire start; rc-service speaker-wireplumber start
	soloist start; rc-service speaker-shairport start
	rc-service speaker-radio start; rc-service speaker-arbiter start
	rc-service panda-bt start
}

case "${1:-}" in
restart)
	case "${2:-}" in
		speaker-soloist) spotify || die "Spotify is off on this board"; bg rc-service "$2" restart ;;
		speaker-shairport|speaker-radio|panda-bt|panda-wifi|avahi-daemon|chronyd) bg rc-service "$2" restart ;;
		bluetooth) bg sh -c 'rc-service bluetooth restart; rc-service panda-bt start' ;;
		*) die "not a restartable service: ${2:-}" ;;
	esac ;;
restart-audio)
	bg "$0" restart-audio-now ;;
bt-reset)
	[ -n "${BT_DRIVER_AFTER_WIFI:-}" ] || die "this board's Bluetooth controller can't be reset by reloading a driver"
	logger -t panda-bt "controller not answering: reloading $BT_DRIVER_AFTER_WIFI"
	# Detached (bg): it stops panda-bt, our caller. The module's own enable line goes low and
	# high again, and the firmware is downloaded again, hence the pause before bluetoothd.
	bg sh -c "rc-service panda-bt stop; rc-service bluetooth stop; rmmod $BT_DRIVER_AFTER_WIFI;
		sleep 2; modprobe $BT_DRIVER_AFTER_WIFI; sleep 12; rc-service bluetooth start; rc-service panda-bt start" ;;
restart-audio-now)
	restart_audio ;;
reboot)
	logger -t panda-web "reboot requested from the web page"
	bg sh -c 'sleep 1; reboot' ;;
poweroff)
	logger -t panda-web "shutdown requested"
	bg sh -c 'sleep 1; poweroff' ;;
save)
	flock /run/panda/media.lock lbu commit && echo saved ;;
lbu-status)
	lbu status 2>&1 || true ;;
chrony)
	chronyc -n tracking 2>&1 || true ;;
logs)
	case "${2:-}" in
		soloist) spotify || die "Spotify is off on this board"; f=/var/log/speaker/$2.log ;;
		pipewire|wireplumber|shairport|arbiter|radio) f=/var/log/speaker/$2.log ;;
		panda-bt|panda-wifi|panda-web|panda-web-admin) f=/var/log/$2.log ;;
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
	match "^[A-Za-z0-9][A-Za-z0-9 ._'-]{0,31}\$" "$name" || die "invalid name"
	[ "$name" = "$SPEAKER_NAME" ] && { echo unchanged; exit 0; }
	tmp=/etc/panda/appliance.conf.new
	awk -v n="$name" '/^SPEAKER_NAME=/ { print "SPEAKER_NAME=\"" n "\""; next } { print }' \
		/etc/panda/appliance.conf > "$tmp" && mv "$tmp" /etc/panda/appliance.conf
	logger -t panda-web "speaker renamed to $name"
	# setup.sh regenerates every config that carries the name and saves (lbu).
	r=""; spotify && r="rc-service speaker-soloist restart && "
	bg sh -c "sh /etc/panda/setup.sh && ${r}rc-service speaker-shairport restart \
		&& rc-service bluetooth restart && rc-service panda-bt start"
	echo renaming ;;
set-soloist-key)
	spotify || die "Spotify is off on this board"
	# One line on stdin. Never echoed, never on a command line.
	IFS= read -r key || true
	key=$(printf '%s' "$key" | tr -d '\r')
	match '^[A-Za-z0-9_.-]{16,200}$' "$key" || die "invalid key format"
	f=/etc/panda/soloist.key
	( umask 077; printf '%s\n' "$key" > "$f.new" ) && chown root:root "$f.new" && mv "$f.new" "$f"
	unset key
	logger -t panda-web "Spotify API key replaced from the web page"
	flock /run/panda/media.lock lbu commit
	rc-service speaker-soloist restart >/dev/null 2>&1 || true
	sleep 6
	if rc-service speaker-soloist status >/dev/null 2>&1; then echo running; else echo "not running"; fi ;;
factory-reset)
	logger -t panda-web "factory reset requested from the web page"
	/etc/panda/factory-reset.sh --dry-run >/dev/null || die "no factory overlay on the card"
	bg /etc/panda/factory-reset.sh
	echo resetting ;;
factory-reset-dry)
	/etc/panda/factory-reset.sh --dry-run 2>&1 || true ;;
set-audio)
	b="${2:-}" t="${3:-}" m="${4:-}" o="${5:-}" u="${6:-}" p="${7:-auto}"
	for v in "$b" "$t"; do
		match '^-?[0-9]{1,2}$' "$v" && [ "$v" -ge -12 ] && [ "$v" -le 12 ] \
			|| die "tone must be an integer -12..12 dB"
	done
	match '^[0-9]{1,3}$' "$m" && [ "$m" -le "${AUDIO_HW_VOLUME_MAX:-0}" ] \
		|| die "cap must be 0..${AUDIO_HW_VOLUME_MAX:-0}"
	case "$o" in onboard|usb) ;; *) die "output must be onboard or usb" ;; esac
	match '^-?[0-9]{1,2}$' "$u" && [ "$u" -ge -30 ] && [ "$u" -le 0 ] \
		|| die "USB cap must be an integer -30..0 dB"
	# "auto" or an ACP profile name (e.g. output:analog-stereo, pro-audio).
	match '^[A-Za-z0-9][A-Za-z0-9:+._-]{0,79}$' "$p" || die "bad USB mode"
	f=/etc/panda/audio.conf
	grep -q '^USB_PROFILE=' "$f" || echo "USB_PROFILE=auto" >> "$f"
	sed -e "s/^BASS_DB=.*/BASS_DB=$b/" -e "s/^TREBLE_DB=.*/TREBLE_DB=$t/" \
		-e "s/^MAX_VOLUME=.*/MAX_VOLUME=$m/" -e "s/^OUTPUT=.*/OUTPUT=$o/" \
		-e "s/^USB_MAX_DB=.*/USB_MAX_DB=$u/" -e "s/^USB_PROFILE=.*/USB_PROFILE=$p/" \
		"$f" > "$f.new" && mv "$f.new" "$f"
	# Boot config, and the running filter (headroom follows the largest boost).
	sh /etc/panda/tone-conf.sh > /etc/pipewire/pipewire.conf.d/20-panda-tone.conf
	mult=$(awk -v b="$b" -v t="$t" 'BEGIN { m = b; if (t > m) m = t; if (m < 0) m = 0
		printf "%.6f", 10 ^ (-(3 + m) / 20) }')
	export PIPEWIRE_RUNTIME_DIR=/run/speaker XDG_RUNTIME_DIR=/run/speaker
	id=$(pw-cli ls Node 2>/dev/null | awk '/^\tid /{ id = $2 } /node.name = "panda-eq"/{ print id + 0; exit }')
	[ -n "$id" ] && pw-cli s "$id" Props "{ params = [ \"bass:Gain\" $b.0 \"treble:Gain\" $t.0 \"pre:Mult\" $mult ] }" >/dev/null 2>&1
	[ -n "${AUDIO_HW_VOLUME_CONTROL:-}" ] && amixer -q -c "$AUDIO_CARD" sset "$AUDIO_HW_VOLUME_CONTROL" "$m"
	# Output choice and the USB cap. As user speaker: PipeWire ignores a root
	# client's metadata writes (the output choice), silently.
	su -s /bin/sh -c 'python3 /etc/panda/audio-route.py' speaker >/dev/null 2>&1 || true
	echo ok ;;
set-master)
	v="${2:-}"
	match '^(0(\.[0-9]{1,3})?|1(\.0{1,3})?)$' "$v" || die "volume must be 0..1"
	f=/etc/panda/audio.conf
	if grep -q '^MASTER_VOLUME=' "$f"; then
		sed "s/^MASTER_VOLUME=.*/MASTER_VOLUME=$v/" "$f" > "$f.new" && mv "$f.new" "$f"
	else
		echo "MASTER_VOLUME=$v" >> "$f"
	fi
	echo ok ;;
ota-status)
	python3 /etc/panda/panda-ota.py status ;;
ota-install)
	logger -t panda-web "update upload started"
	exec python3 /etc/panda/panda-ota.py install ;;
ota-activate)
	case "${2:-}" in a|b) ;; *) die "slot must be a or b" ;; esac
	python3 /etc/panda/panda-ota.py activate "$2" || exit 1
	logger -t panda-web "switching to update slot $2; rebooting"
	bg sh -c 'sleep 2; reboot' ;;
soloist-key-status)
	f=/etc/panda/soloist.key
	if [ -s "$f" ]; then echo "set $(date -r "$f" -u +%Y-%m-%dT%H:%M:%SZ)"; else echo missing; fi ;;
*)
	die "unknown action: ${1:-}" ;;
esac
