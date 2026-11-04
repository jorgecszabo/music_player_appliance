#!/usr/bin/env python3
"""OTA updates: A/B slots on the boot medium (stdlib only; runs as root).

  panda-ota.py status            JSON: slots, booted/confirmed/trial slot, free space
  panda-ota.py install           read a bundle (.pota) on stdin into the standby slot
  panda-ota.py activate [slot]   boot <slot> (default: the standby one) on trial at the
                                 next boot; the caller reboots
  panda-ota.py boot              early boot (panda-ota-boot): arm the rollback on a trial
                                 boot; after a failed trial, restore the settings
  panda-ota.py confirm           health check of a trial boot (panda-ota-confirm):
                                 healthy -> keep the new slot, else reboot (rollback)
  panda-ota.py write-boot        rewrite the bootloader config from the slots and state

Layout on the boot medium (/media/<BOOT_MEDIA>, board.conf):
  slots/<x>/boot/ apks/ extlinux.label chimes/ soloist-runtime.squashfs
            factory.apkovl.tar.gz      what the bundle carried
  slots/<x>/slot.json                  written last: a slot without it is incomplete
  slots/<x>/settings.apkovl.tar.gz     the settings as they were when <x> was left
  slots/state                          "confirmed=<x>" and "trial=<y>" lines

Bundle: an uncompressed tar. manifest.json first, manifest.sig (RSA-SHA256 over the
manifest, by a key in /etc/panda/ota-keys/) second, then the files the manifest lists
(path, size, sha256). Nothing is written before the signature checks out; every file
is hashed while it streams into the standby slot, which nothing boots from.

The bootloader side is the only board-specific part: board.conf OTA_BOOT_SCHEME
(default "extlinux": U-Boot distro boot / syslinux-style menus).
"""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import fcntl
import urllib.request

SLOTS = ("a", "b")
KEYS_DIR = "/etc/panda/ota-keys"
LOCK = "/run/panda/media.lock"
MAX_MANIFEST = 1 << 20
FORMAT = 1
# What a bundle may carry (paths relative to the slot).
ALLOWED = re.compile(r"^(boot/[A-Za-z0-9._+/-]+|apks/[A-Za-z0-9._+/-]+|chimes/[A-Za-z0-9._-]+"
                     r"|extlinux\.label|soloist-runtime\.squashfs|factory\.apkovl\.tar\.gz"
                     r"|uboot/[A-Za-z0-9._-]+)$")
CONFIRM_TIMEOUT_S = 300
CONFIRM_MIN_UPTIME_S = 60
REQUIRED_SERVICES = ("sshd", "panda-web", "speaker-pipewire", "speaker-wireplumber")


def log(msg):
    print(f"panda-ota: {msg}", file=sys.stderr, flush=True)
    if os.environ.get("PANDA_OTA_TEST") != "1":
        subprocess.run(["logger", "-t", "panda-ota", msg], check=False)


def run_root(argv):
    """Commands with side effects outside the boot medium (reboot, lbu)."""
    if os.environ.get("PANDA_OTA_TEST") == "1":
        log("test: would run " + " ".join(argv))
        return
    subprocess.run(argv, check=False)


def die(msg, code=1):
    log(f"ERROR: {msg}")
    print(json.dumps({"ok": False, "error": msg}), flush=True)
    sys.exit(code)


def shell_conf(path):
    """KEY=value lines of a shell config (values unquoted; comments dropped)."""
    out = {}
    try:
        text = open(path).read()
    except OSError:
        return out
    for line in text.splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
        if not m:
            continue
        v = m.group(2)
        if v[:1] in "\"'":
            q = v[0]
            v = v[1:v.find(q, 1)] if v.find(q, 1) > 0 else v[1:]
        else:
            v = v.split("#", 1)[0].strip()
        out[m.group(1)] = v
    return out


