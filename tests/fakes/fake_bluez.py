#!/usr/bin/env python3
"""A fake org.bluez on a private bus, for the arbiter's AVRCP Pause (panda-arbiter bt_pause).

Exposes ObjectManager at "/" with one device's MediaPlayer1. A Pause call is recorded in
$FAKE_BLUEZ_LOG and, depending on $FAKE_BLUEZ_DIR/mode, answered ("ok": also pauses the
fake Bluetooth source; "fail": D-Bus error; "hang": never answers; "noplayer": no player).
"""
import asyncio
import os
import socket

from dbus_next import BusType
from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError
from dbus_next.service import ServiceInterface, method

DIR = os.environ["FAKE_BLUEZ_DIR"]
PLAYER = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF/player0"


def mode():
    try:
        return open(os.path.join(DIR, "mode")).read().strip()
    except OSError:
        return "ok"


def record(what):
    with open(os.path.join(DIR, "calls"), "a") as f:
        f.write(what + "\n")


def pause_bluetooth_source():
    """The phone really pauses: tell the fake Bluetooth source (an mpv) to pause."""
    sock = os.path.join(DIR, "bluetooth.sock")
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(2)
            s.connect(sock)
            s.sendall(b'{"command": ["set_property", "pause", true]}\n')
            s.recv(4096)
    except OSError:
        pass


class ObjectManager(ServiceInterface):
    def __init__(self):
        super().__init__("org.freedesktop.DBus.ObjectManager")

    @method()
    def GetManagedObjects(self) -> "a{oa{sa{sv}}}":
        if mode() == "noplayer":
            return {}
        return {PLAYER: {"org.bluez.MediaPlayer1": {}}}


class MediaPlayer(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.MediaPlayer1")

    @method()
    async def Pause(self):
        record("Pause")
        m = mode()
        if m == "hang":
            await asyncio.sleep(3600)
        if m == "fail":
            raise DBusError("org.bluez.Error.Failed", "no")
        pause_bluetooth_source()


async def main():
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    bus.export("/", ObjectManager())
    bus.export(PLAYER, MediaPlayer())
    await bus.request_name("org.bluez")
    open(os.path.join(DIR, "ready"), "w").close()
    await asyncio.get_event_loop().create_future()


asyncio.run(main())
