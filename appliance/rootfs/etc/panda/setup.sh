#!/bin/sh
# Appliance setup, run on the device as root. Idempotent: safe to run again
# after every deploy (scripts/deploy.sh) and from the first-boot script.
# Reads /etc/panda/board.conf and /etc/panda/appliance.conf, generates the
# board-dependent configs, installs packages, creates service users, enables
# services, and saves everything with lbu.
set -eu
. /etc/panda/board.conf
. /etc/panda/appliance.conf

log() { echo "panda-setup: $*"; logger -t panda-setup "$*" 2>/dev/null || true; }

PACKAGES="alsa-utils pipewire pipewire-alsa pipewire-tools wireplumber shairport-sync nftables
	bluez pipewire-spa-bluez python3 py3-evdev py3-dbus-next
	pipewire-filter-graph pipewire-filter-graph-builtin pipewire-pulse mpv"
# (Alpine ships libpipewire-module-filter-chain in pipewire-pulse; the pulse
# server itself is not started.)

# --- Repositories: boot media first, then the online main + community ----------
rel="v$(cut -d. -f1,2 /etc/alpine-release)"
mirror="${ALPINE_MIRROR:-https://dl-cdn.alpinelinux.org/alpine}"
for repo in main community; do
	grep -qx "$mirror/$rel/$repo" /etc/apk/repositories \
		|| echo "$mirror/$rel/$repo" >> /etc/apk/repositories
done

# --- Packages (the apk cache lives on the read-only boot media) ---------------
missing=""
for p in $PACKAGES; do
	apk info -e "$p" >/dev/null 2>&1 || missing="$missing $p"
done
if [ -n "$missing" ]; then
	log "installing:$missing"
	mount -o remount,rw /media/"$BOOT_MEDIA"
	# Offline (first boot without network) the card's own repository is enough.
	apk update -q || log "apk update failed (offline?); using the card's repository"
	if apk add $missing && apk cache sync; then rc=0; else rc=$?; fi
	sync
	mount -o remount,ro /media/"$BOOT_MEDIA"
	[ "$rc" -eq 0 ] || { log "ERROR: apk add failed"; exit 1; }
fi