# Test hooks (scripts/test-ota.sh runs this on the build host): another
# board.conf, boot medium, kernel command line; no remounts, no reboots.
TEST = os.environ.get("PANDA_OTA_TEST") == "1"
BOARD = shell_conf(os.environ.get("PANDA_OTA_BOARD_CONF", "/etc/panda/board.conf"))
MEDIA = os.environ.get("PANDA_OTA_MEDIA") or "/media/" + BOARD.get("BOOT_MEDIA", "mmcblk0p1")
CMDLINE = os.environ.get("PANDA_OTA_CMDLINE_FILE", "/proc/cmdline")
# The boot medium as the booted system sees it (kernel command line paths).
DEVICE_MEDIA = "/media/" + BOARD.get("BOOT_MEDIA", "mmcblk0p1")
if TEST:
    LOCK = MEDIA + "/.media.lock"
    KEYS_DIR = os.environ.get("PANDA_OTA_KEYS", KEYS_DIR)
SLOTS_DIR = MEDIA + "/slots"
STATE = SLOTS_DIR + "/state"


# --- Boot medium access -------------------------------------------------------------

class Media:
    """Exclusive, read-write access to the boot medium (flock + remount)."""

    def __init__(self, wait=True):
        self.wait = wait

    def __enter__(self):
        os.makedirs(os.path.dirname(LOCK), exist_ok=True)
        self.fd = open(LOCK, "a")
        fcntl.flock(self.fd, fcntl.LOCK_EX | (0 if self.wait else fcntl.LOCK_NB))
        if TEST:
            return self
        if subprocess.run(["mount", "-o", "remount,rw", MEDIA]).returncode != 0:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            raise OSError(f"can't remount {MEDIA} read-write")
        return self

    def __exit__(self, *exc):
        os.sync()
        if TEST:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            self.fd.close()
            return
        for _ in range(5):
            if subprocess.run(["mount", "-o", "remount,ro", MEDIA]).returncode == 0:
                break
            time.sleep(1)
        else:
            log(f"WARNING: {MEDIA} stays read-write (busy)")
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()


