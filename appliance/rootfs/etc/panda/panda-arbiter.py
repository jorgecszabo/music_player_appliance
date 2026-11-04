#!/usr/bin/env python3
"""Last source wins + now-playing info. Runs as user speaker (speaker-arbiter).

Watches PipeWire's playback streams (pw-dump --monitor, event driven). When a
source starts playing while another one plays, the older one is stopped:
  Spotify    soloist ctl pause (Soloist's WebSocket API on 127.0.0.1:47000)
  Bluetooth  AVRCP Pause (org.bluez.MediaPlayer1 of the phone)
  AirPlay    muted (no remote control without shairport's D-Bus interface)
Anything still playing 2 s after a pause request is muted too. A stream this
service muted is unmuted when it's the only one playing again, or when it
starts playing anew (then it wins).

Now playing: Spotify (soloist ctl now --json, while its stream plays) and
AirPlay (shairport-sync's metadata pipe) are written to
/run/speaker/nowplaying.json for the web page. Bluetooth track info comes
from panda-bt (AVRCP).
"""
import base64
import json
import os
import pty
import re
import subprocess
import sys
import threading
import time
import tty

RT = "/opt/soloist-runtime"
SOLOIST_WS = "127.0.0.1:47000"
META_PIPE = "/run/speaker/shairport-metadata"
NOWPLAYING = "/run/speaker/nowplaying.json"
IGNORE = {"chime", "panda-eq.output"}
PAUSE_GRACE_S = 2.0


def log(*a):
    print("panda-arbiter:", *a, flush=True)


def soloist_bin():
    board = {}
    for line in open("/etc/panda/board.conf"):
        m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
        if m:
            board[m.group(1)] = m.group(2).strip('"')
    return f"/media/{board.get('BOOT_MEDIA', 'mmcblk0p1')}/soloist/soloist"


def soloist_ctl(*args, timeout=10):
    cmd = [f"{RT}/lib/ld.so", "--library-path", f"{RT}/lib", soloist_bin(),
           "ctl", "-w", SOLOIST_WS, *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def mpv_command(*cmd):
    """One command over mpv's JSON IPC socket (speaker-radio)."""
    import socket
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(3)
            s.connect("/run/speaker/radio.sock")
            s.sendall(json.dumps({"command": list(cmd)}).encode() + b"\n")
            s.recv(4096)
    except OSError as e:
        log("mpv:", e)


def source_of(props):
    name = props.get("node.name", "")
    app = (props.get("application.name") or "").lower()
    if name.startswith("bluez_input"):
        return "Bluetooth"
    if name == "airplay" or "shairport" in name or "airplay" in app:
        return "AirPlay"
    if name == "spotify" or "spotify" in app or "soloist" in app:
        return "Spotify"
    if name == "radio" or app == "radio":
        return "Radio"
    return props.get("application.name") or name


class NowPlaying:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}

    def set(self, source, **fields):
        with self.lock:
            cur = self.data.get(source, {})
            cur.update({k: v for k, v in fields.items() if v is not None})
            cur["updated"] = time.time()
            self.data[source] = cur
            self.write()

    def clear(self, source):
        with self.lock:
            if self.data.pop(source, None) is not None:
                self.write()

    def write(self):
        tmp = NOWPLAYING + ".new"
        with open(tmp, "w") as f:
            json.dump(self.data, f)
        os.chmod(tmp, 0o640)
        os.replace(tmp, NOWPLAYING)


NOW = NowPlaying()


