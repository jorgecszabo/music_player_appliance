"""The rig: real PipeWire, WirePlumber, mpv, panda-arbiter and panda-web in a container,
supervised like supervise-daemon would, with fake sources and fake peers around them.

Runs as root inside the rig container (tests/graph/Containerfile); services run as the
appliance's unprivileged users. See TESTING.md section 12.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time

REPO = os.environ.get("RIG_REPO", "/repo")
PANDA = f"{REPO}/appliance/rootfs/etc/panda"
FAKES = f"{REPO}/tests/fakes"
SPEAKER_RUN = "/run/speaker"
PANDA_RUN = "/run/panda"
RIG_RUN = "/run/rig"
LOGS = os.environ.get("RIG_LOGS", "/tmp/rig-logs")
WEB_PORT = 8080
RESPAWN_DELAY = 0.5            # the init scripts use 2-3 s; scaled like the arbiter's timers
PAUSE_GRACE = 0.5
ROUTE_SETTLE = 0.3

# Source name (as the arbiter reports it) -> the PipeWire client name its stream has.
CLIENT = {"Radio": "radio", "AirPlay": "airplay", "Spotify": "spotify",
          "Bluetooth": "bluez_input.AA_BB_CC_DD_EE_FF.1"}
SOURCES = list(CLIENT)

SPEAKER_ENV = {"XDG_RUNTIME_DIR": SPEAKER_RUN, "PIPEWIRE_RUNTIME_DIR": SPEAKER_RUN,
               "HOME": "/var/lib/speaker"}


def wait_until(pred, timeout=10.0, what="condition", interval=0.05, rig=None):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            last = pred()
        except Exception as e:      # a probe failing is "not yet"
            last = e
            time.sleep(interval)
            continue
        if last:
            return last
        time.sleep(interval)
    extra = ""
    if rig is not None:
        extra = "\n" + rig.describe()
    raise AssertionError(f"timed out after {timeout}s waiting for {what}{extra}")


class Service:
    def __init__(self, name, argv, user, env=None, pre=None, ready=None, respawn=True, cwd="/"):
        self.name, self.argv, self.user = name, argv, user
        self.env = env or {}
        self.pre, self.ready_fn, self.respawn, self.cwd = pre, ready, respawn, cwd
        self.proc = None
        self.state = "stopped"          # started | starting | stopped | crashed
        self.wanted = False             # the test wants it running (supervisor respawns)
        self.starts = 0
        self.exits = []                 # (time, exit code) of every run that ended


class Rig:
    def __init__(self):
        self.services = {}
        self.locks = {}
        self.lock = threading.Lock()
        self.control = None
        self.threads = []
        self.admin_calls = []
        self.background = 0
        self.log_mark = 0              # decisions() and tracebacks cover the arbiter log from here
        self.closing = False
        self.fake_sources = {}
        self.icy = None
        for d in (LOGS, RIG_RUN):
            os.makedirs(d, exist_ok=True)
        shutil.rmtree(f"{RIG_RUN}/fake", ignore_errors=True)
        for sub in ("soloist", "bluez", "route"):
            os.makedirs(f"{RIG_RUN}/fake/{sub}", exist_ok=True)
        subprocess.run(["chown", "-R", "speaker:speaker", f"{RIG_RUN}/fake"], check=True)
        for p in (SPEAKER_RUN, "/var/log/speaker", "/var/lib/speaker"):
            os.makedirs(p, exist_ok=True)
            subprocess.run(["chown", "speaker:speaker", p], check=True)
        os.chmod(SPEAKER_RUN, 0o750)       # as the init script: group speaker may enter, not write
        os.makedirs(PANDA_RUN, exist_ok=True)
        os.makedirs("/var/lib/panda-web", exist_ok=True)
        subprocess.run(["chown", "-R", "panda-web:panda-web", "/var/lib/panda-web"], check=True)
        os.chmod(RIG_RUN, 0o755)
        os.makedirs(f"{RIG_RUN}/bus", exist_ok=True)
        subprocess.run(["chown", "speaker:speaker", f"{RIG_RUN}/bus"], check=True)
        self.conf = f"{RIG_RUN}/conf"
        os.makedirs(self.conf, exist_ok=True)
        self._write_configs()
        self._start_control()
        self._define_services()

    # --- configuration -----------------------------------------------------------------
    def _write_configs(self):
        open(f"{self.conf}/board.conf", "w").write('BOARD_NAME="rig"\nSPOTIFY=yes\nAUDIO_CARD=""\n')
        open(f"{self.conf}/appliance.conf", "w").write('SPEAKER_NAME="Rig"\n')
        open(f"{self.conf}/audio.conf", "w").write("OUTPUT=onboard\n")
        os.makedirs(f"{self.conf}/pipewire.conf.d", exist_ok=True)
        os.makedirs("/etc/pipewire/pipewire.conf.d", exist_ok=True)
        open("/etc/pipewire/pipewire.conf.d/10-rig.conf", "w").write("""
