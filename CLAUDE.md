# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A network speaker appliance (Spotify Connect via Soloist, AirPlay, Bluetooth A2DP, internet radio)
for old ARM boards, built as a diskless Alpine Linux image. README.md is the user guide and the only
committed documentation. The other markdown files in the root (PLAN.md design log, TESTING.md test
plan, SQUASHFS-ROOT.md boot/root layout, PCDUINO.md and RPI1.md bring-up notes, ...) are untracked
local notes. They may be missing from a fresh clone.

## Commands

Host is Fedora with rootless podman. `BOARD=<name>` (default `pandaboard`; also `beaglexm`,
`pcduino`, `rpi1`) is read by every script through `scripts/env.sh`.

```sh
# Test tooling, once
python3 -m venv .venv && .venv/bin/pip install -r tests/requirements.txt

scripts/check.sh                  # ruff + shellcheck + nft syntax + unit/consistency/fuzz tests (~2.5 min)
scripts/check.sh -k radio         # extra args go to pytest
.venv/bin/python -m pytest tests/unit/test_arbiter.py -k pause   # a single test directly
FUZZ_EXAMPLES=1000 scripts/check.sh tests/fuzz                   # longer fuzz run
scripts/test-graph.sh             # audio-graph integration suite in a podman container (~4 min)
scripts/test-graph.sh tests/graph/test_failures.py -k hung
RIG_SHELL=1 scripts/test-graph.sh # shell inside the graph rig
scripts/test-ota.sh               # OTA slots against build/sdimage/<board>

scripts/build-image.sh            # SD image -> build/sdimage/$BOARD/ (needs container + packages; see README)
scripts/build-ota.sh              # signed update bundle -> build/ota/panda-$BOARD-<version>.pota
scripts/build-package.sh <aport>  # shairport-sync, panda-spectrum, linux-$BOARD
scripts/build-demo.sh             # regenerate docs/panel-demo.html + .png after any web page change
scripts/check-forks.sh            # forks/kernels behind upstream

# On a real board (test PandaBoard: 192.168.0.55 eth)
PANDA_HOST=panda@192.168.0.55 scripts/deploy.sh --restart   # live install into the RAM overlay, gone at reboot
SMOKE_SSH=panda@192.168.0.55 scripts/smoke.sh 192.168.0.55
```

`Makefile` chains the build scripts for one board (`make ota BOARD=rpi1`, `make help`) with stamp
files in `build/.stamps/<board>/`. Keep it in sync: when a build script, its inputs, the build
order, or the list of packages changes, update the Makefile in the same commit.

Ruff only enforces real bugs (`F`, `E9`), not style. Python targets 3.11 and the appliance code is
stdlib only (except `panda-bt.py`, which uses dbus-next). Shell scripts on the device are busybox ash
(`#!/bin/sh`); build scripts are bash.

`tests/unit/test_consistency.py` checks for drift between files: web API paths vs `panda-web.py`,
element ids in `app.js` vs `index.html`, status keys vs `docs/demo/mock.js`, the restartable services
and logs in `panda-web.py` vs `web-admin.sh`, OpenRC dependency references and cycles, and whether
`docs/panel-demo.html` is up to date. If you change one side, update the other (and rerun
`build-demo.sh` for web page changes).

## Architecture

**Generic vs per-board split.** `boards/<board>/` holds everything that depends on the hardware:
`board.conf` (shell variables: arch, kernel, DTB, audio card/rate/quirks, BT controller, button,
LEDs, watchdog, `SPOTIFY=no` etc.), a mkimage profile `mkimg.<board>.sh`, `write-sdcard.sh`, and an
optional kernel aport and U-Boot patches. The appliance code in `appliance/` reads every hardware
detail from `/etc/panda/board.conf` at runtime; it never hard-codes a board. `boards/_template/`
documents every variable. Board names must be shell identifiers (mkimage builds function names
from them).

