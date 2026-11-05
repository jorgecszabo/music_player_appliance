#!/bin/sh
# Appliance setup, run on the device as root: at every boot by the panda-setup
# service (--boot), and again after a rename (web-admin.sh set-name).
# Idempotent, offline, no package installs: the packages come from the booted
# slot's repository (world: panda-appliance panda-board-<board>), the code from
# the panda-appliance package. This makes the running system match this
# version: generated configs (gen-configs.sh), service users, setting defaults,
# enabled services. The lbu overlay holds settings only (PLAN.md, OTA updates).
#
#   setup.sh          regenerate and save (lbu commit)
#   setup.sh --boot   at boot: save only when this version runs for the first
#                     time and isn't on OTA trial (panda-ota-confirm saves then)
set -eu
. /etc/panda/board.conf
. /etc/panda/appliance.conf
BOOT=""
[ "${1:-}" = "--boot" ] && BOOT=1

log() { echo "panda-setup: $*"; logger -t panda-setup "$*" 2>/dev/null || true; }

# --- Repositories: the booted slot's (panda-ota boot), then the online ones -------
rel="v$(cut -d. -f1,2 /etc/alpine-release)"
mirror="${ALPINE_MIRROR:-https://dl-cdn.alpinelinux.org/alpine}"
for repo in main community; do
	grep -qx "$mirror/$rel/$repo" /etc/apk/repositories 2>/dev/null \
		|| echo "$mirror/$rel/$repo" >> /etc/apk/repositories
done
# No apk cache on the card: the slot's repository has every package, and a cache
# shared by both slots would mix versions.
[ -L /etc/apk/cache ] && rm -f /etc/apk/cache

# --- What lbu saves: settings only (rules in /etc/panda/lbu-paths.list). Copied
# every boot: apk audit reports everything under etc/apk/ as changed, so a file
# shipped there would always land in the overlay and shadow later versions.
mkdir -p /etc/apk/protected_paths.d
grep -v "^#" /etc/panda/lbu-paths.list > /etc/apk/protected_paths.d/panda.list

# --- Overlays from before the package split: code that would shadow the package ---
# Older overlays saved the appliance's init scripts explicitly; drop those lines.
if [ -f /etc/apk/protected_paths.d/lbu.list ]; then
	sed -i -e '/^+etc\/init\.d\/panda-/d' -e '/^+etc\/init\.d\/speaker-/d' \
		/etc/apk/protected_paths.d/lbu.list
fi

# --- Service users ------------------------------------------------------------------
if ! id speaker >/dev/null 2>&1; then
	log "creating system user speaker"
	addgroup -S speaker
	adduser -S -D -H -h /var/lib/speaker -s /sbin/nologin -G speaker -g "Audio services" speaker
fi
addgroup speaker audio 2>/dev/null || true
mkdir -p /var/lib/panda-web
# Web control panel: PipeWire via group speaker, privileged actions only
# through doas + /etc/panda/web-admin.sh (/etc/doas.d/panda-web.conf).
if ! id panda-web >/dev/null 2>&1; then
	log "creating system user panda-web"
	addgroup -S panda-web
	adduser -S -D -H -h /var/lib/panda-web -s /sbin/nologin -G panda-web -g "Web control panel" panda-web
fi
addgroup panda-web speaker 2>/dev/null || true
chown panda-web:panda-web /var/lib/panda-web
# Front panel (board.conf LCD_FB): framebuffer, buttons and touch, PipeWire for
# the spectrum. Its calibration is saved (panda.list).
if [ -n "${LCD_FB:-}" ]; then
	if ! id panda-lcd >/dev/null 2>&1; then
		log "creating system user panda-lcd"
		addgroup -S panda-lcd
		adduser -S -D -H -h /var/lib/panda-lcd -s /sbin/nologin -G panda-lcd -g "Front panel" panda-lcd
	fi
	for g in video input speaker; do addgroup panda-lcd "$g" 2>/dev/null || true; done
	mkdir -p /var/lib/panda-lcd
	chown panda-lcd:panda-lcd /var/lib/panda-lcd
