#!/bin/bash
# Cross-compile this kernel on the host; scripts/build-package.sh runs this
# before abuild. The kernel is described in the APKBUILD (_defconfig,
# _fragment, _dtbs); see scripts/cross-build-kernel.sh.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$here/../../../../scripts/cross-build-kernel.sh" "$here"
