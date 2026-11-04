#!/usr/bin/env python3
"""Print slot.json for a built slot directory (build/sdimage/<board>/slots/a).

Usage: scripts/slot-json.py <slot-dir>     (BOARD from the environment, as env.sh)
Fields: version + git (build/panda-appliance.version), built (UTC), board, arch
(boards/<board>/board.conf), kernel (the kernel package in the slot's repository).
The device writes the same fields from an OTA bundle's manifest.
"""
import glob
import json
import os
import re
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
project = os.path.dirname(here)
slot = sys.argv[1]
board = os.environ.get("BOARD", "pandaboard")
conf = {}
for line in open(os.path.join(project, "boards", board, "board.conf")):
    m = re.match(r"^([A-Z_]+)=\"?([^\"#]*)\"?", line.strip())
    if m:
        conf[m.group(1)] = m.group(2).strip()
try:
    version, git = open(os.path.join(project, "build", "panda-appliance.version")).read().split()[:2]
except (OSError, ValueError):
    version, git = "unknown", "unknown"
# The kernel package mkimage used: the newest one in our repository (the slot's
# repository doesn't carry it; its files are in boot/).
kernel = None
pkg = conf.get("KERNEL_PACKAGE", "linux-")
cands = glob.glob(os.path.join(project, "packages", "main", conf.get("ALPINE_ARCH", ""), pkg + "-[0-9]*.apk"))
if cands:
    kernel = os.path.basename(max(cands, key=os.path.getmtime))[:-4]
print(json.dumps({
    "version": version, "git": git,
    "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "board": board, "arch": conf.get("ALPINE_ARCH"), "kernel": kernel,
}, indent=1))