fi

# --- Tone, headroom and volume cap (/etc/panda/audio.conf, the owner's settings) ---
# Created with defaults once; the web page changes it (web-admin.sh set-audio).
if [ ! -f /etc/panda/audio.conf ]; then
	cat > /etc/panda/audio.conf <<EOF
# Owner's audio settings (web page). Shell syntax.
# Bass/treble shelves in dB (-12..12).
BASS_DB=0
TREBLE_DB=0
# Volume cap: hardware gain step (0..AUDIO_HW_VOLUME_MAX in board.conf).
MAX_VOLUME=${AUDIO_HW_VOLUME_MAX:-15}
EOF
fi
# Settings added since: appended with their defaults to an older audio.conf.
grep -q '^OUTPUT=' /etc/panda/audio.conf || cat >> /etc/panda/audio.conf <<EOF
# Output: onboard (the board's jack) or usb (a USB audio card, when plugged in;
# the onboard output otherwise). A board without onboard audio uses USB only.
OUTPUT=$([ -n "${AUDIO_CARD:-}" ] && echo onboard || echo usb)
EOF
grep -q '^USB_MAX_DB=' /etc/panda/audio.conf || cat >> /etc/panda/audio.conf <<EOF
# USB output volume cap in dB (-30..0, software).
USB_MAX_DB=0
EOF
grep -q '^USB_PROFILE=' /etc/panda/audio.conf || cat >> /etc/panda/audio.conf <<EOF
# USB card output mode: auto, or an ACP profile name (output:analog-stereo,
# output:iec958-stereo, pro-audio, ...; the web page lists the card's).
USB_PROFILE=auto
EOF
grep -q '^MASTER_VOLUME=' /etc/panda/audio.conf || cat >> /etc/panda/audio.conf <<EOF
# Master volume (web page slider, 0..1 on its cubic scale), restored at boot.
MASTER_VOLUME=1.0
EOF
# A cap above the board's maximum (board.conf corrected since) is lowered to it.
cap=$(sed -n 's/^MAX_VOLUME=//p' /etc/panda/audio.conf)
if [ -n "${AUDIO_HW_VOLUME_MAX:-}" ] && [ "${cap:-0}" -gt "$AUDIO_HW_VOLUME_MAX" ] 2>/dev/null; then
	log "volume cap $cap is above the board maximum $AUDIO_HW_VOLUME_MAX: lowering it"
	sed -i "s/^MAX_VOLUME=.*/MAX_VOLUME=$AUDIO_HW_VOLUME_MAX/" /etc/panda/audio.conf
fi

# --- Generated configs: PipeWire, tone filter, WirePlumber, shairport-sync, bluez,
# watchdog (board quirks come from board.conf; see gen-configs.sh). Rebuilt every
# boot and kept out of lbu (/etc/apk/protected_paths.d/panda.list).
sh /etc/panda/gen-configs.sh

# --- Spotify: the key is read by speaker-soloist's start_pre (as root) only -------
# board.conf SPOTIFY=no (no Soloist build for this arch): the service is left out.
soloist=speaker-soloist
if [ "${SPOTIFY:-yes}" = no ]; then
	soloist=""
	rc-update del speaker-soloist default >/dev/null 2>&1 || true
elif [ -s /etc/panda/soloist.key ]; then
	chown root:root /etc/panda/soloist.key && chmod 0600 /etc/panda/soloist.key
else
	log "no /etc/panda/soloist.key: Soloist won't start (set it on the web page)"
fi

# --- AirPlay: speaker-shairport runs it, not the package's own (root) service ---
rc-update del shairport-sync default >/dev/null 2>&1 || true

