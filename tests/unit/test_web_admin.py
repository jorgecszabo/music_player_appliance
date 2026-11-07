"""web-admin.sh: the script panda-web runs as root. It must validate for itself.

The script is run for real (with `sh`) on a copy whose absolute paths point into a temp
root and whose PATH starts with recording stubs for rc-service, lbu, reboot and friends.
"""
import os
import stat
import subprocess

import pytest

from tests.conftest import PANDA

STUBS = ["rc-service", "lbu", "logger", "reboot", "poweroff", "flock", "setsid", "pw-cli", "amixer", "chown",
         "doas", "wpctl", "sync"]


@pytest.fixture
def admin(tmp_path):
    root = tmp_path / "root"
    stubs = tmp_path / "stubs"
    for d in ("etc/panda", "etc/pipewire/pipewire.conf.d", "var/log/speaker", "run/panda", "run/speaker"):
        (root / d).mkdir(parents=True)
    stubs.mkdir()
    calls = tmp_path / "calls"
    calls.write_text("")
    for name in STUBS:
        p = stubs / name
        if name == "flock":                     # flock <lock> <cmd...>: run the command
            p.write_text('#!/bin/sh\necho "flock $*" >> "%s"\nshift\nexec "$@"\n' % calls)
        else:
            p.write_text('#!/bin/sh\necho "%s $*" >> "%s"\n' % (name, calls))
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    (root / "etc/panda/board.conf").write_text('SPOTIFY=yes\nAUDIO_HW_VOLUME_MAX=15\nAUDIO_CARD=""\n')
    (root / "etc/panda/appliance.conf").write_text('SPEAKER_NAME="Panda"\n')
    (root / "etc/panda/audio.conf").write_text("BASS_DB=0\nTREBLE_DB=0\nMAX_VOLUME=10\nOUTPUT=onboard\nUSB_MAX_DB=0\n"
                                               "USB_PROFILE=auto\n")
    (root / "etc/panda/tone-conf.sh").write_text("#!/bin/sh\n: tone\n")
    text = open(os.path.join(PANDA, "web-admin.sh")).read()
    text = text.replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={stubs}:/usr/bin:/bin")
    for old in ("/etc/panda", "/etc/pipewire", "/var/log", "/run/panda", "/run/speaker"):
        text = text.replace(old, str(root) + old)
    script = tmp_path / "web-admin.sh"
    script.write_text(text)

    class A:
        pass
    a = A()
    a.root, a.calls_file = root, calls

    def run(*args, stdin=""):
        return subprocess.run(["sh", str(script), *args], input=stdin, capture_output=True, text=True, timeout=20)
    a.run = run
    a.calls = lambda: calls.read_text().splitlines()

    def wait_call(substr, timeout=3):
        """bg() starts things in the background: wait for the stub to record the call."""
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if any(substr in c for c in a.calls()):
                return True
            time.sleep(0.02)
        return False
    a.wait_call = wait_call
    a.read = lambda rel: (root / rel).read_text()
    return a


def test_unknown_action_is_refused(admin):
    r = admin.run("frobnicate")
    assert r.returncode == 2 and "unknown action" in r.stderr
    assert admin.run().returncode == 2


@pytest.mark.parametrize("svc", ["speaker-shairport", "speaker-radio", "panda-bt", "panda-wifi", "avahi-daemon", "chronyd"])
def test_restart_known_services(admin, svc):
    assert admin.run("restart", svc).returncode == 0
    assert admin.wait_call(f"rc-service {svc} restart"), admin.calls()


@pytest.mark.parametrize("svc", ["sshd", "panda-web", "speaker-arbiter", "", "speaker-radio;reboot", "../x",
                                 "speaker-radio\nreboot", "-h"])
def test_restart_other_services_refused(admin, svc):
    r = admin.run("restart", svc)
    assert r.returncode == 2, (svc, r.stderr)
    assert not any("rc-service" in c for c in admin.calls())


def test_restart_soloist_refused_without_spotify(admin):
    (admin.root / "etc/panda/board.conf").write_text('SPOTIFY=no\n')
    assert admin.run("restart", "speaker-soloist").returncode == 2


@pytest.mark.parametrize("name", ["Kitchen", "Living Room", "a", "a.b_c-d'e", "A" * 32])
def test_set_name_valid(admin, name):
    r = admin.run("set-name", name)
    assert r.returncode == 0, r.stderr
    assert f'SPEAKER_NAME="{name}"' in admin.read("etc/panda/appliance.conf")


BAD_NAMES = ["", " lead", "A" * 33, "bad;name", "$(reboot)", "`reboot`", "a\"b", "a\\b", "-x", "a&b", "a|b",
             "a>b", "naïve", "a\tb",
             "Kitchen\nreboot", "Kitchen\n$(reboot)", "Kitchen\n", "\nKitchen", "Kitchen\r", "ok\nSPEAKER_NAME=x"]


@pytest.mark.parametrize("name", BAD_NAMES, ids=repr)
def test_set_name_rejects_everything_else_including_multiline_values(admin, name):
    """The name is written into a file that root sources: a second line is a command."""
    before = admin.read("etc/panda/appliance.conf")
    r = admin.run("set-name", name)
    assert r.returncode == 2, (name, r.stdout, r.stderr)
    assert admin.read("etc/panda/appliance.conf") == before


