#!/usr/bin/env python3
"""Panda Control Panel: the speaker's web page (stdlib only).

Runs as user panda-web (group speaker: PipeWire socket) on port 8080; the
firewall redirects port 80 to it. LAN only (firewall).

  GET  /                     the page (/etc/panda/web/)
  GET  /api/status           status snapshot (JSON)
  GET  /api/events           the same, live, as server-sent events
  GET  /api/logs?name=<log>  last lines of a service log
  POST /api/volume           {"id": <node>, "volume": 0..1} | {"id", "mute": bool}
  POST /api/bt               {"cmd": "pair", "open": bool} | {"cmd": "forget", "path"}
                             | {"cmd": "forget-all"}       (panda-bt control socket)
  POST /api/service          {"name": <service>} | {"name": "audio"}
  POST /api/system           {"action": "save" | "reboot"}
  POST /api/name             {"name": <speaker name>}
  POST /api/spotify-key      {"key": <new API key>}  (write-only: never returned or logged)
  POST /api/audio            {"bass": dB, "treble": dB, "cap": step}  (saved after 15 s)
  GET  /api/radio/search     ?q=&tag=&country=&order=votes|clickcount|name&page=N  (radio-browser.info)
  GET  /api/radio/tags       ?q=<text>   genre suggestions (most stations first)
  GET  /api/radio/countries  all countries with stations (code, name, count)
  GET  /api/radio/favorites
  POST /api/radio            {"cmd": "play", "uuid"} | {"cmd": "stop"} | {"cmd": "fav", "uuid", "add": bool}
  GET  /api/wifi/scan        scan now (a few seconds) and list the networks in range
  POST /api/wifi             {"cmd": "connect", "ssid", "password"?, "hidden"?} | {"cmd": "forget", "ssid"}
                             | {"cmd": "settings", "country"?, "adapter"?}   (panda-wifi control socket;
                             passwords are passed through, never stored, logged or returned here)

Privileged actions run /etc/panda/web-admin.sh through doas (it validates).
Protections: Host header must name this device (DNS rebinding), and POSTs need
JSON + the X-Panda-Panel header (browsers can't send that cross-site without a
CORS preflight, which we never allow).
"""
import json
import os
import pty
import re
import socket
import subprocess
import threading
import time
import traceback
import tty
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PANDA_WEB_PORT", "8080"))
WEB_DIR = "/etc/panda/web"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8")}
ADMIN = ["doas", "-n", "/etc/panda/web-admin.sh"]
PW_ENV = dict(os.environ, PIPEWIRE_RUNTIME_DIR="/run/speaker", XDG_RUNTIME_DIR="/run/speaker")
BT_SOCK = "/run/panda/bt.sock"
WIFI_SOCK = "/run/panda/wifi.sock"
SERVICES = ["panda-firewall", "panda-audio-init", "speaker-pipewire", "speaker-wireplumber",
            "speaker-soloist", "speaker-shairport", "speaker-radio", "speaker-arbiter", "bluetooth",
            "panda-bt", "panda-wifi", "avahi-daemon",
            "chronyd", "sshd", "crond", "panda-web"]
RESTARTABLE = {"speaker-soloist", "speaker-shairport", "speaker-radio", "panda-bt", "panda-wifi", "bluetooth",
               "avahi-daemon", "chronyd"}
LOGS = ["pipewire", "wireplumber", "soloist", "shairport", "arbiter", "radio", "panda-bt", "panda-wifi", "panda-web",
        "panda-web-admin", "messages"]


def run(cmd, timeout=10, env=None, stdin=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env=env, input=stdin).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def shell_conf(path):
    """KEY=value pairs of a simple shell config (no expansion)."""
    out = {}
    for line in read(path).splitlines():
        m = re.match(r'^([A-Z_][A-Z0-9_]*)=(.*)$', line)
        if m:
            v = m.group(2).strip()
            q = re.match(r'^(["\'])(.*?)\1', v)          # quoted (single line)
            if q:
                v = q.group(2)
            else:
                v = re.split(r'\s+#', v, maxsplit=1)[0].strip()   # unquoted; drop "# comment"
            out[m.group(1)] = v
    return out