# --- Our own packages (aports/, pushed by deploy.sh): install if newer/different ----
if ls /tmp/panda-deploy/apks/*.apk >/dev/null 2>&1; then
	new=""
	for f in /tmp/panda-deploy/apks/*.apk; do
		v=$(basename "$f" .apk)
		[ "$(apk info -e -v "${v%-*-*}" 2>/dev/null)" = "$v" ] || new="$new $f"
	done
	if [ -n "$new" ]; then
		log "installing our packages:$new"
		mount -o remount,rw /media/"$BOOT_MEDIA"
		if apk add -q $new && apk cache sync; then rc=0; else rc=$?; fi
		sync
		mount -o remount,ro /media/"$BOOT_MEDIA"
		[ "$rc" -eq 0 ] || { log "ERROR: installing our packages failed"; exit 1; }
	fi
fi

# --- Device manager: eudev instead of mdev -----------------------------------------
# PipeWire finds sound cards through libudev. With mdev, eudev's libudev reports
# every device as uninitialized and PipeWire skips them. udev also sets the
# /dev/snd permissions and handles USB hotplug (DAC, BT dongle).
if ! rc-update show sysinit 2>/dev/null | grep -q '^ *udev '; then
	log "switching the device manager to eudev"
	mount -o remount,rw /media/"$BOOT_MEDIA"
	if setup-devd udev && apk cache sync; then rc=0; else rc=$?; fi
	sync
	mount -o remount,ro /media/"$BOOT_MEDIA"
	[ "$rc" -eq 0 ] || { log "ERROR: setup-devd udev failed"; exit 1; }
fi

# --- Service user ---------------------------------------------------------------
if ! id speaker >/dev/null 2>&1; then
	log "creating system user speaker"
	addgroup -S speaker
	adduser -S -D -H -h /var/lib/speaker -s /sbin/nologin -G speaker -g "Audio services" speaker
fi
addgroup speaker audio 2>/dev/null || true
# Radio favorites (written by panda-web) are saved with lbu.
mkdir -p /var/lib/panda-web
grep -qx "+var/lib/panda-web/radio-favorites.json" /etc/apk/protected_paths.d/lbu.list 2>/dev/null \
	|| lbu include /var/lib/panda-web/radio-favorites.json
# Web control panel: PipeWire via group speaker, privileged actions only
# through doas + /etc/panda/web-admin.sh (/etc/doas.d/panda-web.conf).
if ! id panda-web >/dev/null 2>&1; then
	log "creating system user panda-web"
	addgroup -S panda-web
	adduser -S -D -H -h /var/lib/panda-web -s /sbin/nologin -G panda-web -g "Web control panel" panda-web
fi
addgroup panda-web speaker 2>/dev/null || true

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

# --- Generated configs: PipeWire, tone filter, WirePlumber, shairport-sync, bluez,
# watchdog (board quirks come from board.conf; see gen-configs.sh).
sh /etc/panda/gen-configs.sh

# --- Spotify Connect: Spotify Soloist (see init.d/speaker-soloist) -----------------
# spotifyd/librespot are blocked by Spotify's audio-key change; remove them.
if apk info -e spotifyd >/dev/null 2>&1; then
	log "removing spotifyd"
	rc-service speaker-spotifyd stop >/dev/null 2>&1 || true
	rc-update del speaker-spotifyd default >/dev/null 2>&1 || true
	apk del -q spotifyd
fi
rm -f /etc/init.d/speaker-spotifyd /etc/spotifyd.conf
# Runtime squashfs, pushed by scripts/deploy.sh to /tmp/panda-deploy/.
soloist_dir=/media/"$BOOT_MEDIA"/soloist
new_rt=/tmp/panda-deploy/soloist-runtime.squashfs
if [ -f "$new_rt" ] && ! cmp -s "$new_rt" "$soloist_dir"/soloist-runtime.squashfs; then
	log "installing the Soloist runtime on the boot media"
	if mountpoint -q /opt/soloist-runtime; then
		rc-service speaker-soloist stop >/dev/null 2>&1 || true
		umount /opt/soloist-runtime
	fi
	mount -o remount,rw /media/"$BOOT_MEDIA"
	if mkdir -p "$soloist_dir" && cp "$new_rt" "$soloist_dir"/soloist-runtime.squashfs.new \
		&& sync && mv "$soloist_dir"/soloist-runtime.squashfs.new "$soloist_dir"/soloist-runtime.squashfs; then
		rc=0; else rc=1; fi
	sync
	mount -o remount,ro /media/"$BOOT_MEDIA"
	[ "$rc" -eq 0 ] || { log "ERROR: installing the Soloist runtime failed"; exit 1; }
fi
[ -x "$soloist_dir"/soloist ] || /etc/panda/soloist-update.sh || log "WARNING: Soloist download failed"
# The key is read by speaker-soloist's start_pre (as root) and passed to Soloist,
# so only root needs to read it. (Not group speaker: panda-web is in that group.)
if [ -s /etc/panda/soloist.key ]; then
	chown root:root /etc/panda/soloist.key && chmod 0600 /etc/panda/soloist.key
else
	log "WARNING: no /etc/panda/soloist.key: Soloist won't start (set it on the web page)"
fi

# --- AirPlay: shairport-sync (config from gen-configs.sh) ------------------------
# The package's own (root) service isn't used: speaker-shairport runs it.
rc-update del shairport-sync default >/dev/null 2>&1 || true

# --- Bluetooth: A2DP sink, pairing via button S2 (see /etc/panda/panda-bt.py) --------
# (main.conf from gen-configs.sh.)
# Pairing keys must survive reboots (saved by panda-bt after each pairing).
grep -qx "+var/lib/bluetooth" /etc/apk/protected_paths.d/lbu.list 2>/dev/null || lbu include /var/lib/bluetooth
# ...but not bluez's remote-name cache, which changes all the time.
for d in /var/lib/bluetooth/*/cache; do
	[ -d "$d" ] || continue
	grep -qx -- "-${d#/}" /etc/apk/protected_paths.d/lbu.list 2>/dev/null || lbu exclude "$d"