@pytest.mark.parametrize("args", [
    ("0", "0", "5", "onboard", "0", "auto"), ("-12", "12", "15", "usb", "-30", "output:analog-stereo"),
    ("1", "-1", "0", "onboard", "-5", "pro-audio")])
def test_set_audio_valid(admin, args):
    r = admin.run("set-audio", *args)
    assert r.returncode == 0, r.stderr
    conf = admin.read("etc/panda/audio.conf")
    assert f"BASS_DB={args[0]}" in conf and f"OUTPUT={args[3]}" in conf and f"USB_PROFILE={args[5]}" in conf


@pytest.mark.parametrize("args", [
    ("13", "0", "5", "onboard", "0", "auto"), ("0", "-13", "5", "onboard", "0", "auto"),
    ("0", "0", "16", "onboard", "0", "auto"), ("0", "0", "5", "speaker", "0", "auto"),
    ("0", "0", "5", "onboard", "1", "auto"), ("0", "0", "5", "onboard", "-31", "auto"),
    ("a", "0", "5", "onboard", "0", "auto"), ("0", "0", "5", "onboard", "0", "a b"),
    ("0", "0", "5", "onboard", "0", "auto\nreboot"), ("0\n1", "0", "5", "onboard", "0", "auto"),
    ("0", "0", "5\n6", "onboard", "0", "auto"), ("0", "0", "5", "onboard", "0\n0", "auto"),
    ("0", "0", "5", "onboard", "0", "x;rm"), ("0", "0", "5", "onboard", "0", "$(reboot)"),
    ("", "", "", "", "", ""), ("0", "0", "5", "onboard", "0", "../x"), ("1e1", "0", "5", "onboard", "0", "auto"),
    ("0", "0", "5", "onboard", "0", "x/y"), ("0", "0", "5", "onboard", "0", "a&b")])
def test_set_audio_rejects(admin, args):
    before = admin.read("etc/panda/audio.conf")
    r = admin.run("set-audio", *args)
    assert r.returncode == 2, (args, r.stdout, r.stderr)
    assert admin.read("etc/panda/audio.conf") == before


@pytest.mark.parametrize("v", ["0", "1", "0.5", "0.123", "1.0", "1.000"])
def test_set_master_valid(admin, v):
    assert admin.run("set-master", v).returncode == 0
    assert f"MASTER_VOLUME={v}" in admin.read("etc/panda/audio.conf")


@pytest.mark.parametrize("v", ["", "2", "-1", "1.1", "0.1234", "abc", "0.5\n1", "0.5\nreboot", "1e0", ".5", "0,5",
                               "0.5;x"])
def test_set_master_rejects(admin, v):
    assert admin.run("set-master", v).returncode == 2
    assert "MASTER_VOLUME" not in admin.read("etc/panda/audio.conf")


KEY = "abcdefgh12345678.-_"


def test_set_soloist_key_stores_it_without_echoing(admin):
    r = admin.run("set-soloist-key", stdin=KEY + "\n")
    assert KEY not in r.stdout and KEY not in r.stderr
    assert admin.read("etc/panda/soloist.key").strip() == KEY
    mode = stat.S_IMODE(os.stat(admin.root / "etc/panda/soloist.key").st_mode)
    assert mode & 0o077 == 0, oct(mode)
    assert KEY not in "\n".join(admin.calls())         # never on a command line


@pytest.mark.parametrize("key", ["", "short", "a" * 201, "has space 123456789012", "semi;colon12345678901",
                                 "new\nline1234567890123", "quote'quote1234567890", "$(reboot)1234567890"])
def test_set_soloist_key_rejects(admin, key):
    r = admin.run("set-soloist-key", stdin=key + "\n")
    assert r.returncode == 2
    assert not (admin.root / "etc/panda/soloist.key").exists()


def test_logs_known_names_only(admin):
    (admin.root / "var/log/speaker/radio.log").write_text("hello\n")
    assert admin.run("logs", "radio").stdout.strip() == "hello"
    for bad in ("", "../../etc/shadow", "radio;id", "radio\nmessages", "nope", "/etc/passwd"):
        assert admin.run("logs", bad).returncode == 2, bad


def test_logs_redact_the_spotify_key(admin):
    (admin.root / "etc/panda/soloist.key").write_text(KEY + "\n")
    (admin.root / "var/log/speaker/soloist.log").write_text(f"login with {KEY} ok\nsecond {KEY} {KEY}\n")
    out = admin.run("logs", "soloist").stdout
    assert KEY not in out and "<api-key>" in out


@pytest.mark.parametrize("slot", ["", "c", "ab", "a;b", "A", "a\nb"])
def test_ota_activate_refuses_other_slots(admin, slot):
    assert admin.run("ota-activate", slot).returncode == 2


def test_restart_audio_orders_dependencies_first(admin):
    admin.run("restart-audio-now")
    order = [c.split()[1] for c in admin.calls() if c.startswith("rc-service")]
    starts = [c for c in admin.calls() if c.startswith("rc-service") and c.endswith(("start", "restart"))]
    assert order.index("speaker-pipewire") < len(order)
    names = [c.split()[1] for c in starts]
    assert names.index("speaker-pipewire") < names.index("speaker-wireplumber") < names.index("speaker-radio")
    assert names.index("speaker-wireplumber") < names.index("speaker-arbiter")