def bt_call(req, timeout=5, sock=BT_SOCK, name="panda-bt"):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(sock)
            s.sendall(json.dumps(req).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf)
    except (OSError, ValueError) as e:
        return {"error": f"{name} unavailable ({e.__class__.__name__})"}


def wifi_call(req, timeout=5):
    return bt_call(req, timeout, WIFI_SOCK, "panda-wifi")


# --- internet radio: radio-browser.info + mpv (speaker-radio) ---------------------------
RADIO_DNS = "all.api.radio-browser.info"
RADIO_UA = "PandaSpeaker/1.0 (Alpine Linux audio appliance; panda-web)"
MPV_SOCK = "/run/speaker/radio.sock"
FAVORITES = "/var/lib/panda-web/radio-favorites.json"
STATION_FIELDS = ("stationuuid", "name", "codec", "bitrate", "countrycode", "tags", "votes",
                  "clickcount", "hls")
RADIO = {"station": None}
_radio_cache = {}


_rb_servers = {"names": [], "at": 0.0}


def rb_servers():
    """radio-browser's mirrors, as its docs ask: resolve all.api.radio-browser.info,
    reverse-resolve each address to its server name (HTTPS needs the name)."""
    import random
    if _rb_servers["names"] and time.monotonic() - _rb_servers["at"] < 3600:
        return _rb_servers["names"]
    names = set()
    try:
        for a in {ai[4][0] for ai in socket.getaddrinfo(RADIO_DNS, 443, proto=socket.IPPROTO_TCP)}:
            try:
                names.add(socket.gethostbyaddr(a)[0])
            except OSError:
                pass
    except OSError:
        pass
    names = [n for n in names if n.endswith(".api.radio-browser.info")] or [RADIO_DNS]
    random.shuffle(names)
    _rb_servers.update(names=names, at=time.monotonic())
    return names