def write_atomic(path, data):
    """Write a file on FAT: temp file, fsync, rename over the old one."""
    tmp = path + ".new"
    with open(tmp, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def cmdline_opt(name):
    try:
        for opt in open(CMDLINE).read().split():
            if opt.startswith(name + "="):
                return opt.split("=", 1)[1]
    except OSError:
        pass
    return None


def booted_slot():
    s = cmdline_opt("panda_slot")
    return s if s in SLOTS else None


def read_state():
    st = {"confirmed": None, "trial": None, "failed": None}
    try:
        for line in open(STATE):
            k, _, v = line.strip().partition("=")
            if k in st and v in SLOTS:
                st[k] = v
    except OSError:
        pass
    if st["confirmed"] is None:
        st["confirmed"] = booted_slot()
    return st


def write_state(st):
    write_atomic(STATE, "".join(f"{k}={st[k]}\n" for k in ("confirmed", "trial", "failed")
                                if st.get(k)))


def slot_info(slot):
    try:
        return json.load(open(f"{SLOTS_DIR}/{slot}/slot.json"))
    except (OSError, ValueError):
        return None


def other(slot):
    return "b" if slot == "a" else "a"


def overlay_path():
    host = "panda" if TEST or not os.path.exists("/etc/hostname") else open("/etc/hostname").read().strip()
    p = f"{MEDIA}/{host}.apkovl.tar.gz"
    if os.path.exists(p):
        return p
    found = sorted(glob.glob(MEDIA + "/*.apkovl.tar.gz"))
    return found[0] if found else p


# --- Bootloader schemes ---------------------------------------------------------------

def extlinux_entry(slot):
    """The slot's boot entry: its extlinux.label (the image's extlinux.conf) with the
    paths moved into the slot and the slot's modloop/repository on the command line."""
    text = open(f"{SLOTS_DIR}/{slot}/extlinux.label").read()
    info = slot_info(slot) or {}
    keep = []
    in_label = False
    for line in text.splitlines():
        s = line.strip()
        word = s.split(None, 1)[0].upper() if s else ""
        if word == "LABEL":
            if in_label:
                break                      # the first entry only
            in_label = True
            continue
        if not in_label or word in ("", "MENU", "#") or s.startswith("#"):
            continue
        keep.append(s)
    if not keep:
        raise ValueError(f"slot {slot}: no LABEL entry in extlinux.label")
    modloop = sorted(glob.glob(f"{SLOTS_DIR}/{slot}/boot/modloop-*"))
    extra = []
    if modloop:
        extra.append(f"modloop=/slots/{slot}/boot/{os.path.basename(modloop[0])}")
    extra += [f"alpine_repo={DEVICE_MEDIA}/slots/{slot}/apks", f"panda_slot={slot}", "panic=10"]
    out = [f"LABEL {slot}",
           f"  MENU LABEL Panda {info.get('version', '?')} (slot {slot})"]
    seen_append = False
    for s in keep:
        key, _, val = s.partition(" ")
        k = key.upper()
        if k in ("KERNEL", "LINUX", "INITRD", "FDT", "FDTDIR", "DEVICETREE", "FDTOVERLAYS"):
            val = ",".join(f"/slots/{slot}/{p.strip().lstrip('/')}" for p in val.split(","))
        elif k == "APPEND":
            val = " ".join(o for o in val.split()
                           if not o.startswith(("modloop=", "alpine_repo=", "panda_slot=", "panic=")))
            val = (val + " " + " ".join(extra)).strip()
            seen_append = True
        out.append(f"  {key} {val}")
    if not seen_append:
        out.append("  APPEND " + " ".join(extra))
    return "\n".join(out) + "\n"


def write_extlinux(default):
    slots = [s for s in SLOTS if slot_info(s) and os.path.exists(f"{SLOTS_DIR}/{s}/extlinux.label")]
    if default not in slots:
        raise ValueError(f"slot {default} isn't installed")
    entries = "".join(extlinux_entry(s) for s in slots)

    def conf(dflt):
        return ("# Generated by /etc/panda/panda-ota.py. Don't edit: rewritten on updates.\n"
                f"DEFAULT {dflt}\nTIMEOUT 20\nMENU TITLE Panda boot slots\n" + entries)

    # A copy in /boot/extlinux first: U-Boot looks there when /extlinux/extlinux.conf is
    # missing, so a power cut during the rename below still leaves a bootable card.
    for d in (MEDIA + "/boot/extlinux", MEDIA + "/extlinux"):
        os.makedirs(d, exist_ok=True)
        write_atomic(d + "/extlinux.conf", conf(default))
    # For a bootloader with boot counting (altbootcmd): the other slot, if any.
    fb = other(default) if other(default) in slots else default
    write_atomic(MEDIA + "/extlinux/fallback.conf", conf(fb))


SCHEMES = {"extlinux": write_extlinux}


def write_boot(default):
    scheme = BOARD.get("OTA_BOOT_SCHEME", "extlinux") or "extlinux"
    if scheme not in SCHEMES:
        raise ValueError(f"OTA_BOOT_SCHEME={scheme} is not supported")
    SCHEMES[scheme](default)


# --- Commands ---------------------------------------------------------------------

def cmd_status():
    st = read_state()
    booted = booted_slot()
    try:
        vfs = os.statvfs(MEDIA)
        free = vfs.f_bavail * vfs.f_frsize
    except OSError:
        free = None
    slots = {}
    for s in SLOTS:
        info = slot_info(s)
        if info:
            info = {k: info.get(k) for k in ("version", "git", "built", "installed", "kernel",
                                              "board", "size")}
        slots[s] = info
    cur = {}
    try:
        cur = {"version": open("/etc/panda/version").read().split()[0]}
    except (OSError, IndexError):
        pass
    print(json.dumps({"ok": True, "booted": booted, "confirmed": st["confirmed"],
                      "trial": st["trial"], "standby": other(booted) if booted else None,
                      "slots": slots, "free": free, "running": cur,
                      "board": BOARD.get("BOARD"), "arch": BOARD.get("ALPINE_ARCH"),
                      "rolled_back": st["failed"]}))


def verify_signature(manifest, sig):
    keys = sorted(glob.glob(KEYS_DIR + "/*.pub"))
    if not keys:
        die(f"no trusted keys in {KEYS_DIR}")
    with tempfile.TemporaryDirectory() as d:
        open(d + "/m", "wb").write(manifest)
        open(d + "/s", "wb").write(sig)
        for k in keys:
            r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", k, "-signature",
                                d + "/s", d + "/m"], capture_output=True)
            if r.returncode == 0:
                return os.path.basename(k)
    die("bad signature: the bundle isn't signed by a trusted key")


