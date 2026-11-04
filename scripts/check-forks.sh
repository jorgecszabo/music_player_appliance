#!/bin/bash
# Report which of our own packages are behind their upstream. We hold these
# back on purpose (pkgrel 100 wins over Alpine's build of the same version, and
# setup.sh keeps !mesa/!libcamera in the world), so upstream security fixes
# only reach the boards when we rebuild.
#   - Alpine forks (aports/*, boards/*/aports/* with _upstream=<repo>/<name> and
#     _upstream_base=<ver>-r<rel>): compared with Alpine's current package in
#     v$ALPINE_VERSION for $ALPINE_ARCH.
#   - Board kernels (APKBUILDs with _defconfig, see scripts/cross-build-kernel.sh):
#     compared with kernel.org's newest release in the same major.minor series.
# Usage: [BOARD=<board>] scripts/check-forks.sh
# Exit: 0 all current, 1 something is behind, 2 upstream couldn't be checked.
set -euo pipefail
source "$(dirname "$0")/env.sh"
MIRROR="${ALPINE_MIRROR:-https://dl-cdn.alpinelinux.org/alpine}"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

apkvar() { sed -n "s/^$1=//p" "$2" | tr -d '"' | head -1; }
# Newer of two versions (sort -V copes with x.y.z and -rN).
newer() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)" = "$1" ]; }

fetch_index() {   # repo -> path of its APKINDEX (downloaded once)
    local f="$TMP/$1/APKINDEX"
    if [ ! -f "$f" ]; then
        mkdir -p "$TMP/$1"
        curl -sfL -o "$TMP/$1/APKINDEX.tar.gz" \
            "$MIRROR/v$ALPINE_VERSION/$1/$ALPINE_ARCH/APKINDEX.tar.gz" || return 1
        tar -xzf "$TMP/$1/APKINDEX.tar.gz" -C "$TMP/$1" APKINDEX || return 1
    fi
    echo "$f"
}

behind=0 failed=0
report() { printf '%-22s %-14s %-14s %s\n' "$@"; }
report PACKAGE OURS UPSTREAM STATUS

for a in "$PROJECT_DIR"/aports/*/APKBUILD "$PROJECT_DIR"/boards/*/aports/*/APKBUILD; do
    [ -f "$a" ] || continue
    name=$(apkvar pkgname "$a"); name=${name//\$_flavor/$(apkvar _flavor "$a")}
    ours="$(apkvar pkgver "$a")-r$(apkvar pkgrel "$a")"
    up=$(apkvar _upstream "$a")
    if [ -n "$up" ]; then
        base=$(apkvar _upstream_base "$a")
        [ -n "$base" ] || { report "$name" "$ours" "?" "no _upstream_base in ${a#"$PROJECT_DIR"/}"; failed=1; continue; }
        repo=${up%%/*} upname=${up#*/}
        if ! idx=$(fetch_index "$repo"); then
            report "$name" "$ours" "?" "can't fetch Alpine $repo index"; failed=1; continue
        fi
        cur=$(awk -v RS= -v p="$upname" '$0 ~ "(^|\n)P:" p "\n" { if (match($0, /\nV:[^\n]+/)) print substr($0, RSTART + 3, RLENGTH - 3) }' "$idx" | head -1)
        if [ -z "$cur" ]; then
            report "$name" "$ours" "-" "not in Alpine $repo any more"; behind=1
        elif [ "$cur" = "$base" ]; then
            report "$name" "$ours" "$cur" "ok (forked from $base)"
        elif newer "$cur" "$base"; then
            report "$name" "$ours" "$cur" "BEHIND: forked from $base; rebase on Alpine's $up"; behind=1
        else
            report "$name" "$ours" "$cur" "ok (Alpine is older than our base $base)"
        fi
    elif [ -n "$(apkvar _defconfig "$a")" ]; then
        ver=$(apkvar pkgver "$a"); series=${ver%.*}
        if [ ! -f "$TMP/releases.json" ]; then
            curl -sfL -o "$TMP/releases.json" https://www.kernel.org/releases.json || true
        fi
        cur=$(python3 -I -c '
import json, sys
try:
    rel = json.load(open(sys.argv[1]))["releases"]
except Exception:
    sys.exit(0)
for r in rel:
    if r["version"].startswith(sys.argv[2] + "."):
        print(r["version"]); break' "$TMP/releases.json" "$series" 2>/dev/null || true)
        if [ -z "$cur" ]; then
            report "$name" "$ours" "?" "can't find $series.x on kernel.org (end of life?)"; failed=1
        elif newer "$cur" "$ver"; then
            report "$name" "$ours" "$cur" "BEHIND: new $series.x LTS; bump pkgver, pkgrel=0"; behind=1
        else
            report "$name" "$ours" "$cur" "ok"
        fi
    fi
done

[ "$failed" = 0 ] || exit 2
[ "$behind" = 0 ] || exit 1
