#!/usr/bin/env python3
"""Bluetooth speaker control: pairing agent, pairing button, LED, reconnect.

Runs as root (service panda-bt): it saves pairings with lbu and drives LEDs.

- Agent "NoInputNoOutput" (Just Works). Pairing is accepted only while the
  pairing window is open; paired devices are marked trusted, so they can
  reconnect at any time without asking.
- Button (board.conf BUTTON_INPUT_NAME / BUTTON_KEY):
    short press        open the pairing window for WINDOW_S seconds (press
                       again to close it). Discoverable + pairable only then.
    hold LONG_S secs   forget all paired devices.
- LED (board.conf LED_BT): off = idle, on = a device is connected,
  fast blink = pairing window, slow blink = forgetting.
- New pairings and forgets are saved with `lbu commit` (only then: routine
  connects never write the card).
- At start, try to reconnect to the paired devices (most recent first).
- Control socket /run/panda/bt.sock (group panda-web, for the web page):
  one JSON request per line, one JSON reply per line. Commands:
  {"cmd": "status"}, {"cmd": "pair", "open": true|false},
  {"cmd": "forget", "path": "<device path>"}, {"cmd": "forget-all"},
  {"cmd": "media", "action": "play|pause|next|previous"} (AVRCP, the connected phone).
- If bluetoothd goes away, exit; supervise-daemon restarts us.
- Controller watchdog: every HEALTH_S seconds, send the controller a harmless HCI
  command. A combo chip whose power is cut under its firmware (the WLAN_EN edge on the
  PandaBoard's WL1271) stops answering while hci0 still looks UP, so BlueZ and everything
  above it keep saying "fine". After HEALTH_FAILS silent probes in a row, power-cycle it
  (web-admin.sh bt-reset reloads board.conf BT_DRIVER_AFTER_WIFI). Reported as "alive"
  in the status.
"""
import asyncio
import grp
import json
import os
import select
import socket
import struct
import subprocess
import sys
import time

import evdev
from dbus_next import BusType, DBusError, Message, MessageType, Variant
from dbus_next.errors import InterfaceNotFoundError
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method

WINDOW_S = 120
LONG_S = 5
HEALTH_S = 30          # between probes of the controller
HEALTH_FAILS = 2       # silent probes in a row before it is power-cycled
PROBE_TIMEOUT_S = 5
# Events with a chime (/etc/panda/chime.sh <event>).
CHIMES = {"pairing-on", "pairing-off", "paired", "connected", "disconnected", "forgot-all"}
AGENT_PATH = "/panda/agent"
CONTROL_SOCK = "/run/panda/bt.sock"
CONTROL_GROUP = "panda-web"
BLUEZ = "org.bluez"


def log(*a):
    print("panda-bt:", *a, flush=True)


def hci_probe(index, timeout=PROBE_TIMEOUT_S):
    """Does controller hci<index> answer HCI Read Local Version? True / False; None if we
    can't tell (no Bluetooth sockets, no permission, no such device): not a failure."""
    try:
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    except (OSError, AttributeError):
        return None
    try:
        # Only command-complete / command-status events (hci_filter: type mask, event mask, opcode).
        s.setsockopt(0, 2, struct.pack("<IIIH2x", 1 << 4, (1 << 0x0E) | (1 << 0x0F), 0, 0))   # SOL_HCI, HCI_FILTER
        s.bind((index,))
        s.send(b"\x01\x01\x10\x00")                  # command packet, opcode 0x1001, no parameters
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            if not select.select([s], [], [], left)[0]:
                break
            ev = s.recv(260)
            # 04 0e len ncmd opcode(2) status...   command complete
            # 04 0f len status ncmd opcode(2)      command status (the controller refused)
            if ev[:2] == b"\x04\x0e" and ev[4:6] == b"\x01\x10":
                return True
            if ev[:2] == b"\x04\x0f" and ev[5:7] == b"\x01\x10":
                return True
        return False
    except OSError:
        return None
    finally:
        s.close()


def load_conf():
    keys = ["SPEAKER_NAME", "BT_CONTROLLER", "BUTTON_INPUT_NAME", "BUTTON_KEY", "LED_BT",
            "BT_DRIVER_AFTER_WIFI"]
    script = ". /etc/panda/board.conf; . /etc/panda/appliance.conf; " + "".join(
        f'printf "%s\\0" "${k}"; ' for k in keys)
    vals = subprocess.run(["sh", "-c", script], capture_output=True, check=True).stdout
    return dict(zip(keys, vals.decode().split("\0")))