class Arbiter:
    def __init__(self):
        self.streams = {}      # id -> {"source", "state", "since", "props"}
        self.muted_by_us = set()
        self.lock = threading.Lock()

    # --- PipeWire ------------------------------------------------------------------
    def monitor(self):
        # pw-dump block-buffers a pipe (events would arrive in late bursts): give
        # it a pseudo-terminal, which it line-buffers.
        master, slave = pty.openpty()
        tty.setraw(slave)
        p = subprocess.Popen(["pw-dump", "--monitor", "--no-colors"], stdout=slave,
                             stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(slave)
        # pw-dump prints objects inside long-lived top-level arrays; each object
        # starts with "  {" and ends with "  }" / "  }," (2-space indent). Parse
        # each object as soon as it's complete (waiting for "]" delays events,
        # and re-parsing the whole buffer per line is quadratic).
        obj = None
        for line in os.fdopen(master, "r", errors="replace"):
            if obj is None:
                if line.rstrip() == "  {":
                    obj = [line]
                continue
            obj.append(line)
            if line.rstrip() in ("  }", "  },"):
                text = "".join(obj).rstrip().rstrip(",")
                obj = None
                try:
                    self.update(json.loads(text))
                except ValueError:
                    pass
        log("pw-dump exited; restarting")
        sys.exit(1)

    def update(self, o):
        oid = o.get("id")
        info = o.get("info")
        if os.environ.get("ARBITER_DEBUG") == "2":
            log(f"debug2: obj {oid} {o.get('type')} state={(info or {}).get('state')} "
                f"name={((info or {}).get('props') or {}).get('node.name')}")
        if info is None:          # object removed
            with self.lock:
                st = self.streams.pop(oid, None)
                self.muted_by_us.discard(oid)
            if st:
                self.stream_gone(st)
            return
        # Updates are partial (only the changed fields, often without "type").
        props = info.get("props") or {}
        with self.lock:
            known = self.streams.get(oid)
        if known is None:
            if o.get("type") != "PipeWire:Interface:Node":
                return
            if props.get("media.class") != "Stream/Output/Audio" or props.get("node.name") in IGNORE:
                return
            known = {"source": source_of(props), "state": None, "since": 0.0, "props": props}
            with self.lock:
                self.streams[oid] = known
        elif props:
            known["props"].update(props)
        state = info.get("state")
        if state and state != known["state"]:
            prev, known["state"] = known["state"], state
            if os.environ.get("ARBITER_DEBUG"):
                log(f"debug: {known['source']} node {oid}: {prev} -> {state}")
            if state == "running" and prev != "running":
                known["since"] = time.monotonic()
                threading.Thread(target=self.started, args=(oid,), daemon=True).start()
            elif prev == "running" and state != "running":
                threading.Thread(target=self.stopped, args=(oid,), daemon=True).start()

    # --- policy --------------------------------------------------------------------
    def running(self):
        with self.lock:
            return {i: dict(s) for i, s in self.streams.items() if s["state"] == "running"}

    def started(self, oid):
        with self.lock:
            me = self.streams.get(oid)
        if not me:
            return
        log(f"{me['source']} started (node {oid})")
        if oid in self.muted_by_us:
            self.set_mute(oid, False)
        if me["source"] == "Spotify":
            threading.Thread(target=self.spotify_meta_loop, args=(oid,), daemon=True).start()
        others = {i: s for i, s in self.running().items() if i != oid and i not in self.muted_by_us}
        for i, s in others.items():
            log(f"last source wins: stopping {s['source']} (node {i}) for {me['source']}")
            self.stop_source(i, s)

    def stopped(self, oid):
        with self.lock:
            me = self.streams.get(oid)
        if me:
            log(f"{me['source']} stopped (node {oid})")
        self.unmute_survivor()

    def stream_gone(self, st):
        log(f"{st['source']} stream removed")
        if st["source"] != "Bluetooth":
            NOW.clear(st["source"])
        self.unmute_survivor()

    def unmute_survivor(self):
        run = self.running()
        if len(run) == 1:
            (i,) = run
            if i in self.muted_by_us:
                log(f"{run[i]['source']} is the only source again: unmuting (node {i})")
                self.set_mute(i, False)

    def stop_source(self, oid, s):
        src = s["source"]
        if src == "Spotify":
            soloist_ctl("pause")
        elif src == "Radio":
            mpv_command("stop")
        elif src == "Bluetooth":
            self.bt_pause(s["props"].get("api.bluez5.address", ""))
        else:
            self.set_mute(oid, True)
            return
        time.sleep(PAUSE_GRACE_S)
        with self.lock:
            still = self.streams.get(oid, {}).get("state") == "running"
        if still:
            log(f"{src} still playing after the pause request: muting (node {oid})")
            self.set_mute(oid, True)

    def set_mute(self, oid, mute):
        subprocess.run(["wpctl", "set-mute", str(oid), "1" if mute else "0"],
                       capture_output=True, timeout=10)
        with self.lock:
            (self.muted_by_us.add if mute else self.muted_by_us.discard)(oid)

    def bt_pause(self, address):
        """AVRCP Pause on the phone's media player (bluez D-Bus, default policy)."""
        code = r'''
import asyncio, sys
from dbus_next.aio import MessageBus
from dbus_next import BusType
async def main(addr):
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    intro = await bus.introspect("org.bluez", "/")
    om = bus.get_proxy_object("org.bluez", "/", intro).get_interface("org.freedesktop.DBus.ObjectManager")
    dev = "dev_" + addr.replace(":", "_")
    for path, ifaces in (await om.call_get_managed_objects()).items():
        if "org.bluez.MediaPlayer1" in ifaces and dev in path:
            i2 = await bus.introspect("org.bluez", path)
            await bus.get_proxy_object("org.bluez", path, i2).get_interface("org.bluez.MediaPlayer1").call_pause()
            print("paused", path)
            return
    print("no media player for", addr)
asyncio.run(main(sys.argv[1]))
'''
        r = subprocess.run([sys.executable, "-c", code, address], capture_output=True, text=True, timeout=15)
        log("bluetooth pause:", (r.stdout or r.stderr).strip()[-200:])

    # --- now playing: Spotify --------------------------------------------------------
    def spotify_meta_loop(self, oid):
        """Poll soloist's now-playing while its stream plays (untested: needs a login)."""
        while True:
            with self.lock:
                st = self.streams.get(oid)
            if not st or st["state"] != "running":
                return
            out = soloist_ctl("now", "--json")
            try:
                d = json.loads(out)
            except ValueError:
                d = None
            if isinstance(d, dict):
                t = d.get("track") if isinstance(d.get("track"), dict) else d
                artists = t.get("artists") or t.get("artist")
                if isinstance(artists, list):
                    artists = ", ".join(a.get("name", "") if isinstance(a, dict) else str(a) for a in artists)
                album = t.get("album")
                if isinstance(album, dict):
                    album = album.get("name")
                NOW.set("Spotify", title=t.get("name") or t.get("title"),
                        artist=artists if isinstance(artists, str) else None,
                        album=album if isinstance(album, str) else None)
            time.sleep(5)


# --- now playing: AirPlay (shairport-sync metadata pipe) ------------------------------
ITEM = re.compile(r"<item><type>([0-9a-f]{8})</type><code>([0-9a-f]{8})</code><length>(\d+)</length>"
                  r"(?:\s*<data encoding=\"base64\">\s*([A-Za-z0-9+/=\s]*?)</data>)?</item>", re.S)
FIELDS = {"minm": "title", "asar": "artist", "asal": "album"}


def airplay_meta():
    while True:
        try:
            with open(META_PIPE, "r", errors="replace") as f:   # blocks until shairport opens it
                buf = ""
                for chunk in iter(lambda: f.read(1024), ""):
                    buf += chunk
                    while True:
                        m = ITEM.search(buf)
                        if not m:
                            buf = buf[-65536:]
                            break
                        buf = buf[m.end():]
                        code = bytes.fromhex(m.group(2)).decode("ascii", "replace")
                        data = m.group(4)
                        if code in FIELDS and data:
                            val = base64.b64decode("".join(data.split())).decode("utf-8", "replace")
                            NOW.set("AirPlay", **{FIELDS[code]: val})
                        elif code == "pend":   # play stream end
                            NOW.clear("AirPlay")
        except FileNotFoundError:
            time.sleep(5)
        except OSError as e:
            log("metadata pipe:", e)
            time.sleep(5)


if __name__ == "__main__":
    NOW.write()
    threading.Thread(target=airplay_meta, daemon=True).start()
    Arbiter().monitor()
