#!/bin/bash
# Static checks and the unit/fuzz tests (T0 + T1 of TESTING.md). No board, no container.
#   scripts/check.sh            everything
#   scripts/check.sh -k radio   extra arguments go to pytest
# Needs: python3 with pytest, hypothesis, ruff, shellcheck-py (pip install -r tests/requirements.txt,
# in .venv/ by default: python3 -m venv .venv && .venv/bin/pip install -r tests/requirements.txt).
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1
BIN="$PROJECT_DIR/.venv/bin"
[ -x "$BIN/python3" ] || BIN=""
tool() { if [ -n "$BIN" ] && [ -x "$BIN/$1" ]; then echo "$BIN/$1"; else command -v "$1" || true; fi; }
PY="$(tool python3)"
fail=0
step() { echo; echo "== $*"; }
run() { "$@" || { echo "FAILED: $*"; fail=1; }; }

step "ruff (undefined names, unused variables, syntax errors)"
if RUFF="$(tool ruff)" && [ -n "$RUFF" ]; then run "$RUFF" check --quiet . && echo ok; else echo "ruff not installed: skipped"; fi

step "shellcheck (build scripts, appliance scripts, OpenRC scripts)"
if SC="$(tool shellcheck)" && [ -n "$SC" ]; then
    # SC2034: OpenRC variables look unused; SC3043: busybox ash has 'local';
    # SC3028: HOSTNAME is set by busybox ash; SC2155/SC2148/SC2054: style in build scripts.
    run "$SC" -S warning -e SC2034,SC3043,SC3028,SC2155,SC2148,SC2054 \
        scripts/*.sh appliance/rootfs/etc/panda/*.sh
    run "$SC" -s sh -S warning -e SC2034,SC3043,SC3028,SC2155 appliance/rootfs/etc/init.d/*
    echo ok
else echo "shellcheck not installed: skipped"; fi

step "nftables syntax"
if command -v nft >/dev/null && command -v unshare >/dev/null; then
    # In a throwaway user+network namespace: no privileges needed.
    run unshare -rn nft -c -f appliance/rootfs/etc/panda/firewall.nft && echo ok
else echo "nft or unshare not installed: skipped"; fi

step "pytest: unit, consistency and fuzz tests"
run "$PY" -m pytest "$@"

echo
[ "$fail" = 0 ] && echo "all checks passed" || { echo "SOME CHECKS FAILED"; exit 1; }
