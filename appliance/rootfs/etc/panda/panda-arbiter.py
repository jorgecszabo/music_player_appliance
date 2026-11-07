#!/usr/bin/env python3
"""Last source wins, output routing, now-playing info. Runs as user speaker (speaker-arbiter).

Watches PipeWire's playback streams (pw-dump --monitor, event driven). When a
source starts playing while another one plays, the older one is stopped:
  Spotify    soloist ctl pause (Soloist's WebSocket API on 127.0.0.1:47000)
  Bluetooth  AVRCP Pause (org.bluez.MediaPlayer1 of the phone)
  AirPlay    muted (no remote control without shairport's D-Bus interface)
Anything still playing 2 s after a pause request is muted too. A stream this
service muted is unmuted when it's the only one playing again, or when it
starts playing anew (then it wins).

Output routing: when an audio output (e.g. a USB card) or the tone filter
appears or goes away, /etc/panda/audio-route.py applies the output choice
(audio.conf OUTPUT) and the USB card's volume cap; when the tone filter
starts, also the saved master volume.

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

import panda_ipc

RT = "/opt/soloist-runtime"
SOLOIST_WS = "127.0.0.1:47000"
META_PIPE = "/run/speaker/shairport-metadata"
NOWPLAYING = "/run/speaker/nowplaying.json"
IGNORE = {"chime", "panda-eq.output"}
PAUSE_GRACE_S = 2.0
ROUTE = ["/usr/bin/python3", "/etc/panda/audio-route.py"]
ROUTE_SETTLE_S = 1.5    # let WirePlumber finish setting up a new card first


def log(*a):
    print("panda-arbiter:", *a, flush=True)


def soloist_bin():
    board = {}
    for line in open("/etc/panda/board.conf"):
        m = re.match(r"^([A-Z_]+)=\"?([^\"#\s]*)", line.strip())
        if m:
            board[m.group(1)] = m.group(2)
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
    try:
        panda_ipc.mpv_raw(*cmd)
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
    """Per source: track info, and a play clock shared by every web client.

    The clock is pos (s) as of pos_at (time.monotonic(), the same clock in
    every process), advancing while playing. It counts the stream's running
    time (kept across pauses, back to 0 on a new title) unless the source
    reports a real track position (AirPlay prgr, Spotify position_ms).
    """
    PRECISE_S = 3.0   # a new title this soon after a real position doesn't reset it

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}
        self.precise_at = {}

    def set(self, source, **fields):
        with self.lock:
            cur = self.data.get(source, {})
            title = fields.get("title")
            if (title is not None and title != cur.get("title")
                    and time.monotonic() - self.precise_at.get(source, -1e9) > self.PRECISE_S):
                self._clock(cur, 0.0, cur.get("playing", False))
                cur.pop("duration", None)
            cur.update({k: v for k, v in fields.items() if v is not None})
            cur["updated"] = time.time()
            self.data[source] = cur
            self.write()

    @staticmethod
    def _clock(cur, pos, playing):
        cur["pos"] = round(max(0.0, pos), 3)
        cur["pos_at"] = time.monotonic()
        cur["playing"] = bool(playing)

    @staticmethod
    def _now(cur):
        pos = cur.get("pos", 0.0)
        if cur.get("playing"):
            pos += time.monotonic() - cur.get("pos_at", time.monotonic())
        return pos

    def position(self, source, pos, playing=None, duration=None):
        """A real track position from the source."""
        with self.lock:
            cur = self.data.setdefault(source, {})
            self._clock(cur, pos, cur.get("playing", True) if playing is None else playing)
            if duration:
                cur["duration"] = round(duration, 3)
            self.precise_at[source] = time.monotonic()
            self.write()

    def running(self, source, playing):
        """The source's stream started or stopped running: run or hold the clock."""
        with self.lock:
            cur = self.data.setdefault(source, {})
            if bool(cur.get("playing")) != playing or "pos" not in cur:
                self._clock(cur, self._now(cur), playing)
                self.write()

    def drop(self, source, *keys):
        with self.lock:
            cur = self.data.get(source)
            if cur:
                for k in keys:
                    cur.pop(k, None)
                self.write()

    def clear(self, source):
        with self.lock:
            self.precise_at.pop(source, None)
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
        self.outputs = set()   # ids of alsa_output sinks and the tone filter's output
        self.route_timer = None
        self.route_master = False   # next run also restores the master volume

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
            if oid in self.outputs:
                self.outputs.discard(oid)
                self.schedule_route()
            return
        # Updates are partial (only the changed fields, often without "type").
        props = info.get("props") or {}
        if oid not in self.outputs and o.get("type") == "PipeWire:Interface:Node" and (
                props.get("node.name") == "panda-eq.output"
                or (props.get("media.class") == "Audio/Sink"
                    and props.get("node.name", "").startswith("alsa_output."))):
            self.outputs.add(oid)
            # The tone filter (re)started (boot, audio restart): its volume is
            # back at 100%, so restore the saved master volume.
            self.schedule_route(master=props.get("node.name") == "panda-eq.output")
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
            if state == "running" or prev == "running":
                NOW.running(known["source"], state == "running")
            if state == "running" and prev != "running":
                known["since"] = time.monotonic()
                threading.Thread(target=self.started, args=(oid,), daemon=True).start()
            elif prev == "running" and state != "running":
                threading.Thread(target=self.stopped, args=(oid,), daemon=True).start()

    # --- output routing --------------------------------------------------------------
    def schedule_route(self, master=False):
        """Run audio-route.py once things settle (a card brings several events)."""
        with self.lock:
            self.route_master = self.route_master or master
            if self.route_timer:
                self.route_timer.cancel()
            self.route_timer = threading.Timer(ROUTE_SETTLE_S, self.route)
            self.route_timer.daemon = True
            self.route_timer.start()

    def route(self):
        with self.lock:
            master, self.route_master = self.route_master, False
        try:
            r = subprocess.run(ROUTE + (["--master"] if master else []),
                               capture_output=True, text=True, timeout=60)
            for line in (r.stdout + r.stderr).strip().splitlines():
                log("route:", line)
        except (OSError, subprocess.TimeoutExpired) as e:
            log("audio-route:", e)

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
        NOW.clear(st["source"])   # Bluetooth: only the play clock (track info is panda-bt's)
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
                pos, dur = ms_field(d, t, "position_ms", "progress_ms"), ms_field(t, d, "duration_ms")
                if pos is not None:
                    playing = d.get("is_playing")
                    if playing is None and d.get("is_paused") is not None:
                        playing = not d["is_paused"]
                    NOW.position("Spotify", pos, playing=None if playing is None else bool(playing),
                                 duration=dur)
            time.sleep(5)


