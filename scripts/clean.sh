#!/bin/bash
# Remove build output, local packages, source caches and this project's podman
# containers/images, to get back to a fresh checkout.
#
#   scripts/clean.sh [-n] [-y] [--keep-downloads] [--all]
#     -n, --dry-run       show what would be removed, remove nothing
#     -y, --yes           don't ask for confirmation
#     --keep-downloads    keep build/distfiles (kernel/upstream tarballs)
#     --all               also remove the pulled base images (alpine, fedora, debian)
#
# Never touched: keys/ (abuild signing keys), appliance/wifi.json,
# appliance/authorized_keys, drivers/ submodule, anything tracked by git.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

dry=0 yes=0 keep_dl=0 all=0
for a in "$@"; do
    case "$a" in
        -n|--dry-run)     dry=1 ;;
        -y|--yes)         yes=1 ;;
        --keep-downloads) keep_dl=1 ;;
        --all)            all=1 ;;
        -h|--help)        sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option '$a' (see --help)" >&2; exit 1 ;;
    esac
done

# --- what to remove -------------------------------------------------------
paths=()
if [ -d "$PROJECT_DIR/build" ]; then
    if [ "$keep_dl" = 1 ]; then
        while IFS= read -r -d '' p; do paths+=("$p"); done \
            < <(find "$PROJECT_DIR/build" -mindepth 1 -maxdepth 1 ! -name distfiles -print0)
    else
        paths+=("$PROJECT_DIR/build")
    fi
fi
[ -d "$PROJECT_DIR/packages" ] && paths+=("$PROJECT_DIR/packages")
while IFS= read -r -d '' p; do paths+=("$p"); done < <(
    find "$PROJECT_DIR/aports" -mindepth 2 -maxdepth 2 -type d \( -name src -o -name pkg \) -print0 2>/dev/null
    find "$PROJECT_DIR" -path "$PROJECT_DIR/.git" -prune -o -type d -name __pycache__ -print0
)

command -v podman >/dev/null && have_podman=1 || have_podman=0
images=()
if [ "$have_podman" = 1 ]; then
    pat='^localhost/panda-'
    [ "$all" = 1 ] && pat+='|^docker\.io/library/(alpine|debian):|^registry\.fedoraproject\.org/fedora:'
    mapfile -t images < <(podman images --format '{{.Repository}}:{{.Tag}}' | grep -E "$pat" || true)
    containers=()
    for img in "${images[@]}"; do
        mapfile -t -O "${#containers[@]}" containers < <(podman ps -aq --filter "ancestor=$img")
    done
    mapfile -t dangling < <(podman images -q --filter dangling=true)
fi

# --- report ---------------------------------------------------------------
echo "Files:"
if [ "${#paths[@]}" = 0 ]; then echo "  (nothing)"; else
    du -sh "${paths[@]}" 2>/dev/null | sed "s|$PROJECT_DIR/|  |"
fi
if [ "$have_podman" = 1 ]; then
    echo "Podman:"
    echo "  containers: ${#containers[@]}"
    printf '  image: %s\n' "${images[@]}"
    echo "  dangling images: ${#dangling[@]}  (+ build cache)"
else
    echo "Podman: not installed, skipping"
fi

[ "$dry" = 1 ] && { echo "(dry run, nothing removed)"; exit 0; }
if [ "$yes" = 0 ]; then
    read -r -p "Remove all of the above? [y/N] " ans
    [[ "$ans" =~ ^[yY] ]] || { echo "aborted"; exit 1; }
fi

# --- remove ---------------------------------------------------------------
for p in "${paths[@]}"; do
    # Files written as a subordinate uid inside a container can't be removed
    # by the host user directly; retry inside the rootless user namespace.
    rm -rf -- "$p" 2>/dev/null || { [ "$have_podman" = 1 ] && podman unshare rm -rf -- "$p"; }
done

if [ "$have_podman" = 1 ]; then
    [ "${#containers[@]}" -gt 0 ] && podman rm -f "${containers[@]}" >/dev/null
    [ "${#images[@]}" -gt 0 ] && podman rmi -f "${images[@]}" >/dev/null
    podman image prune -f >/dev/null          # dangling layers from rebuilds
    podman builder prune -f >/dev/null 2>&1 || true   # buildah layer cache
fi

echo "Done."
[ "$have_podman" = 1 ] && podman system df