# --- Bluetooth: pairings are saved (panda.list), bluez's name cache is not ---------
for d in /var/lib/bluetooth/*/cache; do
	[ -d "$d" ] || continue
	grep -qx -- "-${d#/}" /etc/apk/protected_paths.d/lbu.list 2>/dev/null || lbu exclude "$d"
done

# --- Combo Wi-Fi/BT chip: Wi-Fi powers up first (see /etc/init.d/panda-radio-init) ----
if [ -n "${BT_DRIVER_AFTER_WIFI:-}" ]; then
	echo "# Loaded by panda-radio-init after the onboard Wi-Fi is up (shared chip)." > /etc/modprobe.d/panda-radio.conf
	echo "blacklist $BT_DRIVER_AFTER_WIFI" >> /etc/modprobe.d/panda-radio.conf
else
	rm -f /etc/modprobe.d/panda-radio.conf
fi

# --- Wi-Fi: panda-wifi runs wpa_supplicant itself (see /etc/panda/panda-wifi.py) -----
rc-update del wpa_supplicant boot >/dev/null 2>&1 || true
rc-update del wpa_supplicant default >/dev/null 2>&1 || true
[ -f /etc/panda/wifi.json ] && chown root:root /etc/panda/wifi.json && chmod 0600 /etc/panda/wifi.json

# --- Boot: console logins only on the serial console (no display on the board) ----
if grep -qE "^tty[1-6]::respawn" /etc/inittab; then
	log "disabling the tty1-6 consoles (serial console only)"
	sed -i -E "s/^(tty[1-6]::respawn)/#\1/" /etc/inittab
	kill -HUP 1
fi
# Start services in parallel (dependencies are declared in every init script).
grep -q '^rc_parallel="YES"' /etc/rc.conf || sed -i 's/^#\?rc_parallel=.*/rc_parallel="YES"/' /etc/rc.conf

# --- Services -------------------------------------------------------------------
for svc in panda-setup panda-ota-boot panda-firewall panda-reset-button; do
	rc-update add "$svc" boot >/dev/null 2>&1 || true
done
[ -e "${WATCHDOG_DEV:-/dev/watchdog}" ] && rc-update add watchdog boot >/dev/null 2>&1 || true
for svc in panda-audio-init speaker-pipewire speaker-wireplumber $soloist \
	speaker-shairport speaker-radio speaker-arbiter panda-radio-init bluetooth panda-bt \
	panda-wifi panda-chime-boot panda-web panda-ota-confirm crond; do
	rc-update add "$svc" default >/dev/null 2>&1 || true
done
if [ -n "${LCD_FB:-}" ]; then
	rc-update add panda-lcd default >/dev/null 2>&1 || true
else
	rc-update del panda-lcd default >/dev/null 2>&1 || true
fi
if [ -z "$BOOT" ]; then
	sysctl -q -p /etc/sysctl.d/90-panda.conf || true
	rc-service panda-firewall restart >/dev/null 2>&1 || log "WARNING: firewall failed to load"
fi

# --- Save -------------------------------------------------------------------------
# lbu saves to the boot medium (first boot: lbu.conf + fstab entry).
grep -q "^LBU_MEDIA=$BOOT_MEDIA\$" /etc/lbu/lbu.conf 2>/dev/null || setup-lbu "$BOOT_MEDIA"
ver=$(cat /etc/panda/version 2>/dev/null || echo unknown)
if [ -n "$BOOT" ]; then
	[ "$(cat /etc/panda/setup.stamp 2>/dev/null)" = "$ver" ] && exit 0
	slot=$(cat /run/panda/slot 2>/dev/null || true)
	if [ -n "$slot" ] && grep -qx "trial=$slot" /media/"$BOOT_MEDIA"/slots/state 2>/dev/null; then
		log "version $ver on trial: panda-ota-confirm saves once it's healthy"
		echo "$ver" > /etc/panda/setup.stamp
		exit 0
	fi
	echo "$ver" > /etc/panda/setup.stamp
	log "first boot of version $ver; saving with lbu"
else
	log "done; saving with lbu"
fi
mkdir -p /run/panda
flock /run/panda/media.lock lbu commit