def read_member(tf, member, limit):
    if not member.isfile() or member.size > limit:
        die(f"bundle: {member.name} is not a small regular file")
    return tf.extractfile(member).read()


def cmd_install(stream):
    booted = booted_slot()
    if not booted:
        die("not booted from a slot (old card layout): flash a current image once")
    st = read_state()
    if st["trial"]:
        die(f"slot {st['trial']} is on trial: finish that first (reboot)")
    target = other(booted)
    try:
        tf = tarfile.open(fileobj=stream, mode="r|")
        m = tf.next()
        if m is None or m.name != "manifest.json":
            die("not an update bundle (no manifest.json first)")
        manifest_raw = read_member(tf, m, MAX_MANIFEST)
        s = tf.next()
        if s is None or s.name != "manifest.sig":
            die("not an update bundle (no manifest.sig)")
        sig = read_member(tf, s, 4096)
    except (tarfile.TarError, EOFError, OSError):
        die("not an update bundle (not a tar file)")
    key = verify_signature(manifest_raw, sig)
    try:
        man = json.loads(manifest_raw)
    except ValueError:
        die("manifest is not JSON")
    if man.get("format") != FORMAT:
        die(f"unsupported bundle format {man.get('format')}")
    if man.get("board") != BOARD.get("BOARD") or man.get("arch") != BOARD.get("ALPINE_ARCH"):
        die(f"bundle is for {man.get('board')}/{man.get('arch')}, "
            f"this is {BOARD.get('BOARD')}/{BOARD.get('ALPINE_ARCH')}")
    files = {}
    for f in man.get("files", []):
        p = f.get("path", "")
        if not ALLOWED.match(p) or ".." in p.split("/"):
            die(f"manifest: path not allowed: {p!r}")
        if not isinstance(f.get("size"), int) or not re.fullmatch(r"[0-9a-f]{64}", f.get("sha256", "")):
            die(f"manifest: bad entry for {p}")
        files[p] = f
    for need in ("extlinux.label",):
        if need not in files:
            die(f"manifest: no {need}")
    if not any(p.startswith("apks/") for p in files) or not any(p.startswith("boot/") for p in files):
        die("manifest: no boot/ or apks/")
    total = sum(f["size"] for f in files.values())
    log(f"bundle {man.get('version')} ({total} bytes, {len(files)} files, key {key}) -> slot {target}")
    print(json.dumps({"stage": "verified", "version": man.get("version"), "total": total,
                      "slot": target}), flush=True)

    dest = f"{SLOTS_DIR}/{target}"
    with Media():
        old = 0
        for root, _, names in os.walk(dest):
            old += sum(os.path.getsize(os.path.join(root, n)) for n in names)
        vfs = os.statvfs(MEDIA)
        if vfs.f_bavail * vfs.f_frsize + old < total + (8 << 20):
            die(f"not enough space on the card: need {total >> 20} MiB")
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest)
        got = set()
        done = 0
        try:
            for member in tf:
                p = member.name
                # (Iterating a stream replays the members read already.)
                if member.isdir() or member is m or member is s:
                    continue
                if p not in files or p in got or not member.isfile():
                    raise ValueError(f"unexpected entry {p!r}")
                f = files[p]
                if member.size != f["size"]:
                    raise ValueError(f"{p}: size differs from the manifest")
                out = os.path.join(dest, p)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                h = hashlib.sha256()
                src = tf.extractfile(member)
                with open(out, "wb") as o:
                    while True:
                        chunk = src.read(1 << 16)
                        if not chunk:
                            break
                        h.update(chunk)
                        o.write(chunk)
                    o.flush()
                    os.fsync(o.fileno())
                if h.hexdigest() != f["sha256"]:
                    raise ValueError(f"{p}: checksum mismatch")
                got.add(p)
                done += f["size"]
                print(json.dumps({"stage": "copy", "done": done, "total": total}), flush=True)
            missing = set(files) - got
            if missing:
                raise ValueError(f"bundle ends early: {len(missing)} files missing")
        except (ValueError, OSError, tarfile.TarError, EOFError) as e:
            shutil.rmtree(dest, ignore_errors=True)
            die(f"install failed: {e}")
        info = {k: man.get(k) for k in ("version", "git", "built", "kernel", "board", "arch")}
        info.update(size=total, installed=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), key=key)
        write_atomic(dest + "/slot.json", json.dumps(info, indent=1) + "\n")
        # The boot menu lists the new slot (the default stays the running one).
        write_boot(booted)
    log(f"installed {man.get('version')} in slot {target}")
    print(json.dumps({"ok": True, "stage": "installed", "slot": target,
                      "version": man.get("version")}), flush=True)


