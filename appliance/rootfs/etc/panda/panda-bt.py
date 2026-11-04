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
- If bluetoothd goes away, exit; supervise-daemon restarts us.
"""
import asyncio
import subprocess
import sys
import time

import evdev
from dbus_next import BusType, DBusError, Message, MessageType, Variant
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method

WINDOW_S = 120
LONG_S = 5
AGENT_PATH = "/panda/agent"
BLUEZ = "org.bluez"


def log(*a):
    print("panda-bt:", *a, flush=True)


def load_conf():
    keys = ["SPEAKER_NAME", "BT_CONTROLLER", "BUTTON_INPUT_NAME", "BUTTON_KEY", "LED_BT"]
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
        self.forgetting = False

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
        # Hook for chimes (Phase 6).
        log("event", name, detail)

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
        for k in ("Paired", "Trusted", "Connected", "Alias"):
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
                self.event("bt-connected", name)
            else:
                self.event("bt-disconnected", name)
        self.show_state()

    def on_message(self, msg):
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.member == "NameOwnerChanged" and msg.body[0] == BLUEZ and not msg.body[2]:
            if not self.bluez_gone.done():
                self.bluez_gone.set_result(True)
        elif msg.member == "PropertiesChanged" and msg.body[0] == "org.bluez.Device1":
            self.update_device(msg.path, msg.body[1])
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
            proc = await asyncio.create_subprocess_exec("lbu", "commit")
            if await proc.wait() != 0:
                log("ERROR: lbu commit failed")

    async def set_window(self, open_):
        await self.adapter.set_pairable(open_)
        await self.adapter.set_discoverable(open_)

    async def window(self):
        try:
            await self.set_window(True)
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
        if self.window_open:
            self.window_task.cancel()
        else:
            self.window_task = asyncio.ensure_future(self.window())

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
    async def run(self):
        self.save_lock = asyncio.Lock()
        self.bluez_gone = asyncio.get_running_loop().create_future()
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.bus.add_message_handler(self.on_message)
        await self.add_match("type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.bluez'")
        await self.add_match("type='signal',sender='org.bluez'")

        for _ in range(60):  # wait for bluetoothd and the controller
            try:
                self.adapter = await self.iface(self.adapter_path, "org.bluez.Adapter1")
                break
            except DBusError:
                await asyncio.sleep(1)
        else:
            log("no adapter", self.adapter_path)
            sys.exit(1)

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

        tasks = [asyncio.ensure_future(self.button()), asyncio.ensure_future(self.reconnect())]
        await self.bluez_gone
        log("bluetoothd went away; exiting")
        for t in tasks:
            t.cancel()
        self.led.set(False)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(App(load_conf()).run())
