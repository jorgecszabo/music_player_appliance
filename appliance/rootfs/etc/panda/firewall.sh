#!/bin/sh
# Appliance firewall helper.
#   firewall.sh start    load /etc/panda/firewall.nft, then update
#   firewall.sh update   put the current on-link IPv6 prefixes (learned from
#                        router ads, so they can change) into set lan6_ra
# Run by /etc/init.d/panda-firewall and every 15 minutes by cron.
set -eu

update() {
	nft list table inet panda >/dev/null 2>&1 || return 0
	# On-link routes: "2001:db8:1::/64 dev eth0 ..." (no "via", not
	# link-local/multicast/local).
	prefixes=$(ip -6 route show 2>/dev/null | awk '
		$1 ~ /\// && $1 !~ /^(fe80|ff)/ && !/ via / && !seen[$1]++ {
			printf "%s%s", (n++ ? ", " : ""), $1 }')
	{
		echo "flush set inet panda lan6_ra"
		[ -n "$prefixes" ] && echo "add element inet panda lan6_ra { $prefixes }"
	} | nft -f -
}

case "${1:-}" in
	start)  nft -f /etc/panda/firewall.nft; update ;;
	update) update ;;
	*) echo "usage: $0 start|update" >&2; exit 1 ;;
esac