def cmd_activate(slot=None):
    booted = booted_slot()
    if not booted:
        die("not booted from a slot")
    slot = slot or other(booted)
    if slot not in SLOTS or not slot_info(slot):
        die(f"slot {slot} has no complete installation")
    st = read_state()
    if slot == booted:
        if st["trial"]:
            with Media():
                st["trial"] = None
                write_state(st)
                write_boot(booted)
        print(json.dumps({"ok": True, "slot": slot, "note": "already running"}))
        return
    # Save the settings, then keep a copy in this slot: restored if the trial fails
    # and we come back here.
    run_root(["flock", LOCK, "lbu", "commit"])
    with Media():
        ovl = overlay_path()
        if os.path.exists(ovl):
            snap = f"{SLOTS_DIR}/{booted}/settings.apkovl.tar.gz"
            shutil.copyfile(ovl, snap + ".new")
            os.replace(snap + ".new", snap)
        st["confirmed"] = booted
        st["trial"] = slot
        st["failed"] = None
        write_state(st)
        write_boot(slot)
    log(f"slot {slot} ({slot_info(slot).get('version')}) boots next, on trial")
    print(json.dumps({"ok": True, "slot": slot, "version": slot_info(slot).get("version")}))


def cmd_boot():
    """Early in boot (before the appliance services)."""
    booted = booted_slot()
    run_dir = MEDIA + "/.run" if TEST else "/run/panda"
    os.makedirs(run_dir, exist_ok=True)
    open(run_dir + "/slot", "w").write((booted or "") + "\n")
    if not booted:
        return
    # apk's repository for this boot: the slot's (the overlay may name another).
    repo = f"{DEVICE_MEDIA}/slots/{booted}/apks"
    repofile = MEDIA + "/.run/repositories" if TEST else "/etc/apk/repositories"
    try:
        lines = open(repofile).read().splitlines()
    except OSError:
        lines = []
    new = [repo] + [l for l in lines if l.strip() and not l.startswith(DEVICE_MEDIA + "/")]
    if new != lines:
        open(repofile, "w").write("\n".join(new) + "\n")
    st = read_state()
    if not st["trial"]:
        return
    if st["trial"] == booted:
        # Trial boot: from now on, any reboot goes back to the confirmed slot.
        with Media():
            write_boot(st["confirmed"] if slot_info(st["confirmed"] or "") else booted)
        log(f"trial boot of slot {booted}: rollback armed (default: {st['confirmed']})")
        return
    # Back on the old slot with a trial pending: the trial failed.
    log(f"the trial of slot {st['trial']} failed; staying on slot {booted}")
    snap = f"{SLOTS_DIR}/{booted}/settings.apkovl.tar.gz"
    ovl = overlay_path()
    restore = os.path.exists(snap) and (not os.path.exists(ovl) or
                                        open(snap, "rb").read() != open(ovl, "rb").read())
    with Media():
        st["failed"] = st["trial"]
        st["confirmed"] = booted
        st["trial"] = None
        write_state(st)
        write_boot(booted)
        if restore:
            shutil.copyfile(snap, ovl + ".new")
            os.replace(ovl + ".new", ovl)
    open(run_dir + "/ota-rollback", "w").write("1\n")
    if restore:
        log("settings restored from before the update; rebooting once")
        run_root(["reboot"])


