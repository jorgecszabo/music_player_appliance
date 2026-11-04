Board-specific aports (e.g. `linux-<board>` with a kernel config fragment) go here.
`scripts/build-package.sh <name>` prefers `boards/<board>/aports/<name>` over the
generic `aports/<name>`. If Alpine's own kernel flavor (e.g. `linux-lts`) works for the
board, you don't need one. See `boards/pandaboard/aports/linux-pandaboard/` for a
cross-compiled kernel with a fragment.