class Led:
    def __init__(self, name):
        self.d = f"/sys/class/leds/{name}" if name else None

    def _w(self, f, v):
        if not self.d:
            return
        try:
            with open(f"{self.d}/{f}", "w") as fh:
                fh.write(str(v))
        except OSError:
            pass

    def set(self, on):
        self._w("trigger", "none")
        self._w("brightness", 255 if on else 0)

    def blink(self, on_ms, off_ms):
        self._w("trigger", "timer")
        self._w("delay_on", on_ms)
        self._w("delay_off", off_ms)


class Agent(ServiceInterface):
    def __init__(self, app):
        super().__init__("org.bluez.Agent1")
        self.app = app

    @method()
    def Release(self):
        pass

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        raise DBusError("org.bluez.Error.Rejected", "PIN pairing not supported")

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        pass

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        raise DBusError("org.bluez.Error.Rejected", "passkey pairing not supported")

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        self.app.check_pairing(device)

    @method()
    def RequestAuthorization(self, device: "o"):
        self.app.check_pairing(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        self.app.check_service(device, uuid)

    @method()
    def Cancel(self):
        pass


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.hci = cfg["BT_CONTROLLER"] or "hci0"
        self.adapter_path = f"/org/bluez/{self.hci}"
        self.led = Led(cfg["LED_BT"])
        self.devices = {}          # path -> {"Paired", "Trusted", "Connected", "Alias"}
        self.recent = []           # device paths, most recently connected first
        self.window_task = None
        self.window_end = 0.0
        self.forgetting = False
        self.paired_at = -100.0
        self.alive = None          # the controller answers HCI (None: not probed yet / can't tell)
        self.probe = hci_probe     # (index) -> True / False / None; replaced in tests

    # --- state ------------------------------------------------------------------
    @property
    def window_open(self):
        return self.window_task is not None and not self.window_task.done()

    def show_state(self):
        if self.forgetting:
            self.led.blink(500, 500)
        elif self.window_open:
            self.led.blink(100, 100)
        else:
            self.led.set(any(d.get("Connected") for d in self.devices.values()))

    def event(self, name, detail=""):
        log("event", name, detail)
        now = time.monotonic()
        if name == "paired":
            self.paired_at = now
        elif name in ("connected", "pairing-off") and now - self.paired_at < 5:
            return  # the "paired" chime already covers these
        if name in CHIMES:
            subprocess.Popen(["/etc/panda/chime.sh", name], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def check_pairing(self, device):
        if not self.window_open:
            log("rejected pairing outside the window:", device)
            raise DBusError("org.bluez.Error.Rejected", "not in pairing mode")
        log("accepting pairing:", device)

    def check_service(self, device, uuid):
        d = self.devices.get(device, {})
        if self.window_open or d.get("Trusted") or d.get("Paired"):
            return
        log("rejected service", uuid, "for", device)
        raise DBusError("org.bluez.Error.Rejected", "unknown device")

    # --- D-Bus helpers --------------------------------------------------------------
    async def iface(self, path, name):
        intro = await self.bus.introspect(BLUEZ, path)
        return self.bus.get_proxy_object(BLUEZ, path, intro).get_interface(name)

    async def add_match(self, rule):
        await self.bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                    interface="org.freedesktop.DBus", member="AddMatch",
                                    signature="s", body=[rule]))

    def update_device(self, path, props):
        if not path.startswith(self.adapter_path + "/dev_"):
            return
        d = self.devices.setdefault(path, {})
        before = dict(d)
        for k in ("Paired", "Trusted", "Connected", "Alias", "Address"):
            if k in props:
                v = props[k]
                d[k] = v.value if isinstance(v, Variant) else v
        name = d.get("Alias", path)
        if d.get("Paired") and not before.get("Paired"):
            if before:  # not the initial load
                asyncio.ensure_future(self.paired(path))
        if d.get("Connected") != before.get("Connected") and before:
            if d.get("Connected"):
                if path in self.recent:
                    self.recent.remove(path)
                self.recent.insert(0, path)
                self.event("connected", name)
            else:
                self.event("disconnected", name)
        self.show_state()

    def on_message(self, msg):
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.member == "NameOwnerChanged" and msg.body[0] == BLUEZ and not msg.body[2]:
            if not self.bluez_gone.done():
                self.bluez_gone.set_result("bluetoothd went away")
        elif msg.member == "PropertiesChanged" and msg.body[0] == "org.bluez.Device1":
            self.update_device(msg.path, msg.body[1])
        elif msg.member == "InterfacesAdded" and msg.body[0] == self.adapter_path \
                and "org.bluez.Adapter1" in msg.body[1]:
            self.adapter_added.set()
        elif msg.member == "InterfacesRemoved" and msg.body[0] == self.adapter_path \
                and "org.bluez.Adapter1" in msg.body[1]:
            # A USB dongle was unplugged: exit, and the supervisor restarts us
            # into waiting for it again.
            if not self.bluez_gone.done():
                self.bluez_gone.set_result("adapter removed")
        elif msg.member == "InterfacesAdded" and "org.bluez.Device1" in msg.body[1]:
            self.update_device(msg.body[0], msg.body[1]["org.bluez.Device1"])
        elif msg.member == "InterfacesRemoved" and "org.bluez.Device1" in msg.body[1]:
            self.devices.pop(msg.body[0], None)
            if msg.body[0] in self.recent:
                self.recent.remove(msg.body[0])
            self.show_state()

    # --- actions --------------------------------------------------------------------
    async def save(self, why):
        async with self.save_lock:
            log("saving with lbu:", why)
            proc = await asyncio.create_subprocess_exec("flock", "/run/panda/media.lock", "lbu", "commit")
            if await proc.wait() != 0:
                log("ERROR: lbu commit failed")

    async def set_window(self, open_):
        await self.adapter.set_pairable(open_)
        await self.adapter.set_discoverable(open_)

    async def window(self):
        try:
            await self.set_window(True)
            self.window_end = time.monotonic() + WINDOW_S
            log(f"pairing window open ({WINDOW_S} s)")
            self.event("pairing-on")
            self.show_state()
            await asyncio.sleep(WINDOW_S)
        finally:
            await self.set_window(False)
            log("pairing window closed")
            self.event("pairing-off")
            # Let show_state see the task as done.
            asyncio.get_running_loop().call_soon(self.show_state)

    def toggle_window(self):
        self.set_window_state(not self.window_open)

    def set_window_state(self, open_):
        if open_ and not self.window_open:
            self.window_task = asyncio.ensure_future(self.window())
        elif not open_ and self.window_open:
            self.window_task.cancel()

    async def forget(self, path):
        d = self.devices.get(path)
        if d is None:
            raise ValueError("unknown device")
        log("forgetting", d.get("Alias", path))
        await self.adapter.call_remove_device(path)
        self.event("forgot", d.get("Alias", path))
        await self.save(f"forgot {d.get('Alias', path)}")

    # --- control socket (web page) -------------------------------------------------
    async def media(self, action):
        """AVRCP command to the connected phone's media player."""
        if action not in ("play", "pause", "next", "previous"):
            raise ValueError(f"unknown media action {action!r}")
        root = await self.iface("/", "org.freedesktop.DBus.ObjectManager")
        for path, ifaces in (await root.call_get_managed_objects()).items():
            if ("org.bluez.MediaPlayer1" in ifaces
                    and self.devices.get(path.rsplit("/", 1)[0], {}).get("Connected")):
                player = await self.iface(path, "org.bluez.MediaPlayer1")
                await getattr(player, f"call_{action}")()
                return {"ok": True}
        raise ValueError("no connected phone with a media player")

    async def status(self):
        track = None
        try:
            root = await self.iface("/", "org.freedesktop.DBus.ObjectManager")
            for path, ifaces in (await root.call_get_managed_objects()).items():
                p = ifaces.get("org.bluez.MediaPlayer1")
                if p and self.devices.get(path.rsplit("/", 1)[0], {}).get("Connected"):
                    t = p.get("Track", Variant("a{sv}", {})).value
                    track = {k.lower(): v.value for k, v in t.items()
                             if k in ("Title", "Artist", "Album", "Duration")}
                    track["status"] = p["Status"].value if "Status" in p else ""
                    track["position"] = p["Position"].value if "Position" in p else 0
        except DBusError:
            pass
        return {
            "adapter": self.adapter_path,
            "alias": self.cfg["SPEAKER_NAME"],
            "window": self.window_open,
            "window_left": max(0, round(self.window_end - time.monotonic())) if self.window_open else 0,
            "forgetting": self.forgetting,
            "alive": self.alive,
            "devices": [dict(path=p, address=d.get("Address", ""), name=d.get("Alias", ""),
                             paired=bool(d.get("Paired")), trusted=bool(d.get("Trusted")),
                             connected=bool(d.get("Connected")))
                        for p, d in self.devices.items()],
            "track": track,
        }

    async def control(self, req):
        cmd = req.get("cmd")
        if cmd == "status":
            return await self.status()
        if cmd == "pair":
            self.set_window_state(bool(req.get("open")))
            await asyncio.sleep(0.2)
            return {"window": self.window_open}
        if cmd == "forget":
            await self.forget(str(req.get("path", "")))
            return {"ok": True}
        if cmd == "forget-all":
            asyncio.ensure_future(self.forget_all())
            return {"ok": True}
        if cmd == "media":
            return await self.media(str(req.get("action", "")))
        raise ValueError(f"unknown command {cmd!r}")

    async def on_control(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError("a request is a JSON object")
                reply = await self.control(req)
            except (ValueError, DBusError) as e:
                reply = {"error": str(e)}
            except Exception as e:          # a bug must not leave the client without an answer
                log("control request failed:", repr(e))
                reply = {"error": "internal error"}
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    async def control_server(self):
        os.makedirs(os.path.dirname(CONTROL_SOCK), exist_ok=True)
        try:
            os.unlink(CONTROL_SOCK)
        except FileNotFoundError:
            pass
        server = await asyncio.start_unix_server(self.on_control, CONTROL_SOCK)
        try:
            os.chown(CONTROL_SOCK, 0, grp.getgrnam(CONTROL_GROUP).gr_gid)
            os.chmod(CONTROL_SOCK, 0o660)
        except KeyError:
            os.chmod(CONTROL_SOCK, 0o600)
        return server

    async def paired(self, path):
        name = self.devices.get(path, {}).get("Alias", path)
        log("paired:", name)
        try:
            dev = await self.iface(path, "org.bluez.Device1")
            await dev.set_trusted(True)
        except DBusError as e:
            log("can't trust", name, e)
        if self.window_open:
            self.window_task.cancel()
        self.event("paired", name)
        await self.save(f"paired {name}")

    async def forget_all(self):
        self.forgetting = True
        self.show_state()
        if self.window_open:
            self.window_task.cancel()
        for path, d in list(self.devices.items()):
            log("forgetting", d.get("Alias", path))
            try:
                await self.adapter.call_remove_device(path)
            except DBusError as e:
                log("can't remove", path, e)
        self.event("forgot-all")
        await self.save("forgot all devices")
        self.forgetting = False
        self.show_state()

    async def reconnect(self):
        await asyncio.sleep(3)
        paired = [p for p, d in self.devices.items() if d.get("Paired") and not d.get("Connected")]
        paired.sort(key=lambda p: self.recent.index(p) if p in self.recent else len(self.recent))
        for path in paired:
            if any(d.get("Connected") for d in self.devices.values()):
                return
            name = self.devices[path].get("Alias", path)
            log("reconnecting to", name)
            try:
                dev = await self.iface(path, "org.bluez.Device1")
                await asyncio.wait_for(dev.call_connect(), 20)
                log("reconnected to", name)
                return
            except (DBusError, asyncio.TimeoutError) as e:
                log("no reconnect to", name, type(e).__name__, getattr(e, "text", ""))

    async def button(self):
        want = self.cfg["BUTTON_INPUT_NAME"]
        key = evdev.ecodes.ecodes.get(self.cfg["BUTTON_KEY"] or "BTN_0")
        dev = None
        for p in evdev.list_devices():
            d = evdev.InputDevice(p)
            if d.name == want:
                dev = d
                break
        if dev is None:
            log(f"no button input device {want!r}; pairing only via D-Bus")
            return
        log("button:", dev.path, want, self.cfg["BUTTON_KEY"])
        long_task = None

        async def held():
            await asyncio.sleep(LONG_S)
            await self.forget_all()

        async for ev in dev.async_read_loop():
            if ev.type != evdev.ecodes.EV_KEY or ev.code != key:
                continue
            if ev.value == 1:
                long_task = asyncio.ensure_future(held())
            elif ev.value == 0 and long_task and not long_task.done():
                long_task.cancel()
                self.toggle_window()

    # --- main -----------------------------------------------------------------------
    async def health(self, sleep=asyncio.sleep):
        """Probe the controller; power-cycle it when it stops answering (see the docstring)."""
        index = int("".join(c for c in self.hci if c.isdigit()) or 0)
        loop = asyncio.get_running_loop()
        fails = 0
        while True:
            await sleep(HEALTH_S)
            ok = await loop.run_in_executor(None, self.probe, index)
            if ok is None:
                continue
            self.alive = ok
            if ok:
                if fails:
                    log("the controller answers again")
                fails = 0
                continue
            fails += 1
            log(f"the controller {self.hci} does not answer HCI ({fails}/{HEALTH_FAILS})")
            if fails < HEALTH_FAILS:
                continue
            fails = 0
            if not self.cfg.get("BT_DRIVER_AFTER_WIFI"):
                log("no way to power-cycle this controller (board.conf BT_DRIVER_AFTER_WIFI)")
                continue
            log("power-cycling the controller")
            self.led.set(False)
            await loop.run_in_executor(None, lambda: subprocess.run(
                ["/etc/panda/web-admin.sh", "bt-reset"], capture_output=True))
            await sleep(60)          # the reset stops and restarts us

    async def run(self):
        self.save_lock = asyncio.Lock()
        self.bluez_gone = asyncio.get_running_loop().create_future()
        self.adapter_added = asyncio.Event()
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.bus.add_message_handler(self.on_message)
        await self.add_match("type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.bluez'")
        await self.add_match("type='signal',sender='org.bluez'")

        # Wait for bluetoothd and the controller, however long it takes: boards
        # without onboard Bluetooth (BeagleBoard-xM) get one only when a USB
        # dongle is plugged in. Exiting instead made the supervisor restart us
        # every few seconds (a Python start is ~2 s of CPU on a Cortex-A8).
        # BlueZ's InterfacesAdded wakes us; the timeout covers a missed signal.
        waiting = False
        while True:
            try:
                self.adapter = await self.iface(self.adapter_path, "org.bluez.Adapter1")
                break
            except (DBusError, InterfaceNotFoundError):
                if not waiting:
                    log("waiting for the Bluetooth adapter", self.adapter_path)
                    waiting = True
                self.adapter_added.clear()
                try:
                    await asyncio.wait_for(self.adapter_added.wait(), 10)
                except asyncio.TimeoutError:
                    pass
        if self.bluez_gone.done():   # bluetoothd restarted while we waited: fine now
            self.bluez_gone = asyncio.get_running_loop().create_future()

        self.bus.export(AGENT_PATH, Agent(self))
        mgr = await self.iface("/org/bluez", "org.bluez.AgentManager1")
        await mgr.call_register_agent(AGENT_PATH, "NoInputNoOutput")
        await mgr.call_request_default_agent(AGENT_PATH)

        await self.adapter.set_powered(True)
        await self.adapter.set_alias(self.cfg["SPEAKER_NAME"] or "Speaker")
        await self.adapter.set_pairable_timeout(WINDOW_S)
        await self.adapter.set_discoverable_timeout(WINDOW_S)
        await self.set_window(False)

        root = await self.iface("/", "org.freedesktop.DBus.ObjectManager")
        for path, ifaces in (await root.call_get_managed_objects()).items():
            if "org.bluez.Device1" in ifaces:
                self.update_device(path, ifaces["org.bluez.Device1"])
        log(f"ready: {self.adapter_path} as {self.cfg['SPEAKER_NAME']!r},",
            sum(1 for d in self.devices.values() if d.get("Paired")), "paired device(s)")
        self.show_state()

        self.control_srv = await self.control_server()
        tasks = [asyncio.ensure_future(self.button()), asyncio.ensure_future(self.reconnect()),
                 asyncio.ensure_future(self.health())]
        why = await self.bluez_gone
        log(f"{why}; exiting")
        for t in tasks:
            t.cancel()
        self.led.set(False)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(App(load_conf()).run())
