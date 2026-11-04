# Alpine diskless audio appliance

Alpine Linux in diskless mode as a network/Bluetooth speaker. The root filesystem
lives in RAM and the boot media stays read-only, so the power can be cut at any
time. The first board is a PandaBoard rev A1 (OMAP4430). Board-specific parts
live in `boards/<board>/`, and everything else is board-independent.

See [PLAN.md](PLAN.md) for the design, decisions, hardware findings and status.

## Build (Fedora host, rootless podman)

```sh
scripts/build-container.sh                       # target-arch Alpine build container
scripts/setup-keys.sh                            # abuild signing key (once; back up keys/*.rsa)
boards/pandaboard/u-boot/build.sh                # U-Boot v2024.07 (needs gcc-arm-linux-gnu)
scripts/build-package.sh linux-pandaboard        # kernel, cross-compiled (~10 min)
scripts/build-image.sh                           # diskless image into build/sdimage/
sudo boards/pandaboard/write-sdcard.sh /dev/sdX build/sdimage   # erases the card
```

Then boot with the serial console (115200 8N1), set a root password for the
console (`passwd`, `lbu commit`), and log in with `ssh panda@panda.local`.

Other boards: `BOARD=<name>` selects `boards/<name>/`. That directory needs a
`board.conf`, an `mkimg.<name>.sh` profile and a card writer.

## Debugging

`scripts/serial-sysrq.py [keys]` sends Magic SysRq over the serial console and
saves the output to `build/serial-sysrq.log` (`w` blocked tasks, `l` CPUs,
`t` all tasks). Quit picocom first, and don't run it with sudo.
