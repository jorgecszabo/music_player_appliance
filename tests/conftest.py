import importlib.util
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ETC = os.path.join(ROOT, "appliance", "rootfs", "etc")
PANDA = os.path.join(ETC, "panda")
sys.path.insert(0, PANDA)
sys.path.insert(0, ROOT)

_loaded = {}


def load_module(filename, name=None):
    """Import a script from appliance/rootfs/etc/panda by file name (they have dashes)."""
    if filename not in _loaded:
        spec = importlib.util.spec_from_file_location(name or filename.replace("-", "_").removesuffix(".py"),
                                                      os.path.join(PANDA, filename))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _loaded[filename] = mod
    return _loaded[filename]


def wait_until(pred, timeout=5.0, what="condition", interval=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


@pytest.fixture
def ipc(tmp_path, monkeypatch):
    """panda_ipc pointed at sockets in a temp dir."""
    import panda_ipc
    monkeypatch.setattr(panda_ipc, "MPV_SOCK", str(tmp_path / "radio.sock"))
    monkeypatch.setattr(panda_ipc, "BT_SOCK", str(tmp_path / "bt.sock"))
    monkeypatch.setattr(panda_ipc, "WIFI_SOCK", str(tmp_path / "wifi.sock"))
    return panda_ipc


@pytest.fixture
def peers():
    """Collects FakePeers so they are closed at the end of the test."""
    from tests.fakes.peer import FakePeer
    made = []

    def make(path, behaviour):
        p = FakePeer(path, behaviour)
        made.append(p)
        return p
    yield make
    for p in made:
        p.close()