context.properties = { module.portal = false  module.jackdbus-detect = false }
context.objects = [
  { factory = adapter
    args = { factory.name = support.null-audio-sink
             node.name = alsa_output.rig
             media.class = Audio/Sink
             audio.position = [ FL FR ]
             object.linger = true } }
]
""")
        # one second of tone, looped by the fake sources
        import wave
        import math
        import struct
        with wave.open("/tmp/tone.wav", "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(b"".join(struct.pack("<hh", int(3000 * math.sin(i / 20)), int(3000 * math.sin(i / 20)))
                                   for i in range(48000)))
        os.chmod("/tmp/tone.wav", 0o644)

    def env(self, extra=None):
        e = {"PATH": f"{FAKES}/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1",
             "PYTHONPATH": REPO}
        e.update(extra or {})
        return e

    def _define_services(self):
        speaker = dict(SPEAKER_ENV)
        mpv_args = ["mpv", "--idle=yes", "--no-video", "--no-terminal", "--config=no", "--ao=pipewire",
                    "--audio-client-name=radio", f"--input-ipc-server={SPEAKER_RUN}/radio.sock",
                    "--cache=yes", "--cache-secs=20", "--network-timeout=15", "--msg-level=all=warn",
                    "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=10"]
        arbiter_env = dict(speaker, PANDA_SPEAKER_RUN_DIR=SPEAKER_RUN, PANDA_RUN_DIR=PANDA_RUN,
                           PANDA_SOLOIST_CTL=f"{FAKES}/bin/fake-soloist", FAKE_SOLOIST_DIR=f"{RIG_RUN}/fake/soloist",
                           PANDA_BOARD_CONF=f"{self.conf}/board.conf", PANDA_PAUSE_GRACE_S=str(PAUSE_GRACE),
                           PANDA_ROUTE_SETTLE_S=str(ROUTE_SETTLE), PANDA_ROUTE_CMD=f"{FAKES}/bin/fake-route",
                           FAKE_ROUTE_LOG=f"{RIG_RUN}/fake/route/calls", PANDA_DEBUG_EVENTS="1",
                           DBUS_SYSTEM_BUS_ADDRESS=f"unix:path={RIG_RUN}/bus/dbus.sock")
        web_env = {"PANDA_WEB_PORT": str(WEB_PORT), "PANDA_CONF_DIR": self.conf, "PANDA_WEB_DIR": f"{PANDA}/web",
                   "PANDA_SPEAKER_RUN_DIR": SPEAKER_RUN, "PANDA_RUN_DIR": PANDA_RUN,
                   "PANDA_STATE_DIR": "/var/lib/panda-web", "PANDA_ADMIN_CMD": f"{FAKES}/bin/fake-admin",
                   "PANDA_PERIOD_SCALE": "0.1", "HOME": "/var/lib/panda-web",
                   "PANDA_RADIO_BROWSER_BASE": os.environ.get("RIG_RADIO_BROWSER", "")}
        if not web_env["PANDA_RADIO_BROWSER_BASE"]:
            del web_env["PANDA_RADIO_BROWSER_BASE"]
        add = self.add
        add(Service("speaker-pipewire", ["pipewire"], "speaker", speaker,
                    pre=lambda: [os.unlink(p) for p in (f"{SPEAKER_RUN}/pipewire-0",) if os.path.exists(p)],
                    ready=lambda: os.path.exists(f"{SPEAKER_RUN}/pipewire-0") and chmod(f"{SPEAKER_RUN}/pipewire-0")))
        add(Service("speaker-wireplumber", ["wireplumber"], "speaker", speaker,
                    ready=lambda: time.monotonic() - 1.5 > 0))
        add(Service("speaker-radio", mpv_args, "speaker", speaker,
                    pre=lambda: [os.unlink(p) for p in (f"{SPEAKER_RUN}/radio.sock",) if os.path.exists(p)],
                    ready=lambda: os.path.exists(f"{SPEAKER_RUN}/radio.sock") and chmod(f"{SPEAKER_RUN}/radio.sock")))
        add(Service("speaker-arbiter", ["python3", f"{PANDA}/panda-arbiter.py"], "speaker", arbiter_env,
                    ready=lambda: os.path.exists(f"{SPEAKER_RUN}/nowplaying.json")))
        add(Service("panda-web", ["python3", f"{PANDA}/panda-web.py"], "panda-web", web_env,
                    ready=lambda: tcp_open(WEB_PORT), cwd="/var/lib/panda-web"))
        add(Service("fake-dbus", ["dbus-daemon", "--session", f"--address=unix:path={RIG_RUN}/bus/dbus.sock", "--nofork",
                                  "--nopidfile"], "speaker", speaker,
                    ready=lambda: os.path.exists(f"{RIG_RUN}/bus/dbus.sock")))
        add(Service("fake-bluez", ["python3", f"{FAKES}/fake_bluez.py"], "speaker",
                    dict(speaker, DBUS_SYSTEM_BUS_ADDRESS=f"unix:path={RIG_RUN}/bus/dbus.sock",
                         FAKE_BLUEZ_DIR=f"{RIG_RUN}/fake/bluez"),
                    ready=lambda: os.path.exists(f"{RIG_RUN}/fake/bluez/ready")))

    def add(self, svc):
        self.services[svc.name] = svc
        self.locks[svc.name] = threading.RLock()     # like rc-service: one start/stop/restart at a time

    # --- supervisor ---------------------------------------------------------------------
    def _spawn(self, svc):
        if svc.pre:
            svc.pre()
        log = open(f"{LOGS}/{svc.name}.log", "ab")
        log.write(f"\n--- start {time.strftime('%H:%M:%S')} ---\n".encode())
        log.flush()
        svc.state = "starting"
        svc.starts += 1
        svc.proc = subprocess.Popen(svc.argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    user=svc.user, group=svc.user, extra_groups=["speaker"] if svc.user != "speaker" else None,
                                    env=self.env(svc.env), cwd=svc.cwd, start_new_session=True)
        threading.Thread(target=self._watch, args=(svc, svc.proc), daemon=True).start()

    def _watch(self, svc, proc):
        """Mark started when ready; notice exits; respawn like supervise-daemon."""
        t0 = time.monotonic()
        while proc.poll() is None:
            if svc.state == "starting" and svc.proc is proc:
                try:
                    if svc.ready_fn is None or svc.ready_fn():
                        svc.state = "started"
                except Exception:
                    pass
            time.sleep(0.05)
            if time.monotonic() - t0 > 30 and svc.state == "starting":
                svc.state = "started"
        svc.exits.append((time.strftime("%H:%M:%S"), proc.returncode))
        if svc.proc is not proc or self.closing:
            return
        with self.lock:
            if svc.proc is not proc:        # restarted between the check above and now
                return
            if not svc.wanted:
                svc.state = "stopped"
                return
            svc.state = "crashed"
        if svc.respawn:
            time.sleep(RESPAWN_DELAY)
            with self.lock:
                if svc.wanted and svc.proc is proc and not self.closing:
                    self._spawn(svc)

    def start(self, name, wait=True, timeout=20):
        svc = self.services[name]
        with self.locks[name]:
            with self.lock:
                svc.wanted = True
                if svc.proc is None or svc.proc.poll() is not None:
                    self._spawn(svc)
            if wait:
                wait_until(lambda: svc.state == "started", timeout, f"{name} started", rig=self)

    def stop(self, name, sig=signal.SIGTERM):
        svc = self.services[name]
        with self.locks[name]:
            with self.lock:
                svc.wanted = False
                proc = svc.proc
            if proc and proc.poll() is None:
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(5)
            svc.state = "stopped"

    def restart(self, name, wait=True):
        with self.locks[name]:
            self.stop(name)
            self.start(name, wait=wait)

    def kill(self, name, sig=signal.SIGKILL):
        """Crash it: the supervisor respawns it after RESPAWN_DELAY."""
        svc = self.services[name]
        os.killpg(svc.proc.pid, sig)

    def hang(self, name):
        os.killpg(self.services[name].proc.pid, signal.SIGSTOP)

    def unhang(self, name):
        os.killpg(self.services[name].proc.pid, signal.SIGCONT)

    def states(self):
        return {n: s.state for n, s in self.services.items() if not n.startswith("fake-")}

    # --- control socket (rc-status / web-admin fakes) -----------------------------------------
    def _start_control(self):
        path = f"{RIG_RUN}/control.sock"
        if os.path.exists(path):
            os.unlink(path)
        self.control = socket.socket(socket.AF_UNIX)
        self.control.bind(path)
        os.chmod(path, 0o666)
        self.control.listen(8)

        def serve():
            while not self.closing:
                try:
                    conn, _ = self.control.accept()
                except OSError:
                    return
                threading.Thread(target=self._control, args=(conn,), daemon=True).start()
        threading.Thread(target=serve, daemon=True).start()

    def _control(self, conn):
        try:
            conn.settimeout(5)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            req = json.loads(buf)
            if req["cmd"] == "states":
                out = self.states()
            else:
                self.admin_calls.append(req["args"])
                out = {"out": self._admin(req["args"])}
            conn.sendall(json.dumps(out).encode() + b"\n")
        except (OSError, ValueError, KeyError):
            pass
        finally:
            conn.close()

    def _admin(self, args):
        """What web-admin.sh does for the verbs the web page uses (restarts run in the background)."""
        if args[:1] == ["restart"] and len(args) == 2 and args[1] in self.services:
            self._background(self.restart, args[1], False)
            return ""
        if args[:1] == ["restart-audio"]:
            self._background(self.restart_audio)
            return ""
        return "ok"

    def _background(self, fn, *args):
        """Like web-admin.sh's bg(): returns at once; busy() says whether any is still running."""
        def run():
            try:
                fn(*args)
            finally:
                with self.lock:
                    self.background -= 1
        with self.lock:
            self.background += 1
        threading.Thread(target=run, daemon=True).start()

    def busy(self):
        return self.background > 0

    def restart_audio(self):
        """web-admin.sh restart_audio, in its order."""
        for n in ("speaker-arbiter", "speaker-radio", "speaker-wireplumber", "speaker-pipewire"):
            self.stop(n)
        for n in ("speaker-pipewire", "speaker-wireplumber", "speaker-radio", "speaker-arbiter"):
            self.start(n)

    # --- bring up / tear down -----------------------------------------------------------------
    def up(self, web=True):
        self.start("fake-dbus")
        self.start("fake-bluez")
        self.start("speaker-pipewire")
        self.start("speaker-wireplumber")
        wait_until(lambda: any(s["name"] == "alsa_output.rig" for s in self.nodes()), 15, "the null sink", rig=self)
        self.start("speaker-radio")
        self.start("speaker-arbiter")
        if web:
            self.start("panda-web")
        for src in ("AirPlay", "Spotify", "Bluetooth"):
            self.fake_sources[src] = FakeSource(self, src)
        self.fake_sources["Bluetooth"].sock_alias = f"{RIG_RUN}/fake/bluez/bluetooth.sock"
        self.fake_sources["Spotify"].sock_alias = f"{RIG_RUN}/fake/soloist/spotify.sock"

    def down(self):
        self.closing = True
        for fs in self.fake_sources.values():
            fs.close()
        for name in list(self.services):
            try:
                self.stop(name, signal.SIGKILL)
            except Exception:
                pass
        try:
            self.control.close()
        except OSError:
            pass

    # --- observation ---------------------------------------------------------------------------
    def pw_dump(self):
        r = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=10,
                           env=self.env(SPEAKER_ENV))
        return json.loads(r.stdout)

    def nodes(self):
        out = []
        for o in self.pw_dump():
            if o.get("type") != "PipeWire:Interface:Node":
                continue
            info = o.get("info") or {}
            props = info.get("props") or {}
            pr = ((info.get("params") or {}).get("Props") or [{}])[0]
            out.append({"id": o["id"], "name": props.get("node.name"), "app": props.get("application.name"),
                        "class": props.get("media.class"), "state": info.get("state"),
                        "mute": bool(pr.get("mute")), "volume": pr.get("volume")})
        return out

    def streams(self):
        """Playback streams by source name -> {id, state, mute}. Ignores the chime & tone filter."""
        by_client = {v: k for k, v in CLIENT.items()}
        out = {}
        for n in self.nodes():
            if n["class"] == "Stream/Output/Audio" and n["name"] in by_client:
                out[by_client[n["name"]]] = n
        return out

    # --- driving sources -----------------------------------------------------------------------
    def play(self, source, url=None):
        """Start `source`: the real mpv for Radio (an endless fake stream), a fake player otherwise."""
        if source == "Radio":
            from panda_ipc import mpv_raw
            mpv_raw("set_property", "pause", False)
            reply = mpv_raw("loadfile", url or self.icy.base + "/stream", "replace", timeout=5)
            assert reply.get("error") == "success", f"mpv refused loadfile: {reply}"
        else:
            self.fake_sources[source].play()

    def stop_source(self, source):
        if source == "Radio":
            from panda_ipc import mpv
            mpv("stop")
        else:
            self.fake_sources[source].stop()

    def wait_state(self, source, state, timeout=10):
        return wait_until(lambda: (self.streams().get(source) or {}).get("state") == state, timeout,
                          f"{source} {state}", rig=self)

    def wait_running(self, source, timeout=10):
        return self.wait_state(source, "running", timeout)

    def wait_not_running(self, source, timeout=10):
        return wait_until(lambda: source not in self.running(), timeout, f"{source} not running", rig=self)

    def settle(self, quiet=1.0, timeout=15):
        """Wait until the audible set, running set and decisions stop changing for `quiet` s."""
        last, since = None, time.monotonic()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            cur = (tuple(self.audible()), tuple(self.running()), len(self.decisions()))
            if cur != last:
                last, since = cur, time.monotonic()
            elif time.monotonic() - since >= quiet:
                return cur
            time.sleep(0.1)
        raise AssertionError("the graph never settled\n" + self.describe())

    def audible(self):
        return sorted(s for s, n in self.streams().items() if n["state"] == "running" and not n["mute"])

    def running(self):
        return sorted(s for s, n in self.streams().items() if n["state"] == "running")

    def nowplaying(self):
        try:
            return json.load(open(f"{SPEAKER_RUN}/nowplaying.json"))
        except (OSError, ValueError):
            return {}

    def log(self, name):
        try:
            return open(f"{LOGS}/{name}.log", errors="replace").read()
        except OSError:
            return ""

    def decisions(self):
        out = []
        for line in self.log("speaker-arbiter")[self.log_mark:].splitlines():
            if "DECISION " in line:
                try:
                    out.append(json.loads(line.split("DECISION ", 1)[1]))
                except ValueError:
                    pass
        return out

    def calls(self, what):
        """Recorded calls of a fake: 'soloist', 'bluez' or 'route'."""
        path = f"{RIG_RUN}/fake/{what}/calls"
        try:
            return open(path).read().splitlines()
        except OSError:
            return []

    def describe(self):
        try:
            st = {s: (n["state"], n["mute"]) for s, n in self.streams().items()}
        except Exception as e:
            st = f"(pw-dump failed: {e})"
        exits = {n: s.exits[-3:] for n, s in self.services.items() if s.exits}
        return (f"rig: services={self.states()} streams={st} nowplaying={self.nowplaying()} exits={exits}\n"
                f"decisions={self.decisions()[-8:]}")

    def dump_artifacts(self, dest):
        os.makedirs(dest, exist_ok=True)
        for n in list(self.services):
            try:
                shutil.copy(f"{LOGS}/{n}.log", f"{dest}/{n}.log")
            except OSError:
                pass
        try:
            json.dump(self.pw_dump(), open(f"{dest}/pw-dump.json", "w"), indent=1)
        except Exception:
            pass
        open(f"{dest}/summary.txt", "w").write(self.describe())

    def arbiter_tracebacks(self):
        return [ln for ln in self.log("speaker-arbiter")[self.log_mark:].splitlines() if "Traceback" in ln]


