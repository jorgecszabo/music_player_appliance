Board-specific aports (e.g. `linux-<board>` with a kernel config fragment) go here.
`scripts/build-package.sh <name>` prefers `boards/<board>/aports/<name>` over the
generic `aports/<name>`. If Alpine's own kernel flavor (e.g. `linux-lts`) works for the
board, you don't need one.

For a cross-compiled kernel, copy `boards/pandaboard/aports/linux-pandaboard/`:
the APKBUILD sets `_flavor`, `_defconfig`, `_fragment`, `_dtbs` and
`_kconfig_check`, and its `cross-build.sh` hands the aport to
`scripts/cross-build-kernel.sh`, which builds into `build/kernel-staging/<flavor>/`.
It also builds the generic out-of-tree drivers in `drivers/` (`DEFAULT_EXTMODS`
in that script; today `r92su`, USB Wi-Fi, a git submodule of
github.com/jorgecszabo/rtl8192su) for the kernel; each one's
`kconfig.require` lists the options the fragment must enable. Set
`_extmods_skip="drivers/<name>"` in the APKBUILD to leave one out, and
`_extmods="drivers/<name>"` to add a driver only this board uses. Alpine's stock
kernels don't get these drivers.