done

# --- Factory overlay for factory resets (pushed by deploy.sh from the image) ---------
new_fo=/tmp/panda-deploy/factory.apkovl.tar.gz
fo=/media/"$BOOT_MEDIA"/factory/"$HOSTNAME".apkovl.tar.gz
if [ -f "$new_fo" ] && ! cmp -s "$new_fo" "$fo"; then
	log "installing the factory overlay on the boot media"
	mount -o remount,rw /media/"$BOOT_MEDIA"
	mkdir -p "$(dirname "$fo")" && cp "$new_fo" "$fo.new" && sync && mv "$fo.new" "$fo" \
		|| log "ERROR: installing the factory overlay failed"
	sync
	mount -o remount,ro /media/"$BOOT_MEDIA"
fi

# --- Chimes: synthesized once onto the boot media (slow on this CPU) ----------------
chimes=/media/"$BOOT_MEDIA"/chimes
sum=$(md5sum /etc/panda/chimes.py | cut -d" " -f1)
if [ "$(cat "$chimes"/.source 2>/dev/null)" != "$sum" ]; then
	log "synthesizing the chimes"
	rm -rf /tmp/panda-chimes
	if python3 /etc/panda/chimes.py /tmp/panda-chimes; then
		echo "$sum" > /tmp/panda-chimes/.source
		mount -o remount,rw /media/"$BOOT_MEDIA"
		rm -rf "$chimes".new && cp -r /tmp/panda-chimes "$chimes".new && sync \
			&& rm -rf "$chimes" && mv "$chimes".new "$chimes" || log "ERROR: installing the chimes failed"
		sync
		mount -o remount,ro /media/"$BOOT_MEDIA"
	else
		log "ERROR: chime synthesis failed"
	fi
	rm -rf /tmp/panda-chimes
fi

# --- Boot: console logins only on the serial console (no display on the board) ----
if grep -qE "^tty[1-6]::respawn" /etc/inittab; then
	log "disabling the tty1-6 consoles (serial console only)"
	sed -i -E "s/^(tty[1-6]::respawn)/#\1/" /etc/inittab
	kill -HUP 1
fi

# Start services in parallel (dependencies are declared in every init script).
grep -q '^rc_parallel="YES"' /etc/rc.conf || sed -i 's/^#\?rc_parallel=.*/rc_parallel="YES"/' /etc/rc.conf

# --- Services -------------------------------------------------------------------
rc-update add panda-firewall boot >/dev/null 2>&1 || true
rc-update add panda-reset-button boot >/dev/null 2>&1 || true
if [ -e "${WATCHDOG_DEV:-/dev/watchdog}" ]; then
	rc-update add watchdog boot >/dev/null 2>&1 || true
	rc-service watchdog restart >/dev/null 2>&1 || log "WARNING: the watchdog failed to start"
fi
sysctl -q -p /etc/sysctl.d/90-panda.conf || true
for svc in panda-audio-init speaker-pipewire speaker-wireplumber speaker-soloist \
	speaker-shairport speaker-radio speaker-arbiter bluetooth panda-bt panda-chime-boot panda-web crond; do
	rc-update add "$svc" default >/dev/null 2>&1 || true
done
# Load/refresh the firewall now (also on redeploys with changed rules).
rc-service panda-firewall restart >/dev/null 2>&1 || log "WARNING: firewall failed to load"

# apk's built-in protected paths keep only symlinks under etc/init.d
# ("@etc/init.d"), so lbu would save the runlevel links but not our scripts,
# and the services vanish on reboot. Include them explicitly.
for f in /etc/init.d/panda-* /etc/init.d/speaker-*; do
	[ -f "$f" ] || continue
	grep -qx "+${f#/}" /etc/apk/protected_paths.d/lbu.list 2>/dev/null || lbu include "$f"
done

log "done; saving with lbu"
lbu commit