def ms_field(*dicts_then_keys):
    """The first numeric value (ms) of any of the keys in any of the dicts, in seconds."""
    dicts = [x for x in dicts_then_keys if isinstance(x, dict)]
    keys = [x for x in dicts_then_keys if isinstance(x, str)]
    for dct in dicts:
        for k in keys:
            v = dct.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                return v / 1000
    return None


# --- now playing: AirPlay (shairport-sync metadata pipe) ------------------------------
ITEM = re.compile(r"<item><type>([0-9a-f]{8})</type><code>([0-9a-f]{8})</code><length>(\d+)</length>"
                  r"(?:\s*<data encoding=\"base64\">\s*([A-Za-z0-9+/=\s]*?)</data>)?</item>", re.S)
# Track info (iPhone/Mac senders) and the sender itself (PipeWire sends no track info).
FIELDS = {"minm": "title", "asar": "artist", "asal": "album", "snua": "client", "clip": "client_ip"}


def airplay_progress(text):
    """prgr: RTP frames (44.1 kHz) "start/current/end" of the track, at play, resume and seek."""
    try:
        start, cur, end = (int(x) for x in text.strip().split("/"))
    except ValueError:
        return
    pos, dur = ((cur - start) % 2**32) / 44100, ((end - start) % 2**32) / 44100
    if pos <= dur:      # end is 0/garbage for senders without a track length
        NOW.position("AirPlay", pos, playing=True, duration=dur)
    else:
        NOW.position("AirPlay", pos, playing=True)


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
                        if code == "prgr" and data:
                            airplay_progress(base64.b64decode("".join(data.split())).decode("ascii", "replace"))
                        elif code in FIELDS and data:
                            val = base64.b64decode("".join(data.split())).decode("utf-8", "replace")
                            NOW.set("AirPlay", **{FIELDS[code]: val})
                        elif code == "pbeg":   # play stream begin: forget the last track
                            NOW.drop("AirPlay", "title", "artist", "album")
                        elif code == "pfls":   # flush: paused (or seeking; prgr follows)
                            NOW.running("AirPlay", False)
                        elif code == "prsm":   # resumed
                            NOW.running("AirPlay", True)
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
