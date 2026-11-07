#!/bin/bash
# The audio-graph integration suite (TESTING.md section 12): real PipeWire, WirePlumber, mpv,
# panda-arbiter and panda-web in a rootless podman container with a null audio sink.
#   scripts/test-graph.sh                 quick tier (not soak)
#   scripts/test-graph.sh -k transitions  extra arguments go to pytest
#   scripts/test-graph.sh tests/graph/test_failures.py   only these files
#   RIG_SHELL=1 scripts/test-graph.sh     a shell in the rig instead
# Failure artifacts (logs, pw-dump, summary) land in build/graph-artifacts/.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ALPINE_VERSION=$(sed -n 's/^ALPINE_VERSION=//p' "$PROJECT_DIR/scripts/env.sh" | head -1)
IMAGE="localhost/panda-graph-rig:${ALPINE_VERSION}"
podman build -q -t "$IMAGE" --build-arg "ALPINE_VERSION=$ALPINE_VERSION" \
    -f "$PROJECT_DIR/tests/graph/Containerfile" "$PROJECT_DIR/tests/graph" >/dev/null
mkdir -p "$PROJECT_DIR/build/graph-artifacts"
rm -rf "$PROJECT_DIR"/build/graph-artifacts/*
# Arguments that are paths under tests/graph select those files; otherwise the whole suite runs.
paths=(); rest=()
for a in "$@"; do case "$a" in tests/graph*) paths+=("$a") ;; *) rest+=("$a") ;; esac; done
[ ${#paths[@]} -eq 0 ] && paths=(tests/graph)
cmd=(python3 -m pytest -p no:cacheprovider "${paths[@]}" -m "not soak" -o testpaths=tests/graph "${rest[@]}")
[ -n "${RIG_SHELL:-}" ] && cmd=(sh)
tty=(); [ -t 0 ] && tty=(-it)
exec podman run --rm "${tty[@]}" \
    -e RIG=1 -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$PROJECT_DIR":/repo:ro,z -v "$PROJECT_DIR/build/graph-artifacts":/artifacts:z \
    -w /repo "$IMAGE" "${cmd[@]}"
