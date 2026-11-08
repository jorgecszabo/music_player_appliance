#!/usr/bin/env python3
"""Bring the saved users and groups in line with the root image (runs as root, stdlib only).

  merge-users.py <image etc> <live etc>     e.g. /media/root-ro/etc /etc (setup.sh --boot)

The root image (SQUASHFS-ROOT.md) has the system and service users the packages created
at build time, with the uids its files are owned by. The settings overlay unpacked over
it carries saved copies of passwd, group and shadow, which would replace the image's. So:
  passwd  the image's entries, then the saved ones the image doesn't have (the admin user)
  group   the image's entries with the saved extra members added, then saved-only groups
  shadow  the saved entry for every user that has one (passwords are settings), else the
          image's; then saved-only entries
A file is rewritten only when it changes (no copy-up, nothing new for lbu to save).
"""
import os
import sys


def read(path):
    try:
        return [line for line in open(path).read().splitlines() if line.strip()]
    except OSError:
        return []


def name(line):
    return line.split(":", 1)[0]


def merge_passwd(image, live):
    have = {name(l) for l in image}
    return image + [l for l in live if name(l) not in have]


def merge_group(image, live):
    saved = {name(l): l for l in live}
    out = []
    for line in image:
        f = line.split(":")
        if len(f) == 4 and f[0] in saved:
            members = [m for m in f[3].split(",") if m]
            sf = saved[f[0]].split(":")
            extra = [m for m in (sf[3].split(",") if len(sf) == 4 else []) if m and m not in members]
            f[3] = ",".join(members + extra)
            line = ":".join(f)
        out.append(line)
    have = {name(l) for l in image}
    return out + [l for l in live if name(l) not in have]


def merge_shadow(image, live):
    saved = {name(l): l for l in live}
    have = {name(l) for l in image}
    return [saved.get(name(l), l) for l in image] + [l for l in live if name(l) not in have]


def clashes(passwd):
    """Users sharing a uid (a saved user against an image user): logged, not fixed."""
    seen, out = {}, []
    for line in passwd:
        f = line.split(":")
        if len(f) > 2:
            if f[2] in seen:
                out.append(f"uid {f[2]}: {seen[f[2]]} and {f[0]}")
            seen.setdefault(f[2], f[0])
    return out


def write_if_changed(path, lines, like):
    """Write `lines` to `path` (mode and owner of `path`, else of `like`)."""
    text = "\n".join(lines) + "\n"
    try:
        if open(path).read() == text:
            return False
    except OSError:
        pass
    st = None
    for p in (path, like):
        try:
            st = os.stat(p)
            break
        except OSError:
            pass
    tmp = path + ".new"
    # 0600 until the final mode is set: shadow is never readable on the way.
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(text)
    if st:
        os.chmod(tmp, st.st_mode & 0o7777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass
    os.replace(tmp, path)
    return True


def main(image_etc, live_etc):
    changed = []
    for fname, merge in (("passwd", merge_passwd), ("group", merge_group), ("shadow", merge_shadow)):
        image = read(os.path.join(image_etc, fname))
        if not image:
            continue
        lines = merge(image, read(os.path.join(live_etc, fname)))
        if fname == "passwd":
            for c in clashes(lines):
                print(f"merge-users: WARNING: {c}", file=sys.stderr)
        if write_if_changed(os.path.join(live_etc, fname), lines, os.path.join(image_etc, fname)):
            changed.append(fname)
    if changed:
        print("merge-users: updated " + " ".join(changed))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
