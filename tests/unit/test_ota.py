"""panda-ota.py: bundle verification and the slot state machine, on a temp boot medium.

The script runs in its own test mode (PANDA_OTA_TEST=1: no remounts, reboots or lbu), as
scripts/test-ota.sh does against a built image; these tests need no image.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile

import pytest

from tests.conftest import PANDA

OTA = os.path.join(PANDA, "panda-ota.py")
LABEL = "LABEL x\n  MENU LABEL Panda\n  KERNEL /boot/vmlinuz\n  INITRD /boot/initramfs\n  APPEND root=/dev/ram quiet\n"


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("keys")
    for name in ("good", "other"):
        subprocess.run(["openssl", "genrsa", "-out", str(d / f"{name}.pem"), "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", str(d / f"{name}.pem"), "-pubout", "-out", str(d / f"{name}.rsa.pub")],
                       check=True, capture_output=True)
    return d


class Env:
    def __init__(self, tmp_path, keys):
        self.keys, self.media = keys, tmp_path / "media"
        self.trusted = tmp_path / "trusted"
        self.trusted.mkdir()
        shutil.copy(keys / "good.rsa.pub", self.trusted / "good.rsa.pub")
        (tmp_path / "board.conf").write_text('BOARD=testboard\nALPINE_ARCH=armv7\nBOOT_MEDIA=mmcblk0p1\n')
        self.cmdline = tmp_path / "cmdline"
        self.cmdline.write_text("console=ttyS0 panda_slot=a\n")
        self.media.mkdir()
        self.board_conf = tmp_path / "board.conf"
        # slot a: installed and booted
        a = self.media / "slots" / "a"
        (a / "boot").mkdir(parents=True)
        (a / "extlinux.label").write_text(LABEL)
        (a / "slot.json").write_text(json.dumps({"version": "0.9"}))
        (self.media / "slots" / "state").write_text("confirmed=a\n")

    def run(self, *args, stdin=b""):
        env = dict(os.environ, PANDA_OTA_TEST="1", PANDA_OTA_MEDIA=str(self.media), PANDA_OTA_BOARD_CONF=str(self.board_conf),
                   PANDA_OTA_KEYS=str(self.trusted), PANDA_OTA_CMDLINE_FILE=str(self.cmdline))
        return subprocess.run([sys.executable, "-I", OTA, *args], input=stdin, capture_output=True, env=env, timeout=60)

    def booted(self, slot):
        self.cmdline.write_text(f"console=ttyS0 panda_slot={slot}\n")

    def state(self):
        p = self.media / "slots" / "state"
        return p.read_text() if p.exists() else ""

    def written(self):
        """Every file under the medium except slot a and the state."""
        out = []
        for root, _, names in os.walk(self.media):
            for n in names:
                p = os.path.relpath(os.path.join(root, n), self.media)
                if not p.startswith("slots/a/") and p not in ("slots/state", ".media.lock"):
                    out.append(p)
        return sorted(out)


@pytest.fixture
def env(tmp_path, keys):
    return Env(tmp_path, keys)


def make_bundle(keys, files=None, manifest=None, signer="good", extra_members=(), skip=(), links=()):
    files = files if files is not None else {
        "extlinux.label": LABEL.encode(), "boot/vmlinuz": b"kernel" * 100, "boot/initramfs": b"initrd" * 50,
        "apks/pkg.apk": b"apk" * 200}
    man = {"format": 1, "board": "testboard", "arch": "armv7", "version": "1.0", "git": "abc", "built": "now",
           "kernel": "6.x",
           "files": [{"path": p, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()} for p, d in files.items()]}
    man.update(manifest or {})
    raw = json.dumps(man).encode()
    sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(keys / f"{signer}.pem")], input=raw,
                         capture_output=True, check=True).stdout
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        def add(name, data):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        add("manifest.json", raw)
        add("manifest.sig", sig)
        for p, d in files.items():
            if p not in skip:
                add(p, d)
        for name, data in extra_members:
            add(name, data)
        for name, target in links:
            ti = tarfile.TarInfo(name)
            ti.type = tarfile.SYMTYPE
            ti.linkname = target
            tf.addfile(ti)
    return buf.getvalue()


def result(proc):
    lines = [json.loads(l) for l in proc.stdout.decode().splitlines() if l.strip().startswith("{")]
    return lines[-1] if lines else {}


# --- install ---------------------------------------------------------------------------------------------

def test_good_bundle_installs_into_the_other_slot(env, keys):
    r = env.run("install", stdin=make_bundle(keys))
    assert r.returncode == 0, r.stderr
    assert result(r)["ok"] is True and result(r)["slot"] == "b"
    assert (env.media / "slots/b/slot.json").exists() and (env.media / "slots/b/boot/vmlinuz").read_bytes() == b"kernel" * 100
    conf = (env.media / "extlinux/extlinux.conf").read_text()
    assert "DEFAULT a" in conf and "LABEL b" in conf and "panda_slot=b" in conf       # the running slot stays the default


def test_bad_signature_writes_nothing(env, keys):
    r = env.run("install", stdin=make_bundle(keys, signer="other"))
    assert r.returncode != 0 and result(r)["ok"] is False and "signature" in result(r)["error"]
    assert env.written() == []


def test_no_trusted_keys_is_refused(env, keys):
    for f in env.trusted.iterdir():
        f.unlink()
    r = env.run("install", stdin=make_bundle(keys))
    assert r.returncode != 0 and env.written() == []


@pytest.mark.parametrize("cut", [0, 1, 100, 511, 512, 700, 1024, 2048, 4096, 6000])
def test_truncated_bundles_never_leave_an_installed_slot(env, keys, cut):
    bundle = make_bundle(keys)
    r = env.run("install", stdin=bundle[:min(cut, len(bundle) - 1)])
    assert r.returncode != 0, r.stdout
    assert not (env.media / "slots/b/slot.json").exists()
    assert env.state() == "confirmed=a\n"


def test_truncated_at_every_block_boundary(env, keys):
    bundle = make_bundle(keys)
    with tarfile.open(fileobj=io.BytesIO(bundle)) as tf:
        last = tf.getmembers()[-1]
        content_end = last.offset_data + last.size            # after this only padding and end blocks follow
    for cut in sorted(set(list(range(0, content_end, 512)) + [content_end - 1])):
        r = env.run("install", stdin=bundle[:cut])
        assert not (env.media / "slots/b/slot.json").exists(), cut
        assert r.returncode != 0, cut


@pytest.mark.parametrize("bad", [
    {"manifest": {"board": "otherboard"}}, {"manifest": {"arch": "x86_64"}}, {"manifest": {"format": 2}},
    {"manifest": {"format": None}},
])
def test_wrong_board_arch_or_format(env, keys, bad):
    r = env.run("install", stdin=make_bundle(keys, **bad))
    assert r.returncode != 0 and not (env.media / "slots/b/slot.json").exists()


@pytest.mark.parametrize("path", ["../escape", "boot/../../escape", "/etc/passwd", "boot/../x", "evil", "boot/a b",
                                  "boot/x;rm", "apks/\x00x", "etc/shadow", "boot/\n"])
def test_paths_outside_the_allowed_set_are_refused(env, keys, path):
    files = {"extlinux.label": LABEL.encode(), "boot/vmlinuz": b"k", "apks/p.apk": b"a", path: b"x"}
    r = env.run("install", stdin=make_bundle(keys, files=files))
    assert r.returncode != 0
    assert env.written() == [] and not (env.media.parent / "escape").exists()


def test_checksum_mismatch_cleans_up(env, keys):
    files = {"extlinux.label": LABEL.encode(), "boot/vmlinuz": b"KERNELDATA", "apks/p.apk": b"apk"}
    bundle = make_bundle(keys, files=files)
    bundle = bundle.replace(b"KERNELDATA", b"KERNELDATB", 1)            # payload no longer matches its sha256
    r = env.run("install", stdin=bundle)
    assert r.returncode != 0 and "checksum" in result(r).get("error", "")
    assert not (env.media / "slots/b/slot.json").exists() and not (env.media / "slots/b/boot").exists()


def test_extra_member_not_in_the_manifest_is_refused(env, keys):
    r = env.run("install", stdin=make_bundle(keys, extra_members=[("boot/extra", b"surprise")]))
    assert r.returncode != 0 and not (env.media / "slots/b/slot.json").exists()


def test_duplicate_member_is_refused(env, keys):
    r = env.run("install", stdin=make_bundle(keys, extra_members=[("boot/vmlinuz", b"kernel" * 100)]))
    assert r.returncode != 0 and not (env.media / "slots/b/slot.json").exists()


def test_symlink_member_is_refused(env, keys):
    files = {"extlinux.label": LABEL.encode(), "boot/vmlinuz": b"k", "apks/p.apk": b"a"}
    r = env.run("install", stdin=make_bundle(keys, files=files, skip={"boot/vmlinuz"},
                                             links=[("boot/vmlinuz", "/etc/passwd")]))
    assert r.returncode != 0 and not (env.media / "slots/b/slot.json").exists()


@pytest.mark.parametrize("missing", ["extlinux.label", "boot/vmlinuz", "apks/pkg.apk"])
def test_incomplete_manifests_are_refused(env, keys, missing):
    files = {"extlinux.label": LABEL.encode(), "boot/vmlinuz": b"kernel", "apks/pkg.apk": b"apk"}
    del files[missing]
    r = env.run("install", stdin=make_bundle(keys, files=files))
    assert r.returncode != 0 and not (env.media / "slots/b/slot.json").exists()


@pytest.mark.parametrize("junk", [b"", b"not a tar", b"\x00" * 2048, os.urandom(3000)])
def test_garbage_input(env, keys, junk):
    r = env.run("install", stdin=junk)
    assert r.returncode != 0 and env.written() == []


def test_install_is_refused_while_a_trial_is_pending(env, keys):
    (env.media / "slots/state").write_text("confirmed=a\ntrial=b\n")
    r = env.run("install", stdin=make_bundle(keys))
    assert r.returncode != 0 and "trial" in result(r)["error"] and not (env.media / "slots/b").exists()


def test_install_is_refused_when_not_booted_from_a_slot(env, keys):
    env.cmdline.write_text("console=ttyS0\n")
    r = env.run("install", stdin=make_bundle(keys))
    assert r.returncode != 0 and env.written() == []


# --- activate / state machine -----------------------------------------------------------------------------

def installed(env, keys):
    assert env.run("install", stdin=make_bundle(keys)).returncode == 0


def test_activate_puts_the_new_slot_on_trial(env, keys):
    installed(env, keys)
    r = env.run("activate")
    assert r.returncode == 0 and result(r)["slot"] == "b"
    assert "trial=b" in env.state() and "confirmed=a" in env.state()
    assert "DEFAULT b" in (env.media / "extlinux/extlinux.conf").read_text()


def test_activate_without_an_installed_other_slot_is_refused(env):
    r = env.run("activate")
    assert r.returncode != 0 and "trial" not in env.state()


@pytest.mark.parametrize("slot", ["c", "", "ab", "a;b", "../a", "A"])
def test_activate_bad_slot(env, slot):
    r = env.run("activate", slot)
    assert r.returncode != 0 and "trial" not in env.state()


def test_activating_the_running_slot_clears_a_trial(env, keys):
    installed(env, keys)
    env.run("activate")
    env.booted("b")                                                     # the trial boots...
    r = env.run("activate", "b")                                        # ...and the user keeps it
    assert r.returncode == 0 and "trial" not in env.state()


def test_revert_goes_back_to_the_confirmed_slot(env, keys):
    installed(env, keys)
    env.run("activate")
    env.booted("b")
    r = env.run("activate", "a")
    assert r.returncode == 0 and "trial=a" in env.state()


def test_status_is_valid_json_in_every_state(env, keys):
    for step in (None, "install", "activate"):
        if step == "install":
            installed(env, keys)
        elif step == "activate":
            env.run("activate")
        r = env.run("status")
        assert r.returncode == 0 and json.loads(r.stdout.decode().splitlines()[-1])
    env.state()


def test_unknown_command(env):
    assert env.run("frobnicate").returncode != 0