def rb_get(path, params=None, timeout=10, ttl=300):
    """GET a radio-browser API path (JSON), trying the mirrors in turn; cached."""
    import urllib.parse
    import urllib.request
    query = "?" + urllib.parse.urlencode(params) if params else ""
    key = path + query
    hit = _radio_cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl and not path.startswith("/json/url/"):
        return hit[1]
    err = None
    for i, server in enumerate(rb_servers()[:3]):
        req = urllib.request.Request(f"https://{server}{key}", headers={"User-Agent": RADIO_UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read(8 * 1024 * 1024))
            break
        except (OSError, ValueError) as e:
            err = e
            if i == 0:
                _rb_servers["at"] = 0.0   # rediscover next time
    else:
        raise err or OSError("no radio-browser server")
    if len(_radio_cache) > 300:
        _radio_cache.clear()
    _radio_cache[key] = (time.monotonic(), data)
    return data


def station_summary(st):
    out = {k: st.get(k) for k in STATION_FIELDS}
    out["name"] = str(out.get("name") or "").strip()[:120]
    out["tags"] = str(out.get("tags") or "")[:200]
    return out


def mpv(*cmd, timeout=3):
    """Send one command to mpv's JSON IPC; return its "data" (None on error)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(MPV_SOCK)
            s.sendall(json.dumps({"command": list(cmd), "request_id": 1}).encode() + b"\n")
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return None
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("request_id") == 1:
                        return msg.get("data") if msg.get("error") == "success" else None
    except OSError:
        return None


def radio_status():
    idle = mpv("get_property", "idle-active")
    if idle is None:
        return {"available": False}
    st = RADIO["station"]
    if idle:
        return {"available": True, "playing": False, "station": st}
    meta = mpv("get_property", "metadata") or {}
    title = meta.get("icy-title") or meta.get("StreamTitle") or ""
    params = mpv("get_property", "audio-params") or {}
    return {"available": True, "playing": True, "station": st,
            "title": str(title)[:200],
            "codec": mpv("get_property", "audio-codec-name") or "",
            "bitrate": mpv("get_property", "audio-bitrate"),
            "rate": params.get("samplerate"),
            "buffering": bool(mpv("get_property", "paused-for-cache"))}


def load_favorites():
    try:
        with open(FAVORITES) as f:
            favs = json.load(f)
        return [station_summary(s) for s in favs if isinstance(s, dict)][:100]
    except (OSError, ValueError):
        return []


def save_favorites(favs):
    tmp = FAVORITES + ".new"
    with open(tmp, "w") as f:
        json.dump(favs, f)
    os.replace(tmp, FAVORITES)
    schedule_save()


# --- status collection ----------------------------------------------------------------
class PwObjects:
    """PipeWire's objects, kept current by one long-running pw-dump --monitor.

    Running pw-dump every 2 s cost ~6% CPU on a Cortex-A8, plus parsing its
    160 KB of JSON here each time. The monitor prints every object once, then
    only what changes: partial objects (just the changed info fields) and
    {"id": N, "info": null} when an object is removed.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.objects = {}

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                self.monitor()
            except OSError as e:
                print(f"panda-web: pw-dump --monitor: {e}", flush=True)
            with self.lock:
                self.objects.clear()   # the next pw-dump starts with a full dump
            time.sleep(2)

    def monitor(self):
        # Same parsing as panda-arbiter: a pseudo-terminal so pw-dump
        # line-buffers, and each 2-space-indented object parsed when complete.
        master, slave = pty.openpty()
        tty.setraw(slave)
        p = subprocess.Popen(["pw-dump", "--monitor", "--no-colors"], stdout=slave,
                             stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=PW_ENV)
        os.close(slave)
        obj = None
        try:
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
        finally:
            p.kill()
            p.wait()

    def update(self, o):
        oid = o.get("id")
        if oid is None:
            return
        info = o.get("info", {})
        with self.lock:
            if info is None:
                self.objects.pop(oid, None)
                return
            cur = self.objects.get(oid)
            if cur is None:
                self.objects[oid] = o
                return
            for k, v in o.items():
                if k != "info":
                    cur[k] = v
            ci = cur.get("info") or {}
            cur["info"] = ci
            for k, v in (info or {}).items():
                if k in ("props", "params") and isinstance(v, dict):
                    ci[k] = {**(ci.get(k) or {}), **v}   # params: each key replaced whole
                else:
                    ci[k] = v

    def nodes(self):
        with self.lock:
            return [o for o in self.objects.values() if o.get("type") == "PipeWire:Interface:Node"]


PW = PwObjects()


class Collector:
    """Builds the status snapshot every 2 s while someone is looking.

    Fast data (PipeWire, Bluetooth, /proc) is read in the main loop; slow
    probes (doas helpers, pw-top) run in their own loop and are merged from
    a cache, so a new visitor gets data right away.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.status = {}
        self.seq = 0
        self.active_until = 0.0
        self.hits = 0
        self.prev_cpu = None
        self.slow = {}          # results of the slower probes
        self.slow_at = {}
        self.wake_fast = threading.Event()
        self.wake_slow = threading.Event()
        # (name, period in s), cheapest first.
        self.probes = [("services", 15, self.services), ("net", 10, self.net),
                       ("pwtop", 10, self.pwtop), ("lbu", 15, self.lbu), ("sync", 30, self.sync),
                       ("spotify", 30, self.spotify)]

    def active(self):
        return time.monotonic() < self.active_until

    def touch(self):
        idle = not self.active()
        self.active_until = time.monotonic() + 15
        if idle:
            self.wake_fast.set()
            self.wake_slow.set()

    def refresh(self, name):
        """Run a slow probe again soon (e.g. after saving)."""
        self.slow_at.pop(name, None)
        self.wake_slow.set()

    def start(self):
        threading.Thread(target=self.fast_loop, daemon=True).start()
        threading.Thread(target=self.slow_loop, daemon=True).start()

    def fast_loop(self):
        while True:
            if self.active():
                try:
                    snap = self.collect()
                except Exception as e:  # a probe must never kill the collector
                    snap = {"error": f"collector: {e}"}
                self.publish(snap)
            self.wake_fast.wait(2)
            self.wake_fast.clear()

    def slow_loop(self):
        while True:
            if self.active():
                for name, period, fn in self.probes:
                    now = time.monotonic()
                    if now - self.slow_at.get(name, -1e9) >= period:
                        self.slow_at[name] = now
                        try:
                            self.slow[name] = fn()
                        except Exception as e:
                            self.slow[name] = {"error": str(e)}
            self.wake_slow.wait(1)
            self.wake_slow.clear()

    def publish(self, snap):
        with self.cond:
            self.status = snap
            self.seq += 1
            self.cond.notify_all()

    def wait(self, seq, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != seq, timeout)
            return self.seq, self.status

    # --- probes ----------------------------------------------------------------
    def collect(self):
        board = shell_conf("/etc/panda/board.conf")
        app = shell_conf("/etc/panda/appliance.conf")
        return {
            "time": time.time(),
            "hits": self.hits,
            "host": self.host(board, app),
            "cpu": self.cpu(board),
            "mem": self.mem(),
            "net": self.slow.get("net"),
            "audio": self.audio(),
            "bt": bt_call({"cmd": "status"}),
            "wifi": wifi_call({"cmd": "status"}),
            "services": self.slow.get("services"),
            "sync": self.slow.get("sync"),
            "lbu": self.slow.get("lbu"),
            "tone": self.tone(board),
            "radio": radio_status(),
            "nowplaying": self.nowplaying(),
            "spotify": self.slow.get("spotify"),
        }

    def host(self, board, app):
        up = float(read("/proc/uptime", "0").split()[0])
        return {
            "hostname": socket.gethostname(),
            "speaker_name": app.get("SPEAKER_NAME", ""),
            "board": board.get("BOARD", ""),
            "board_name": board.get("BOARD_NAME", ""),
            "board_blurb": board.get("BOARD_BLURB", ""),
            "board_badge": board.get("BOARD_BADGE", ""),
            "bt_controller": board.get("BT_DESCRIPTION", ""),
            "kernel": os.uname().release,
            "alpine": read("/etc/alpine-release").strip(),
            "uptime": up,
        }

    def cpu(self, board):
        fields = [int(x) for x in read("/proc/stat").splitlines()[0].split()[1:]]
        idle, total = fields[3] + fields[4], sum(fields)
        pct = None
        if self.prev_cpu:
            dt = total - self.prev_cpu[1]
            pct = round(100 * (1 - (idle - self.prev_cpu[0]) / dt), 1) if dt else 0.0
        self.prev_cpu = (idle, total)
        temp = None
        want = board.get("THERMAL_ZONE_TYPE", "cpu_thermal")
        for z in sorted(os.listdir("/sys/class/thermal")):
            if z.startswith("thermal_zone") and read(f"/sys/class/thermal/{z}/type").strip() == want:
                t = read(f"/sys/class/thermal/{z}/temp").strip()
                temp = int(t) / 1000 if t.lstrip("-").isdigit() else None
                break
        return {"percent": pct, "load": [float(x) for x in read("/proc/loadavg").split()[:3]],
                "temp": temp, "cores": os.cpu_count()}

    def mem(self):
        m = {}
        for line in read("/proc/meminfo").splitlines():
            k, v = line.split(":", 1)
            m[k] = int(v.split()[0])
        return {"total": m.get("MemTotal", 0), "available": m.get("MemAvailable", 0),
                "shmem": m.get("Shmem", 0)}

    def net(self):
        addrs = []
        for line in run(["ip", "-o", "addr", "show"]).splitlines():
            parts = line.split()
            if len(parts) > 3 and parts[1] != "lo" and parts[2] in ("inet", "inet6"):
                addrs.append({"if": parts[1], "family": parts[2], "addr": parts[3]})
        links = {}
        for ifname in os.listdir("/sys/class/net"):
            if ifname == "lo":
                continue
            links[ifname] = {"state": read(f"/sys/class/net/{ifname}/operstate").strip(),
                             "speed": read(f"/sys/class/net/{ifname}/speed").strip(),
                             "mac": read(f"/sys/class/net/{ifname}/address").strip()}
        return {"addrs": addrs, "links": links}

    def services(self):
        states = {}
        for line in run(["rc-status", "-a", "-f", "ini"]).splitlines():
            m = re.match(r"^\s*([\w.-]+)\s*=\s*(\w+)", line)
            if m:
                states[m.group(1)] = m.group(2)
        return {s: states.get(s, "not in a runlevel") for s in SERVICES}

    def sync(self):
        out = run(ADMIN + ["chrony"])
        info = {}
        for line in out.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                info[k.strip()] = v.strip()
        return {"reference": info.get("Reference ID", ""), "stratum": info.get("Stratum", ""),
                "offset": info.get("System time", ""), "leap": info.get("Leap status", ""),
                "raw": out.strip() if not info else ""}

    def nowplaying(self):
        try:
            with open("/run/speaker/nowplaying.json") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def tone(self, board):
        a = shell_conf("/etc/panda/audio.conf")
        def num(v, d=0):
            try:
                return int(v)
            except (TypeError, ValueError):
                return d
        hw_max = num(board.get("AUDIO_HW_VOLUME_MAX"))
        return {"bass": num(a.get("BASS_DB")), "treble": num(a.get("TREBLE_DB")),
                "cap": num(a.get("MAX_VOLUME"), hw_max), "cap_max": hw_max,
                "cap_step_db": num(board.get("AUDIO_HW_VOLUME_STEP_DB"), 1),
                "available": bool(board.get("AUDIO_HW_VOLUME_CONTROL"))}

    def spotify(self):
        """Whether an API key is set and when it changed. Never the key itself."""
        out = run(ADMIN + ["soloist-key-status"]).split()
        return {"key_set": bool(out) and out[0] == "set",
                "key_changed": out[1] if len(out) > 1 else None}

    def lbu(self):
        out = run(ADMIN + ["lbu-status"])
        files = [l.strip() for l in out.splitlines() if re.match(r"^[UAD] ", l.strip())]
        return {"changes": len(files), "files": files[:50]}

    def pwtop(self):
        """Quantum and error (xrun) counts per node id, from pw-top."""
        lines = run(["pw-top", "-b", "-n", "2"], timeout=6, env=PW_ENV).splitlines()
        heads = [i for i, l in enumerate(lines) if " QUANT " in l]
        last = lines[heads[-1] + 1:] if heads else []
        res = {}
        for line in last:
            f = line.split()
            if len(f) > 8 and f[1].isdigit():
                res[int(f[1])] = {"quantum": f[2], "errors": f[8]}
        return res

    def audio(self):
        top = self.slow.get("pwtop") or {}
        sink, output, streams = None, None, []
        for o in PW.nodes():
            info = o.get("info") or {}
            props = info.get("props") or {}
            params = info.get("params") or {}
            pr = (params.get("Props") or [{}])[0]
            vols = pr.get("channelVolumes") or [pr.get("volume", 1.0)]
            vol = (sum(vols) / len(vols)) ** (1 / 3) if vols else 1.0  # wpctl's cubic scale
            fmt = (params.get("Format") or [{}])[0]
            node = {
                "id": o["id"],
                "name": props.get("node.name", ""),
                "description": props.get("node.description") or props.get("media.name", ""),
                "state": info.get("state", ""),
                "volume": round(vol, 3),
                "mute": bool(pr.get("mute", False)),
                "format": fmt.get("format", ""),
                "rate": fmt.get("rate"),
                "channels": fmt.get("channels"),
                **top.get(o["id"], {}),
            }
            cls = props.get("media.class", "")
            if cls == "Audio/Sink" and node["name"] == "panda-eq":
                sink = node          # master volume: the tone filter's sink
            elif cls == "Audio/Sink" and node["name"].startswith("alsa_output"):
                output = node        # the codec, fixed at 100%
            elif cls == "Stream/Output/Audio" and node["name"] not in ("chime", "panda-eq.output"):
                n = node["name"]
                app = props.get("application.name", "")
                if n.startswith("bluez_input"):
                    node["source"] = "Bluetooth"
                    node["codec"] = props.get("api.bluez5.codec", "")
                    node["device"] = props.get("node.description") or props.get("api.bluez5.address", "")
                elif n == "airplay" or "shairport" in n.lower() or "shairport" in app.lower():
                    node["source"] = "AirPlay"
                elif n == "spotify" or "spotify" in app.lower() or "soloist" in app.lower():
                    node["source"] = "Spotify"
                elif n == "radio" or app.lower() == "radio":
                    node["source"] = "Radio"
                else:
                    node["source"] = app or n
                streams.append(node)
        if sink is None:     # no tone filter: the codec's sink is the master
            sink = output
        if sink is not None and output is not None and sink is not output:
            sink = {**sink, "format": output["format"], "rate": output["rate"],
                    "quantum": output.get("quantum"), "errors": output.get("errors"),
                    "description": output["description"] + " (via tone filter)"}
        return {"sink": sink, "streams": streams}


COLLECTOR = Collector()

_save_timer = None
_save_lock = threading.Lock()


def schedule_save(delay=15):
    """Save to the card once, delay seconds after the last change (debounce)."""
    global _save_timer
    def save():
        run(ADMIN + ["save"], timeout=60)
        COLLECTOR.refresh("lbu")
    with _save_lock:
        if _save_timer:
            _save_timer.cancel()
        _save_timer = threading.Timer(delay, save)
        _save_timer.daemon = True
        _save_timer.start()


def local_hosts():
    names = {"localhost", "127.0.0.1", "[::1]"}
    hn = socket.gethostname()
    names |= {hn, f"{hn}.local"}
    for line in run(["ip", "-o", "addr", "show"]).splitlines():
        p = line.split()
        if len(p) > 3 and p[2] in ("inet", "inet6"):
            a = p[3].split("/")[0]
            names.add(a if p[2] == "inet" else f"[{a}]")
    return names


class Hosts:
    names, at = set(), 0.0

    @classmethod
    def ok(cls, host):
        if not host:
            return False
        h = host.lower()
        h = h.rsplit(":", 1)[0] if not h.endswith("]") and h.count(":") == 1 else h
        if h.startswith("[") and "]:" in h:
            h = h.split("]:")[0] + "]"
        if time.monotonic() - cls.at > 30:
            cls.names, cls.at = local_hosts(), time.monotonic()
        return h in cls.names


# --- HTTP ------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "PandaHTTPd/1.0"
    sys_version = ""

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/"):
            print(f"{self.client_address[0]} {fmt % args}", flush=True)

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self'; script-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")

    def end_headers(self):
        self.security_headers()
        super().end_headers()

    def check_host(self):
        if Hosts.ok(self.headers.get("Host", "")):
            return True
        self.send_json({"error": "unknown host"}, 421)
        return False

    def do_GET(self):
        if not self.check_host():
            return
        url = urlparse(self.path)
        if url.path in STATIC:
            name, ctype = STATIC[url.path]
            body = read(os.path.join(WEB_DIR, name)).encode()
            if url.path == "/":
                COLLECTOR.hits += 1
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/status":
            COLLECTOR.touch()
            seq, snap = COLLECTOR.wait(-1, 0)
            if not snap:
                seq, snap = COLLECTOR.wait(seq, 5)
            self.send_json(snap)
        elif url.path == "/api/events":
            self.events()
        elif url.path == "/api/radio/search":
            q = parse_qs(url.query)
            arg = lambda k: (q.get(k, [""])[0] or "").strip()[:60]
            order = arg("order") if arg("order") in ("votes", "clickcount", "name") else "votes"
            page = int(arg("page")) if arg("page").isdigit() else 0
            page = min(page, 250)
            # One extra row tells whether there's a next page.
            params = {"limit": 41, "offset": page * 40, "hidebroken": "true", "order": order,
                      "reverse": "false" if order == "name" else "true"}
            if arg("q"):
                params["name"] = arg("q")
            if arg("tag"):
                params["tag"] = arg("tag").lower()
            if re.fullmatch(r"[A-Za-z]{2}", arg("country")):
                params["countrycode"] = arg("country").upper()
            try:
                res = [s for s in rb_get("/json/stations/search", params) if isinstance(s, dict)]
                self.send_json({"stations": [station_summary(s) for s in res[:40]],
                                "page": page, "more": len(res) > 40})
            except Exception as e:
                self.send_json({"error": f"radio-browser.info: {e.__class__.__name__}"}, 502)
        elif url.path == "/api/radio/tags":
            q = (parse_qs(url.query).get("q", [""])[0] or "").strip().lower()[:40]
            try:
                if len(q) < 2:
                    tags = rb_get("/json/tags", {"order": "stationcount", "reverse": "true",
                                                 "hidebroken": "true", "limit": 40}, ttl=86400)
                else:
                    import urllib.parse
                    tags = rb_get("/json/tags/" + urllib.parse.quote(q, safe=""),
                                  {"order": "stationcount", "reverse": "true",
                                   "hidebroken": "true", "limit": 25}, ttl=86400)
                self.send_json({"tags": [{"name": str(t.get("name", ""))[:60],
                                          "count": t.get("stationcount", 0)}
                                         for t in tags if isinstance(t, dict) and t.get("name")]})
            except Exception as e:
                self.send_json({"error": f"radio-browser.info: {e.__class__.__name__}"}, 502)
        elif url.path == "/api/radio/countries":
            try:
                cs = rb_get("/json/countries", {"order": "name", "hidebroken": "true"}, ttl=86400)
                self.send_json({"countries": [{"code": str(c.get("iso_3166_1", ""))[:2].upper(),
                                               "name": str(c.get("name", ""))[:60],
                                               "count": c.get("stationcount", 0)}
                                              for c in cs if isinstance(c, dict)
                                              and re.fullmatch(r"[A-Za-z]{2}", str(c.get("iso_3166_1", "")))]})
            except Exception as e:
                self.send_json({"error": f"radio-browser.info: {e.__class__.__name__}"}, 502)
        elif url.path == "/api/wifi/scan":
            res = wifi_call({"cmd": "scan"}, timeout=20)
            self.send_json(res, 502 if "error" in res else 200)
        elif url.path == "/api/radio/favorites":
            self.send_json({"stations": load_favorites()})
        elif url.path == "/api/logs":
            name = parse_qs(url.query).get("name", [""])[0]
            if name not in LOGS:
                return self.send_json({"error": "unknown log"}, 400)
            self.send_json({"name": name, "text": run(ADMIN + ["logs", name])})
        else:
            self.send_json({"error": "not found"}, 404)

    def events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seq = -1
        try:
            self.wfile.write(b"retry: 3000\n\n")
            while True:
                COLLECTOR.touch()
                new, snap = COLLECTOR.wait(seq, 15)
                if new != seq and snap:
                    seq = new
                    self.wfile.write(b"data: " + json.dumps(snap).encode() + b"\n\n")
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, TimeoutError):
            pass

    def do_POST(self):
        if not self.check_host():
            return
        if self.headers.get("X-Panda-Panel") != "1" or \
                not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send_json({"error": "forbidden"}, 403)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
            if not isinstance(req, dict):
                raise ValueError
        except ValueError:
            return self.send_json({"error": "bad request"}, 400)
        path = urlparse(self.path).path
        handler = {"/api/volume": self.volume, "/api/bt": self.bt,
                   "/api/service": self.service, "/api/system": self.system,
                   "/api/name": self.name, "/api/spotify-key": self.spotify_key,
                   "/api/audio": self.audio_settings, "/api/radio": self.radio,
                   "/api/wifi": self.wifi}.get(path)
        if handler is None:
            return self.send_json({"error": "not found"}, 404)
        try:
            self.send_json(handler(req))
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except OSError as e:
            # Network trouble, e.g. radio-browser.info resetting the
            # connection (it does that now and then): say so instead of
            # dropping the request.
            reason = getattr(e, "reason", None) or e.strerror or e.__class__.__name__
            self.send_json({"error": f"network error: {reason}"}, 502)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": f"internal error: {e.__class__.__name__}"}, 500)

    # --- actions ---------------------------------------------------------------
    def volume(self, req):
        node = int(req.get("id", -1))
        audio = COLLECTOR.status.get("audio") or {}
        ids = {n["id"] for n in audio.get("streams", [])}
        if audio.get("sink"):
            ids.add(audio["sink"]["id"])
        if node not in ids:
            raise ValueError("unknown node")
        if "mute" in req:
            run(["wpctl", "set-mute", str(node), "1" if req["mute"] else "0"], env=PW_ENV)
        if "volume" in req:
            v = min(1.0, max(0.0, float(req["volume"])))
            run(["wpctl", "set-volume", str(node), f"{v:.3f}"], env=PW_ENV)
        return {"ok": True}

    def bt(self, req):
        cmd = req.get("cmd")
        if cmd == "pair":
            return bt_call({"cmd": "pair", "open": bool(req.get("open"))})
        if cmd == "forget":
            return bt_call({"cmd": "forget", "path": str(req.get("path", ""))}, timeout=30)
        if cmd == "forget-all":
            return bt_call({"cmd": "forget-all"})
        raise ValueError("unknown command")

    def wifi(self, req):
        cmd = req.get("cmd")
        if cmd == "connect":
            out = {"cmd": "connect", "ssid": str(req.get("ssid", ""))[:64],
                   "hidden": bool(req.get("hidden"))}
            if req.get("password"):
                out["password"] = str(req["password"])[:64]
            res = wifi_call(out, timeout=15)
        elif cmd == "forget":
            res = wifi_call({"cmd": "forget", "ssid": str(req.get("ssid", ""))[:64]})
        elif cmd == "settings":
            res = wifi_call({"cmd": "settings", **{k: str(req[k])[:32] for k in ("country", "adapter")
                                                   if k in req}})
        else:
            raise ValueError("unknown command")
        if "error" in res:
            raise ValueError(res["error"])
        COLLECTOR.wake_fast.set()
        return res

    def service(self, req):
        name = req.get("name")
        if name == "audio":
            run(ADMIN + ["restart-audio"])
        elif name in RESTARTABLE:
            run(ADMIN + ["restart", name])
        else:
            raise ValueError("not restartable")
        COLLECTOR.refresh("services")   # its period is 15 s
        return {"ok": True}

    def system(self, req):
        action = req.get("action")
        if action == "save":
            out = run(ADMIN + ["save"], timeout=60)
            COLLECTOR.refresh("lbu")
            return {"ok": "saved" in out}
        if action == "reboot":
            run(ADMIN + ["reboot"])
            return {"ok": True}
        if action == "factory-reset-check":
            return {"ok": True, "result": run(ADMIN + ["factory-reset-dry"]).strip()}
        if action == "factory-reset":
            out = run(ADMIN + ["factory-reset"], timeout=20).strip()
            return {"ok": out == "resetting", "result": out}
        raise ValueError("unknown action")

    def name(self, req):
        name = str(req.get("name", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._'-]{0,31}", name):
            raise ValueError("names: 1-32 letters, digits, spaces and . _ - '")
        out = run(ADMIN + ["set-name", name], timeout=15)
        return {"ok": bool(out.strip()), "result": out.strip()}


    def radio(self, req):
        cmd = req.get("cmd")
        if cmd == "stop":
            mpv("stop")
            return {"ok": True}
        uuid = str(req.get("uuid", ""))
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
            raise ValueError("bad station id")
        if cmd == "fav":
            favs = [f for f in load_favorites() if f.get("stationuuid") != uuid]
            if req.get("add"):
                st = rb_get(f"/json/stations/byuuid/{uuid}")
                if not st:
                    raise ValueError("unknown station")
                favs.append(station_summary(st[0]))
            save_favorites(favs)
            return {"ok": True, "stations": favs}
        if cmd == "play":
            st = rb_get(f"/json/stations/byuuid/{uuid}")
            if not st:
                raise ValueError("unknown station")
            # /json/url/<uuid> counts a click (as the API asks) and gives the URL.
            res = rb_get(f"/json/url/{uuid}")
            stream = (res or {}).get("url") or st[0].get("url_resolved") or st[0].get("url") or ""
            if not re.match(r"^https?://", stream):
                raise ValueError("station has no http(s) stream")
            if mpv("loadfile", stream, "replace", timeout=5) is None and mpv("get_property", "idle-active") is None:
                raise ValueError("the radio player isn't running")
            RADIO["station"] = station_summary(st[0])
            return {"ok": True, "station": RADIO["station"]}
        raise ValueError("unknown command")

    def audio_settings(self, req):
        try:
            b, t, c = (int(req.get(k)) for k in ("bass", "treble", "cap"))
        except (TypeError, ValueError):
            raise ValueError("bass, treble and cap must be integers")
        out = run(ADMIN + ["set-audio", str(b), str(t), str(c)], timeout=20).strip()
        if out != "ok":
            raise ValueError("rejected")
        schedule_save()
        return {"ok": True}

    def spotify_key(self, req):
        key = str(req.get("key", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{16,200}", key):
            raise ValueError("that doesn't look like an API key")
        out = run(ADMIN + ["set-soloist-key"], timeout=60, stdin=key + "\n").strip()
        del key
        COLLECTOR.refresh("spotify")
        COLLECTOR.refresh("services")
        COLLECTOR.refresh("lbu")
        if out.endswith("running") and not out.endswith("not running"):
            return {"ok": True, "result": "running"}
        return {"ok": False, "result": out.splitlines()[-1] if out else "failed"}


class Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


if __name__ == "__main__":
    PW.start()
    COLLECTOR.start()
    print(f"panda-web: listening on port {PORT}", flush=True)
    Server(("::", PORT), Handler).serve_forever()