class FakeSource:
    """A pausable audio source that looks like a phone app to the arbiter: an idle mpv whose
    PipeWire client name is the source's. play()/pause()/resume()/stop() go over its IPC socket."""

    def __init__(self, rig, source, suffix=""):
        self.rig, self.source = rig, source
        self.sock = f"{SPEAKER_RUN}/fake-{source.lower()}{suffix}.sock"
        self.sock_alias = None
        self.proc = None
        self.start()

    def start(self):
        if os.path.exists(self.sock):
            os.unlink(self.sock)
        log = open(f"{LOGS}/fake-{self.source.lower()}.log", "ab")
        self.proc = subprocess.Popen(
            ["mpv", "--idle=yes", "--no-video", "--no-terminal", "--config=no", "--ao=pipewire",
             f"--audio-client-name={CLIENT[self.source]}", f"--input-ipc-server={self.sock}", "--loop-file=inf",
             "--msg-level=all=warn"],
            user="speaker", group="speaker", env=self.rig.env(SPEAKER_ENV), stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
        wait_until(lambda: os.path.exists(self.sock), 10, f"fake {self.source} socket")
        # The fakes that other fakes control (soloist/bluez) reach it through a socket they can find.
        if self.sock_alias:
            self._link()

    def _link(self):
        try:
            if os.path.lexists(self.sock_alias):
                os.unlink(self.sock_alias)
            os.symlink(self.sock, self.sock_alias)
        except OSError:
            pass

    def ipc(self, *cmd):
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(5)
            s.connect(self.sock)
            s.sendall(json.dumps({"command": list(cmd), "request_id": 7}).encode() + b"\n")
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    raise OSError("closed")
                buf += chunk
                for line in buf.splitlines():
                    m = json.loads(line)
                    if m.get("request_id") == 7:
                        return m

    def play(self):
        if self.sock_alias:
            self._link()
        self.ipc("set_property", "pause", False)
        self.ipc("loadfile", "/tmp/tone.wav")

    def pause(self):
        self.ipc("set_property", "pause", True)

    def resume(self):
        self.ipc("set_property", "pause", False)

    def stop(self):
        self.ipc("stop")

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def chmod(path):
    os.chmod(path, 0o660)
    return True


def tcp_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def api(path, body=None, timeout=10):
    """(http status, parsed JSON) for a panda-web request; never raises on 4xx/5xx."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{WEB_PORT}{path}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Panda-Panel": "1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")
