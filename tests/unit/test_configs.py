"""gen-configs.sh and tone-conf.sh: every board's config generates, and the output is well-formed."""
import glob
import os
import re
import subprocess

import pytest

from tests.conftest import PANDA, ROOT

BOARDS = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(ROOT, "boards", "*", "board.conf"))
                if "_template" not in p)


def conf_dir(tmp_path, board, name="Panda", audio=""):
    d = tmp_path / "conf"
    d.mkdir(exist_ok=True)
    (d / "board.conf").write_text(open(os.path.join(ROOT, "boards", board, "board.conf")).read())
    (d / "appliance.conf").write_text(f'SPEAKER_NAME="{name}"\n')
    if audio:
        (d / "audio.conf").write_text(audio)
    return d


def generate(tmp_path, board, **kw):
    d = conf_dir(tmp_path, board, **kw)
    out = tmp_path / "out"
    r = subprocess.run(["sh", os.path.join(PANDA, "gen-configs.sh")], env=dict(os.environ, CONF_DIR=str(d), ROOT=str(out)),
                       capture_output=True, text=True, timeout=60)
    return r, out


def files(out):
    return {os.path.relpath(os.path.join(r, n), out): open(os.path.join(r, n)).read()
            for r, _, names in os.walk(out) for n in names}


def balanced(text):
    # Strip strings and comments, then check braces/brackets pair up.
    text = re.sub(r'"[^"\n]*"', '""', text)
    text = re.sub(r"(#|//).*", "", text)
    stack = []
    for ch in text:
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack or {"}": "{", "]": "["}[ch] != stack.pop():
                return False
    return not stack


@pytest.mark.parametrize("board", BOARDS)
def test_every_board_generates_its_configs(tmp_path, board):
    r, out = generate(tmp_path, board)
    assert r.returncode == 0, r.stderr
    got = files(out)
    for expected in ("etc/pipewire/pipewire.conf.d/10-panda.conf", "etc/pipewire/pipewire.conf.d/20-panda-tone.conf",
                     "etc/wireplumber/wireplumber.conf.d/10-panda.conf", "etc/shairport-sync.conf",
                     "etc/bluetooth/main.conf", "etc/conf.d/watchdog"):
        assert expected in got, f"{board}: {expected} not generated"
    for name, text in got.items():
        assert text.strip(), f"{board}: {name} is empty"
        assert not re.search(r"\$\{|\$\(|\$[A-Z_]{3,}", text), f"{board}: {name} has an unexpanded variable"
        if name.endswith(".conf") and "pipewire" in name or "wireplumber" in name:
            assert balanced(text), f"{board}: {name} has unbalanced braces"


@pytest.mark.parametrize("board", BOARDS)
def test_generation_is_idempotent(tmp_path, board):
    r, out = generate(tmp_path, board)
    first = files(out)
    r2 = subprocess.run(["sh", os.path.join(PANDA, "gen-configs.sh")],
                        env=dict(os.environ, CONF_DIR=str(tmp_path / "conf"), ROOT=str(out)), capture_output=True, text=True)
    assert r2.returncode == 0 and files(out) == first


@pytest.mark.parametrize("name", ["Panda", "Living Room", "Kitchen's", "a.b_c-d", "A" * 32])
def test_speaker_name_reaches_every_service_config_unchanged(tmp_path, name):
    r, out = generate(tmp_path, BOARDS[0], name=name)
    assert r.returncode == 0, r.stderr
    got = files(out)
    assert f'name = "{name}";' in got["etc/shairport-sync.conf"]
    assert f"Name = {name}\n" in got["etc/bluetooth/main.conf"]


def test_audio_settings_change_only_the_tone_filter(tmp_path):
    r, out = generate(tmp_path, BOARDS[0], audio="BASS_DB=6\nTREBLE_DB=-3\n")
    tone = files(out)["etc/pipewire/pipewire.conf.d/20-panda-tone.conf"]
    assert '"Gain" = 6.0' in tone and '"Gain" = -3.0' in tone


@pytest.mark.parametrize("bass,treble,mult", [(0, 0, 0.707946), (6, 0, 0.354813), (0, 12, 0.177828), (-6, -6, 0.707946),
                                              (12, 12, 0.177828)])
def test_headroom_follows_the_largest_boost(tmp_path, bass, treble, mult):
    d = conf_dir(tmp_path, BOARDS[0], audio=f"BASS_DB={bass}\nTREBLE_DB={treble}\n")
    out = subprocess.run(["sh", os.path.join(PANDA, "tone-conf.sh")], env=dict(os.environ, CONF_DIR=str(d)),
                         capture_output=True, text=True, check=True).stdout
    got = float(re.search(r'"Mult" = ([0-9.]+)', out).group(1))
    assert got == pytest.approx(mult, abs=1e-5)
    assert balanced(out)


def test_watchdog_interval_follows_the_timeout(tmp_path):
    d = conf_dir(tmp_path, BOARDS[0])
    with open(d / "board.conf", "a") as f:
        f.write("\nWATCHDOG_TIMEOUT=30\n")
    out = tmp_path / "out"
    subprocess.run(["sh", os.path.join(PANDA, "gen-configs.sh")], env=dict(os.environ, CONF_DIR=str(d), ROOT=str(out)),
                   check=True, capture_output=True)
    assert "-T 30 -t 5" in files(out)["etc/conf.d/watchdog"]


def test_factory_reset_dry_run_never_touches_anything(tmp_path):
    media = tmp_path / "media" / "mmcblk0p1"
    (media / "factory").mkdir(parents=True)
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo("etc/hostname")
        ti.size = 6
        tf.addfile(ti, io.BytesIO(b"panda\n"))
    (media / "factory" / "panda.apkovl.tar.gz").write_bytes(buf.getvalue())
    (media / "panda.apkovl.tar.gz").write_bytes(b"current settings")
    text = open(os.path.join(PANDA, "factory-reset.sh")).read()
    text = text.replace(". /etc/panda/board.conf", 'BOOT_MEDIA=mmcblk0p1').replace(". /etc/panda/appliance.conf", "HOSTNAME=panda")
    text = text.replace("media=/media/", f"media={tmp_path}/media/").replace("/run/panda/slot", str(tmp_path / "noslot"))
    script = tmp_path / "factory-reset.sh"
    script.write_text(text)
    before = {p: p.read_bytes() for p in media.rglob("*") if p.is_file()}
    r = subprocess.run(["sh", str(script), "--dry-run"], capture_output=True, text=True, env=dict(os.environ, PATH="/usr/bin:/bin"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "dry run" in r.stdout
    assert {p: p.read_bytes() for p in media.rglob("*") if p.is_file()} == before
    # no factory overlay: refuses
    (media / "factory" / "panda.apkovl.tar.gz").unlink()
    r = subprocess.run(["sh", str(script), "--dry-run"], capture_output=True, text=True, env=dict(os.environ, PATH="/usr/bin:/bin"))
    assert r.returncode == 1
