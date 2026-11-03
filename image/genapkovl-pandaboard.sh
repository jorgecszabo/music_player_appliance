#!/bin/sh -e
# First-boot overlay for the PandaBoard image: hostname, DHCP on eth0,
# NTP, and lbu configured to save to the boot SD card.
HOSTNAME="$1"
[ -n "$HOSTNAME" ] || { echo "usage: $0 hostname"; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

makefile() {
	OWNER="$1"; PERMS="$2"; FILENAME="$3"
	cat > "$FILENAME"
	chown "$OWNER" "$FILENAME"
	chmod "$PERMS" "$FILENAME"
}

rc_add() {
	mkdir -p "$tmp"/etc/runlevels/"$2"
	ln -sf /etc/init.d/"$1" "$tmp"/etc/runlevels/"$2"/"$1"
}

mkdir -p "$tmp"/etc/network "$tmp"/etc/apk "$tmp"/etc/lbu

makefile root:root 0644 "$tmp"/etc/hostname <<EOT
$HOSTNAME
EOT

makefile root:root 0644 "$tmp"/etc/network/interfaces <<EOT
auto lo
iface lo inet loopback

auto eth0
iface eth0 inet dhcp
EOT

makefile root:root 0644 "$tmp"/etc/apk/world <<EOT
alpine-base
chrony
EOT

# Save the overlay to the boot card (mounted read-only except during commit).
makefile root:root 0644 "$tmp"/etc/lbu/lbu.conf <<EOT
LBU_MEDIA=mmcblk0p1
EOT

rc_add devfs sysinit
rc_add dmesg sysinit
rc_add mdev sysinit
rc_add hwdrivers sysinit
rc_add modloop sysinit

rc_add hwclock boot
rc_add modules boot
rc_add sysctl boot
rc_add hostname boot
rc_add bootmisc boot
rc_add syslog boot
rc_add networking boot

rc_add chronyd default

rc_add mount-ro shutdown
rc_add killprocs shutdown
rc_add savecache shutdown

tar -c -C "$tmp" etc | gzip -9n > "$HOSTNAME".apkovl.tar.gz
