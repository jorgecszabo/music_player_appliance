#!/bin/sh
# Play an appliance chime: chime.sh <event> [--wait]
# Events: boot pairing-on paired connected disconnected pairing-off forgot-all
# The WAVs are synthesized by /etc/panda/chimes.py on the build host and come
# with the image/update: the booted slot's chimes/ (OTA), else <boot>/chimes. Level: CHIME_VOLUME from appliance.conf (linear stream
# volume, mixed over any music; the hardware gain cap still applies).
. /etc/panda/board.conf
. /etc/panda/appliance.conf
slot=$(cat /run/panda/slot 2>/dev/null)
f=/media/"$BOOT_MEDIA"/slots/"$slot"/chimes/"$1".wav
[ -n "$slot" ] && [ -f "$f" ] || f=/media/"$BOOT_MEDIA"/chimes/"$1".wav
[ -n "$1" ] || { echo "usage: $0 <event> [--wait]" >&2; exit 1; }
[ -f "$f" ] || { echo "no chime $f" >&2; exit 1; }
play() {
	PIPEWIRE_RUNTIME_DIR=/run/speaker XDG_RUNTIME_DIR=/run/speaker \
		pw-play --volume "${CHIME_VOLUME:-0.3}" --media-role Notification \
		-P '{ node.name = "chime" media.name = "chime" }' "$f"
}
if [ "$2" = "--wait" ]; then play; else play </dev/null >/dev/null 2>&1 & fi
