"""panda-bt: the control socket, the pairing policy and the pairing window (BlueZ is faked)."""
import asyncio
import json

import pytest

from tests.conftest import load_module


@pytest.fixture(scope="module")
def bt():
    return load_module("panda-bt.py")


@pytest.fixture
def app(bt, monkeypatch):
    cfg = {"BT_CONTROLLER": "hci0", "LED_BT": "", "SPEAKER_NAME": "Test", "BUTTON_INPUT_NAME": "", "BUTTON_KEY": ""}
    monkeypatch.setattr(bt, "CHIMES", set(), raising=False)
    a = bt.App(cfg)
    a.led = type("L", (), {"set": lambda s, on: None, "blink": lambda s, a, b: None})()

    class FakeAdapter:
        def __init__(self):
            self.calls = []

        async def set_pairable(self, v):
            self.calls.append(("pairable", v))

        async def set_discoverable(self, v):
            self.calls.append(("discoverable", v))

        async def call_remove_device(self, path):
            self.calls.append(("remove", path))
    a.adapter = FakeAdapter()
    a.saved = []

    async def save(why):
        a.saved.append(why)
    a.save = save
    return a


def run(coro):
    return asyncio.run(coro)


# --- control socket ---------------------------------------------------------------------------------

async def talk(bt, app, tmp_path, monkeypatch, lines):
    sock = str(tmp_path / "bt.sock")
    monkeypatch.setattr(bt, "CONTROL_SOCK", sock)
    monkeypatch.setattr(bt, "CONTROL_GROUP", "no-such-group-xyz")

    async def status():
        return {"status": "fake"}
    app.status = status
    server = await app.control_server()
    replies = []
    try:
        for raw in lines:
            r, w = await asyncio.open_unix_connection(sock)
            w.write(raw)
            await w.drain()
            try:
                replies.append(await asyncio.wait_for(r.readline(), 3))
            finally:
                w.close()
        r, w = await asyncio.open_unix_connection(sock)          # still serving afterwards?
        w.write(b'{"cmd": "status"}\n')
        await w.drain()
        replies.append(await asyncio.wait_for(r.readline(), 3))
        w.close()
    finally:
        server.close()
    return replies


def test_control_replies_to_garbage_and_keeps_serving(bt, app, tmp_path, monkeypatch):
    lines = [b"[1]\n", b"5\n", b"null\n", b'"x"\n', b"{\n", b"\xff\xfe\n", b"\n", b'{"cmd": ["x"]}\n',
             b'{"cmd": "nope"}\n', b'{"cmd": "media", "action": "rm -rf"}\n', b'{"cmd": "forget", "path": "../x"}\n',
             b'{"cmd": "forget", "path": ["a"]}\n', b'{"cmd": "media", "action": null}\n']
    replies = run(talk(bt, app, tmp_path, monkeypatch, lines))
    for line, reply in zip(lines, replies):
        assert reply.endswith(b"\n") and "error" in json.loads(reply), (line, reply)
    assert json.loads(replies[-1]) == {"status": "fake"}


def test_forget_unknown_device_is_an_error_not_a_crash(bt, app):
    with pytest.raises(ValueError):
        run(app.forget("/org/bluez/hci0/dev_00"))


def test_forget_known_device_removes_and_saves(bt, app):
    app.devices["/org/bluez/hci0/dev_AA"] = {"Alias": "Phone", "Paired": True}
    run(app.forget("/org/bluez/hci0/dev_AA"))
    assert ("remove", "/org/bluez/hci0/dev_AA") in app.adapter.calls and app.saved == ["forgot Phone"]


# --- pairing policy -------------------------------------------------------------------------------------

def test_pairing_is_rejected_outside_the_window(bt, app):
    with pytest.raises(bt.DBusError):
        app.check_pairing("/dev")


def test_services_of_unknown_devices_are_rejected(bt, app):
    with pytest.raises(bt.DBusError):
        app.check_service("/dev_unknown", "0000110b")
    app.devices["/dev_paired"] = {"Paired": True}
    app.check_service("/dev_paired", "0000110b")                    # a paired device may use services
    app.devices["/dev_trusted"] = {"Trusted": True}
    app.check_service("/dev_trusted", "0000110b")


