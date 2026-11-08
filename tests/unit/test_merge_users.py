"""merge-users.py: the root image's users and groups win; saved passwords and the admin user stay."""
import os
import stat

import pytest

from tests.conftest import load_module

mu = load_module("merge-users.py")

IMAGE = {
    "passwd": ["root:x:0:0:root:/root:/bin/sh", "chrony:x:101:102:chrony:/dev/null:/sbin/nologin",
               "speaker:x:102:104:Audio services:/var/lib/speaker:/sbin/nologin"],
    "group": ["root:x:0:root", "audio:x:18:speaker", "chrony:x:102:chrony", "speaker:x:104:panda-web"],
    "shadow": ["root:*::0:::::", "chrony:!::0:::::", "speaker:!::0:::::"],
}


def write(d, files, mode=0o644):
    d.mkdir(exist_ok=True)
    for name, lines in files.items():
        p = d / name
        p.write_text("\n".join(lines) + "\n")
        p.chmod(0o640 if name == "shadow" else mode)


def lines(d, name):
    return (d / name).read_text().splitlines()


@pytest.fixture
def image(tmp_path):
    write(tmp_path / "image", IMAGE)
    return tmp_path / "image"


def test_a_fresh_system_is_left_alone(tmp_path, image):
    live = tmp_path / "live"
    write(live, IMAGE)
    before = {n: os.stat(live / n).st_mtime_ns for n in IMAGE}
    assert mu.main(str(image), str(live)) == 0
    assert {n: os.stat(live / n).st_mtime_ns for n in IMAGE} == before          # not rewritten


def test_saved_files_get_the_image_users_and_keep_the_admin(tmp_path, image):
    live = tmp_path / "live"
    # Saved by an older version: chrony had another uid, speaker didn't exist, admin did.
    write(live, {
        "passwd": ["root:x:0:0:root:/root:/bin/sh", "chrony:x:100:101:chrony:/dev/null:/sbin/nologin",
                   "admin:x:1000:1000:Appliance admin:/home/admin:/bin/ash"],
        "group": ["root:x:0:root", "audio:x:18:", "chrony:x:101:chrony", "admin:x:1000:", "wheel:x:10:admin"],
        "shadow": ["root:$6$saved$hash:20000:0:::::", "chrony:!::0:::::", "admin:*:20000:0:99999:7:::"],
    })
    mu.main(str(image), str(live))
    assert lines(live, "passwd") == IMAGE["passwd"] + ["admin:x:1000:1000:Appliance admin:/home/admin:/bin/ash"]
    assert lines(live, "group") == IMAGE["group"] + ["admin:x:1000:", "wheel:x:10:admin"]
    assert lines(live, "shadow") == ["root:$6$saved$hash:20000:0:::::", "chrony:!::0:::::", "speaker:!::0:::::",
                                     "admin:*:20000:0:99999:7:::"]                  # root's password kept


def test_saved_group_members_are_added_to_image_groups(tmp_path, image):
    live = tmp_path / "live"
    write(live, dict(IMAGE, group=["root:x:0:root", "audio:x:18:admin,speaker", "chrony:x:102:chrony",
                                   "speaker:x:104:panda-web"]))
    mu.main(str(image), str(live))
    assert "audio:x:18:speaker,admin" in lines(live, "group")


def test_modes_are_kept_and_shadow_never_widens(tmp_path, image):
    live = tmp_path / "live"
    write(live, {"passwd": ["admin:x:1000:1000::/home/admin:/bin/ash"], "group": ["admin:x:1000:"],
                 "shadow": ["admin:*:20000:0:::::"]})
    mu.main(str(image), str(live))
    assert stat.S_IMODE(os.stat(live / "shadow").st_mode) == 0o640
    assert stat.S_IMODE(os.stat(live / "passwd").st_mode) == 0o644


def test_a_missing_live_file_takes_the_image_mode(tmp_path, image):
    live = tmp_path / "live"
    live.mkdir()
    mu.main(str(image), str(live))
    assert lines(live, "shadow") == IMAGE["shadow"]
    assert stat.S_IMODE(os.stat(live / "shadow").st_mode) == 0o640


def test_uid_clashes_are_reported(tmp_path, image, capsys):
    live = tmp_path / "live"
    write(live, dict(IMAGE, passwd=IMAGE["passwd"] + ["olduser:x:102:102::/:/sbin/nologin"]))
    mu.main(str(image), str(live))
    assert "uid 102: speaker and olduser" in capsys.readouterr().err


def test_no_image_files_changes_nothing(tmp_path):
    live = tmp_path / "live"
    write(live, IMAGE)
    (tmp_path / "empty").mkdir()
    mu.main(str(tmp_path / "empty"), str(live))
    assert lines(live, "passwd") == IMAGE["passwd"]