def healthy():
    booted = booted_slot()
    if booted and os.path.exists(f"{SLOTS_DIR}/{booted}/FAIL_HEALTH"):
        return "FAIL_HEALTH test flag in the slot"
    if TEST:
        return None
    # A network speaker that isn't on the network is broken, even if it runs.
    addrs = subprocess.run(["ip", "-o", "addr", "show", "scope", "global"],
                           capture_output=True, text=True).stdout
    if not addrs.strip():
        return "no network address"
    for svc in REQUIRED_SERVICES:
        if subprocess.run(["rc-service", svc, "status"], capture_output=True).returncode != 0:
            return f"{svc} is not running"
    try:
        port = os.environ.get("PANDA_WEB_PORT", "8080")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status",
                                     headers={"Host": "localhost"})
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status != 200:
                return f"web page answers {r.status}"
    except Exception as e:  # noqa: BLE001 - any failure is unhealthy
        return f"web page: {e}"
    return None


def cmd_confirm():
    booted = booted_slot()
    st = read_state()
    if not booted or st["trial"] != booted:
        return
    timeout = int(os.environ.get("PANDA_OTA_CONFIRM_TIMEOUT", CONFIRM_TIMEOUT_S)) if TEST else CONFIRM_TIMEOUT_S
    min_up = 0 if TEST else CONFIRM_MIN_UPTIME_S
    step = 1 if TEST else 10
    deadline = time.monotonic() + timeout
    why = "not checked yet"
    while time.monotonic() < deadline:
        up = float(open("/proc/uptime").read().split()[0])
        if up >= min_up:
            why = healthy()
            if why is None:
                break
        time.sleep(step)
    else:
        log(f"slot {booted} is unhealthy ({why}): rebooting into slot {st['confirmed']}")
        run_root(["reboot"])
        return
    with Media():
        st["confirmed"] = booted
        st["trial"] = None
        write_state(st)
        write_boot(booted)
    log(f"slot {booted} ({(slot_info(booted) or {}).get('version')}) confirmed")
    # Settings the new version created at boot (users, defaults) are saved now.
    run_root(["flock", LOCK, "lbu", "commit"])


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        cmd_status()
    elif cmd == "install":
        cmd_install(sys.stdin.buffer)
    elif cmd == "activate":
        cmd_activate(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "boot":
        cmd_boot()
    elif cmd == "confirm":
        cmd_confirm()
    elif cmd == "write-boot":
        booted = booted_slot()
        st = read_state()
        with Media():
            write_boot(st["trial"] or st["confirmed"] or booted)
    else:
        die(f"unknown command {cmd}")


if __name__ == "__main__":
    main()
