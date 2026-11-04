Board-specific aports (e.g. `linux-<board>` with a kernel config fragment) go here.
`scripts/build-package.sh <name>` prefers `boards/<board>/aports/<name>` over the
generic `aports/<name>`. If Alpine's own kernel flavor (e.g. `linux-lts`) works for the
board, you don't need one.

For a cross-compiled kernel, copy `boards/pandaboard/aports/linux-pandaboard/`:
the APKBUILD sets `_flavor`, `_defconfig`, `_fragment`, `_dtbs` and
`_kconfig_check`, and its `cross-build.sh` hands the aport to
`scripts/cross-build-kernel.sh`, which builds into `build/kernel-staging/<flavor>/`.