def test_pairing_accepted_only_while_the_window_is_open(bt, app):
    async def scenario():
        app.set_window_state(True)
        await asyncio.sleep(0.05)
        assert app.window_open
        app.check_pairing("/dev")                                   # accepted
        assert ("pairable", True) in app.adapter.calls and ("discoverable", True) in app.adapter.calls
        app.set_window_state(False)
        await asyncio.sleep(0.05)
        assert not app.window_open
        with pytest.raises(bt.DBusError):
            app.check_pairing("/dev")
        assert app.adapter.calls[-2:] == [("pairable", False), ("discoverable", False)]
    run(scenario())


def test_the_window_closes_by_itself(bt, app, monkeypatch):
    monkeypatch.setattr(bt, "WINDOW_S", 0.1)

    async def scenario():
        app.set_window_state(True)
        await asyncio.sleep(0.05)
        assert app.window_open
        await asyncio.sleep(0.3)
        assert not app.window_open and app.adapter.calls[-1] == ("discoverable", False)
    run(scenario())


def test_opening_twice_and_closing_twice_are_harmless(bt, app):
    async def scenario():
        app.set_window_state(True)
        app.set_window_state(True)
        await asyncio.sleep(0.05)
        app.toggle_window()
        await asyncio.sleep(0.05)
        app.set_window_state(False)
        assert not app.window_open
    run(scenario())


def test_paired_devices_are_marked_trusted(bt, app):
    trusted = []

    class Dev:
        async def set_trusted(self, v):
            trusted.append(v)

    async def iface(path, name):
        return Dev()
    app.iface = iface
    app.devices["/dev_A"] = {"Alias": "Phone"}
    run(app.paired("/dev_A"))
    assert trusted == [True]


def test_connected_chime_is_suppressed_right_after_pairing(bt, app, monkeypatch):
    played = []
    monkeypatch.setattr(bt, "CHIMES", {"paired", "connected", "pairing-off"})
    monkeypatch.setattr(bt.subprocess, "Popen", lambda cmd, **k: played.append(cmd[-1]))
    app.event("paired")
    app.event("connected")
    app.event("pairing-off")
    assert played == ["paired"]


# --- the controller watchdog ------------------------------------------------------------------------

def watch(bt, app, answers, ticks, monkeypatch, driver="hci_uart"):
    """Run app.health() for `ticks` probes whose results are `answers`; return the bt-reset calls."""
    app.cfg["BT_DRIVER_AFTER_WIFI"] = driver
    resets = []
    monkeypatch.setattr(bt.subprocess, "run", lambda cmd, **kw: resets.append(cmd))
    it = iter(answers)
    app.probe = lambda index: next(it)

    class Done(Exception):
        pass
    n = {"sleeps": 0}

    async def sleep(_):
        n["sleeps"] += 1
        if n["sleeps"] > ticks:
            raise Done()
    with pytest.raises(Done):
        run(app.health(sleep=sleep))
    return resets


def test_a_controller_that_answers_is_left_alone(bt, app, monkeypatch):
    assert watch(bt, app, [True] * 5, 5, monkeypatch) == []
    assert app.alive is True


def test_one_missed_probe_is_forgiven(bt, app, monkeypatch):
    assert watch(bt, app, [True, False, True, True], 4, monkeypatch) == []
    assert app.alive is True


def test_a_silent_controller_is_power_cycled_after_two_probes(bt, app, monkeypatch):
    assert watch(bt, app, [True, False, False], 3, monkeypatch) == [["/etc/panda/web-admin.sh", "bt-reset"]]
    assert app.alive is False


def test_probes_that_cannot_tell_are_ignored(bt, app, monkeypatch):
    assert watch(bt, app, [None, None, None, None], 4, monkeypatch) == []
    assert app.alive is None


def test_without_a_driver_to_reload_it_only_reports(bt, app, monkeypatch):
    assert watch(bt, app, [False, False, False, False], 4, monkeypatch, driver="") == []
    assert app.alive is False


def test_status_reports_whether_the_controller_answers(bt, app):
    app.alive = False
    app.devices = {}

    async def fake_iface(*a):
        raise bt.DBusError("org.bluez.Error.Failed", "no bus")
    app.iface = fake_iface
    assert run(app.status())["alive"] is False