**Packaging and image.** `appliance/rootfs/` (services, daemons, web page) becomes the noarch
`panda-appliance` apk (`aports/panda-appliance/APKBUILD`, built from the tree mounted at `/work`);
its `depends` comes from `appliance/packages.list`, and it has one `panda-board-<board>` subpackage
per board that carries board.conf. `aports/` also holds a fork of shairport-sync (native PipeWire
backend; `pkgrel=100` so it wins over Alpine's build). PipeWire and mpv are stock Alpine packages. Packages are built in a target-arch Alpine container under qemu-user and land in
`packages/main/<arch>/`. Kernels are cross-compiled on the host by `scripts/cross-build-kernel.sh`,
which also builds the out-of-tree `drivers/r92su` (a git submodule). U-Boot v2024.07 is built with
Fedora's cross gcc.

**Runtime model.** Each card has A/B OTA slots (`slots/<x>/` holds the kernel, initramfs, modules
and `rootfs.squashfs`). A patched initramfs `/init` (`appliance/initramfs/`) mounts the slot's
squashfs read-only with a tmpfs overlay, then unpacks the lbu apkovl (settings only, see
`etc/panda/lbu-paths.list`) on top. Files in the overlay win over package files, so **never copy
appliance code into the overlay/`/etc` on the device**; use `deploy.sh`. At every boot,
`panda-setup` runs `setup.sh`, which merges users, runs `gen-configs.sh` (renders the
PipeWire/WirePlumber/shairport/bluez/watchdog configs from board.conf + appliance.conf + audio.conf)
and enables services. OTA bundles are verified and installed into the standby slot by
`panda-ota.py`, booted on trial, confirmed by `panda-ota-confirm`, and rolled back if unhealthy.
Breaking changes to the card format mean rewriting the card; there is no migration code.

**Services** (`appliance/rootfs/etc/init.d/`, OpenRC + supervise-daemon; Python and shell in
`etc/panda/`):
- `speaker-*` run as user `speaker`: pipewire, wireplumber, shairport, soloist (Spotify), radio (mpv).
- `panda-arbiter.py` (speaker-arbiter): watches `pw-dump --monitor`. The last source to start wins,
  and the older one is paused (Soloist WS / AVRCP) or muted. It also triggers `audio-route.py` for
  output selection and volume caps, and writes `/run/speaker/nowplaying.json`.
- `panda-web.py`: the Win98-style control panel (stdlib HTTP + SSE on :8080, port 80 redirected by
  the firewall), running as user `panda-web`. Its only privileged path is
  `doas /etc/panda/web-admin.sh <verb>`, which validates every argument itself.
- `panda-bt.py` (BlueZ over D-Bus, pairing, AVRCP) and `panda-wifi.py` expose line-JSON Unix
  sockets in `/run/panda/`. `panda_ipc.py` is the shared client; it never raises, and a peer that is
  down is reported as an ordinary error reply.
- `panda-lcd.py` (front panel) is on hold.

**Test seams.** The graph rig and unit tests redirect paths and commands with `PANDA_*`
environment variables (`PANDA_RUN_DIR`, `PANDA_CONF_DIR`, `PANDA_ADMIN_CMD`, `PANDA_ROUTE_CMD`,
`PANDA_SOLOIST_CTL`, `PANDA_SPEAKER_RUN_DIR`, `PANDA_STATE_DIR`, `PANDA_WEB_DIR`,
`PANDA_RADIO_BROWSER_BASE`, `PANDA_PERIOD_SCALE`, `PANDA_BOARD_CONF`, `PANDA_PAUSE_GRACE_S`,
`PANDA_ROUTE_SETTLE_S`). Nothing sets them on the appliance.
`PANDA_DEBUG_EVENTS=1` makes the arbiter log its decisions as JSON. The fake peers (BlueZ, Soloist,
radio-browser, stream server) are in `tests/fakes/`. When a graph test fails, its artifacts are in
`build/graph-artifacts/<test>/`.

## Project rules

- No board-specific patches in the generic code (`appliance/`, `aports/`, `scripts/`): no
  `if BOARD = ...` branches and no hard-coded board hardware. Add a `board.conf` variable with a
  generic default (documented in `boards/_template/board.conf`), or put the change under
  `boards/<board>/`.
- Hardware quirks go in `board.conf` only, and only for onboard hardware. USB peripherals get
  generic handling.
- Never list process command lines as root on a board (`ps`, `top`, `pgrep -a`, `/proc/*/cmdline`):
  Soloist's API key is in its argv. `/proc` is mounted with `hidepid`. Never read
  `/etc/panda/soloist.key`.
- Don't load modules or install dkms/kernel drivers on the Fedora host. Test on the board or in
  containers.
- Commit directly to `main`.
- Keep README.md up to date: when a change affects what it documents (build steps, commands, boards,
  features, behaviour, configuration), update it in the same commit.
- Never commit markdown plans or notes (yours or the user's). Only documentation goes into git:
  README.md files and this CLAUDE.md. `.gitignore` ignores every other `*.md`, so write new plans
  and notes as local files and never force-add them. Don't link to them from README.md or from code.
