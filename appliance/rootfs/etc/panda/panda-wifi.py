#!/usr/bin/env python3
"""Wi-Fi client for the speaker (stdlib only). Runs as root (service panda-wifi).

- Adapters: every network interface with a phy80211, onboard (SDIO etc.) or
  USB. One is used at a time: "auto" prefers a USB adapter (usually 5 GHz and a
  better antenna; the onboard WL1271 is 2.4 GHz and shares its antenna with
  Bluetooth), else the onboard one. Plugging/unplugging a dongle switches.
- Runs wpa_supplicant on the active adapter and udhcpc once associated (route
  metric 300+: wired Ethernet, metric 200+, wins when both are up).
- Saved networks: /etc/panda/wifi.json (root, 0600), saved with lbu. WPA2
  passwords are stored as the PSK (PBKDF2 hash), never in clear; a WPA3-only
  (SAE) network needs the password itself, also only in that file.
- Factory/first boot: a wifi.json on the boot partition is imported, then
  deleted from the card:
      {"country": "DE", "networks": [{"ssid": "Home", "password": "..."}]}
  (or just {"ssid": ..., "password": ...}; "hidden": true, "wpa3": true optional).
- Onboard adapters with no MAC address of their own (the WL1271's comes out
  random every boot) get a stable one derived from board.conf WIFI_MAC_FROM.
- Control socket /run/panda/wifi.sock (group panda-web): one JSON request per
  line, one JSON reply. Commands: status, scan, results, connect {ssid,
  password, hidden}, forget {ssid}, settings {country, adapter}.
  Passwords and PSKs are never returned, logged or printed.

  panda-wifi.py --convert in.json out.json   (build host: hash a wifi.json for
                                              the image; no clear passwords)
"""
import grp
import hashlib
import json
import os
import re
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time

CONF = "/etc/panda/wifi.json"
RUN = "/run/panda"
CONTROL_SOCK = RUN + "/wifi.sock"
CONTROL_GROUP = "panda-web"
WPA_DIR = "/run/wpa_supplicant"
WPA_CONF = RUN + "/wpa_supplicant.conf"
PENDING_TIMEOUT = 180     # s: how long Connect keeps trying a new network before giving up
RETRY_PENDING = 10        # s: idle time before nudging wpa_supplicant to retry (new network)
RETRY_SAVED = 30          # s: same, for saved networks while disconnected


def log(*a):
    print("panda-wifi:", *a, flush=True)


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def shell_conf(path):
    out = {}
    for line in read(path).splitlines():
        m = re.match(r'^([A-Z_][A-Z0-9_]*)=(.*)$', line)
        if m:
            v = m.group(2).strip()
            q = re.match(r'^(["\'])(.*?)\1', v)
            v = q.group(2) if q else re.split(r'\s+#', v, maxsplit=1)[0].strip()
            out[m.group(1)] = v
    return out


# --- saved networks --------------------------------------------------------------------
def check_ssid(ssid):
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32:
        raise ValueError("network names are 1-32 bytes")
    return ssid


def check_password(pw):
    """WPA passphrase: 8-63 printable ASCII, or a 64-hex-digit PSK."""
    if not isinstance(pw, str):
        raise ValueError("bad password")
    if re.fullmatch(r"[0-9a-fA-F]{64}", pw):
        return pw
    if not (8 <= len(pw) <= 63 and all(32 <= ord(c) <= 126 for c in pw)):
        raise ValueError("Wi-Fi passwords are 8-63 characters (letters, digits, symbols)")
    return pw


def psk_hash(ssid, pw):
    if re.fullmatch(r"[0-9a-fA-F]{64}", pw):
        return pw.lower()
    return hashlib.pbkdf2_hmac("sha1", pw.encode(), ssid.encode(), 4096, 32).hex()


def make_entry(ssid, password=None, hidden=False, sae=False):
    """A saved network. password None = open network."""
    e = {"ssid": check_ssid(ssid), "hidden": bool(hidden), "added": int(time.time())}
    if password:
        password = check_password(password)
        e["psk"] = psk_hash(ssid, password)
        if sae:
            if len(password) == 64 and re.fullmatch(r"[0-9a-fA-F]{64}", password):
                raise ValueError("WPA3 networks need the password, not a PSK")
            e["sae_password"] = password
    return e


def parse_import(obj):
    """wifi.json (boot partition / build) -> (country, [entries])."""
    if not isinstance(obj, dict):
        raise ValueError("wifi.json must be a JSON object")
    nets = obj.get("networks")
    if nets is None and "ssid" in obj:
        nets = [obj]
    if not isinstance(nets, list):
        raise ValueError("wifi.json needs \"networks\": [...] or \"ssid\"")
    country = str(obj.get("country", "") or "").upper()
    if country and not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("country must be a 2-letter code")
    out = []
    for n in nets:
        if not isinstance(n, dict):
            raise ValueError("each network must be an object")
        if "psk" in n and not n.get("password"):       # already converted
            e = {"ssid": check_ssid(n["ssid"]), "hidden": bool(n.get("hidden")),
                 "psk": check_password(n["psk"]).lower(), "added": int(time.time())}
            if n.get("sae_password"):
                e["sae_password"] = check_password(n["sae_password"])
            out.append(e)
        else:
            out.append(make_entry(n.get("ssid"), n.get("password") or None,
                                  n.get("hidden"), n.get("wpa3")))
    return country, out


def load_conf():
    try:
        with open(CONF) as f:
            c = json.load(f)
        if isinstance(c, dict):
            c.setdefault("networks", [])
            c.setdefault("country", "")
            c.setdefault("adapter", "auto")
            return c
    except (OSError, ValueError):
        pass
    return {"country": "", "adapter": "auto", "networks": []}


def write_conf(c, path=CONF):
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(c, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# --- wpa_supplicant ----------------------------------------------------------------------
def unescape(s):
    """wpa_supplicant's printf_encode() -> bytes -> str."""
    out, i = bytearray(), 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n == "x" and i + 4 <= len(s):
                try:
                    out.append(int(s[i + 2:i + 4], 16))
                    i += 4
                    continue
                except ValueError:
                    pass
            out += {"n": b"\n", "r": b"\r", "t": b"\t", "e": b"\x1b"}.get(n, n.encode())
            i += 2
            continue
        out += c.encode()
        i += 1
    return out.decode("utf-8", errors="replace")


def security(flags):
    if "EAP" in flags:
        return "enterprise"
    if "WEP" in flags:
        return "wep"
    sae, psk = "SAE" in flags, "PSK" in flags
    if sae and psk:
        return "wpa2/wpa3"
    if sae:
        return "wpa3"
    if psk:
        return "wpa2" if ("WPA2" in flags or "RSN" in flags) else "wpa"
    if "OWE" in flags:
        return "owe"
    return "open"


def wpa_conf_text(conf):
    lines = [f"ctrl_interface=DIR={WPA_DIR} GROUP=0", "update_config=0", "ap_scan=1",
             "sae_pwe=2"]
    if conf.get("country"):
        lines.append(f"country={conf['country']}")
    n = len(conf["networks"])
    for i, e in enumerate(conf["networks"]):
        lines.append("network={")
        lines.append(f"\tssid={e['ssid'].encode().hex()}")
        if e.get("hidden"):
            lines.append("\tscan_ssid=1")
        if e.get("sae_password"):
            lines += ["\tkey_mgmt=SAE WPA-PSK", f"\tpsk={e['psk']}",
                      f"\tsae_password=\"{e['sae_password']}\"", "\tieee80211w=1"]
        elif e.get("psk"):
            lines += ["\tkey_mgmt=WPA-PSK WPA-PSK-SHA256", f"\tpsk={e['psk']}", "\tieee80211w=1"]
        else:
            lines.append("\tkey_mgmt=NONE")
        lines.append(f"\tpriority={n - i}")        # list order: most recently added first
        lines.append("}")
    return "\n".join(lines) + "\n"


class WpaCtrl:
    """One wpa_supplicant control socket connection (datagram)."""
    _n = 0

    def __init__(self, ifname):
        WpaCtrl._n += 1
        self.local = f"{RUN}/wpa-ctrl-{os.getpid()}-{WpaCtrl._n}"
        try:
            os.unlink(self.local)
        except FileNotFoundError:
            pass
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.s.bind(self.local)
        self.s.connect(f"{WPA_DIR}/{ifname}")
        self.s.settimeout(5)
        self.lock = threading.Lock()

    def request(self, cmd, timeout=5):
        with self.lock:
            self.s.settimeout(timeout)
            self.s.send(cmd.encode())
            while True:
                data = self.s.recv(65536).decode(errors="replace")
                if not data.startswith("<"):         # skip unsolicited events
                    return data

    def recv_event(self, timeout):
        self.s.settimeout(timeout)
        return self.s.recv(65536).decode(errors="replace")

    def close(self):
        try:
            self.s.close()
        finally:
            try:
                os.unlink(self.local)
            except FileNotFoundError:
                pass


def kv(text):
    return dict(l.split("=", 1) for l in text.splitlines() if "=" in l)


# --- adapters ----------------------------------------------------------------------------
def adapters(board):
    out = []
    try:
        names = sorted(os.listdir("/sys/class/net"))
    except OSError:
        names = []
    for name in names:
        base = f"/sys/class/net/{name}"
        if not os.path.exists(base + "/phy80211"):
            continue
        dev = os.path.realpath(base + "/device")
        usb = "/usb" in dev
        driver = os.path.basename(os.path.realpath(base + "/device/driver")) if \
            os.path.exists(base + "/device/driver") else ""
        if usb:
            parent = os.path.dirname(dev)            # the USB device (above the interface)
            prod = read(parent + "/product").strip()
            manu = read(parent + "/manufacturer").strip()
            ids = f"{read(parent + '/idVendor').strip()}:{read(parent + '/idProduct').strip()}"
            desc = " ".join(x for x in (manu, prod) if x) or f"USB Wi-Fi adapter {ids}"
            desc = f"{desc} (USB {ids})"
        else:
            desc = board.get("WIFI_ONBOARD_DESCRIPTION") or f"Onboard Wi-Fi ({driver})"
        phy = read(base + "/phy80211/name").strip()
        out.append({"name": name, "kind": "usb" if usb else "onboard", "description": desc,
                    "driver": driver, "phy": phy, "mac": read(base + "/address").strip()})
    return out


_bands = {}


def bands(phy):
    if phy not in _bands:
        try:
            info = subprocess.run(["iw", "phy", phy, "info"], capture_output=True, text=True,
                                  timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            info = ""
        b = [{"1": "2.4 GHz", "2": "5 GHz", "4": "6 GHz"}.get(m, m)
             for m in re.findall(r"^\s*Band (\d+):", info, re.M)]
        if b:
            _bands[phy] = b
        return b
    return _bands[phy]


def stable_mac(src_if):
    """A locally administered MAC derived from another interface's MAC."""
    m = read(f"/sys/class/net/{src_if}/address").strip()
    if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", m) or m == "00:00:00:00:00:00":
        return None
    b = [int(x, 16) for x in m.split(":")]
    b[0] = (b[0] | 0x02) & 0xFE
    b[5] ^= 0x01
    return ":".join(f"{x:02x}" for x in b)


def sh(*cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def unblock(phy):
    for r in os.listdir("/sys/class/rfkill") if os.path.isdir("/sys/class/rfkill") else []:
        dev = os.path.realpath(f"/sys/class/rfkill/{r}/device")
        if os.path.basename(dev) == phy:
            try:
                with open(f"/sys/class/rfkill/{r}/soft", "w") as f:
                    f.write("0")
            except OSError:
                pass


# --- the daemon ----------------------------------------------------------------------------
class Wifi:
    def __init__(self):
        self.board = shell_conf("/etc/panda/board.conf")
        self.app = shell_conf("/etc/panda/appliance.conf")
        self.conf = load_conf()
        self.lock = threading.RLock()
        self.save_lock = threading.Lock()
        self.iface = None            # active interface name
        self.wpa = None              # wpa_supplicant Popen
        self.ctrl = None             # WpaCtrl for requests
        self.dhcp = None             # udhcpc Popen
        self.dhcp_ssid = None
        self.results = []            # latest scan results
        self.results_at = 0.0
        self.scan_done = threading.Event()
        self.pending = None          # {"ssid", "entry", "until", ...} while trying a new network
        self.nudged = 0.0            # last time we told wpa_supplicant to try again
        self.last_error = ""
        self.gen = 0                 # bumps when the active adapter changes

    # --- persistence -----------------------------------------------------------------
    def save(self, why):
        with self.lock:
            write_conf(self.conf)
        with self.save_lock:
            log("saving with lbu:", why)
            if not sh("lbu", "commit", timeout=120):
                log("ERROR: lbu commit failed")

    def import_boot_media(self):
        media = "/media/" + self.board.get("BOOT_MEDIA", "mmcblk0p1")
        path = media + "/wifi.json"
        if not os.path.isfile(path):
            return
        try:
            with open(path) as f:
                country, nets = parse_import(json.load(f))
        except (OSError, ValueError) as e:
            log(f"ignoring {path}: {e}")
            return
        with self.lock:
            names = {e["ssid"] for e in nets}
            self.conf["networks"] = nets + [e for e in self.conf["networks"] if e["ssid"] not in names]
            if country:
                self.conf["country"] = country
        log(f"imported {len(nets)} network(s) from {path}")
        self.save("imported wifi.json from the boot partition")
        # Don't leave the password on the card.
        if sh("mount", "-o", "remount,rw", media):
            try:
                os.unlink(path)
            except OSError as e:
                log("couldn't delete", path, e)
            os.sync()
            sh("mount", "-o", "remount,ro", media)

    # --- adapter selection / process supervision ----------------------------------------
    def choose(self):
        want = self.conf.get("adapter", "auto")
        ads = adapters(self.board)
        if want == "off" or not ads:
            return None, ads
        by = {a["name"]: a for a in ads}
        if want in by:
            return by[want], ads
        if want in ("usb", "onboard"):
            pick = [a for a in ads if a["kind"] == want]
            if pick:
                return pick[0], ads
        usb = [a for a in ads if a["kind"] == "usb"]
        return (usb or ads)[0], ads

    def stop_iface(self):
        with self.lock:
            if self.ctrl:
                self.ctrl.close()
                self.ctrl = None
            for p in (self.dhcp, self.wpa):
                if p and p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(10)
                    except subprocess.TimeoutExpired:
                        p.kill()
            if self.iface:
                log("stopped on", self.iface)
                sh("ip", "addr", "flush", "dev", self.iface)
                if not self.keep_up(self.iface):
                    sh("ip", "link", "set", self.iface, "down")
            self.dhcp = self.wpa = None
            self.dhcp_ssid = None
            self.iface = None
            self.results = []
            self.gen += 1

    def keep_up(self, name):
        """Onboard interface of a combo chip whose Bluetooth dies when Wi-Fi powers
        up or down (board.conf BT_DRIVER_AFTER_WIFI): never take it down."""
        if not self.board.get("BT_DRIVER_AFTER_WIFI"):
            return False
        return any(a["name"] == name and a["kind"] == "onboard" for a in adapters(self.board))

    def start_iface(self, ad):
        name = ad["name"]
        unblock(ad["phy"])
        src = self.board.get("WIFI_MAC_FROM", "")
        # A shared-chip onboard interface is never taken down (that would kill
        # Bluetooth); panda-radio-init already set its MAC before bringing it up.
        if ad["kind"] == "onboard" and src and not self.keep_up(name):
            mac = stable_mac(src)
            if mac and ad["mac"] != mac:
                sh("ip", "link", "set", name, "down")
                if sh("ip", "link", "set", name, "address", mac):
                    log(f"{name}: MAC {mac} (derived from {src})")
        sh("ip", "link", "set", name, "up")
        if self.conf.get("country"):
            sh("iw", "reg", "set", self.conf["country"])
        os.makedirs(RUN, exist_ok=True)
        self.write_wpa_conf()
        try:
            os.unlink(f"{WPA_DIR}/{name}")
        except FileNotFoundError:
            pass
        self.wpa = subprocess.Popen(["wpa_supplicant", "-i", name, "-D", "nl80211,wext",
                                     "-c", WPA_CONF, "-s"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        for _ in range(50):
            if os.path.exists(f"{WPA_DIR}/{name}"):
                break
            time.sleep(0.1)
        try:
            self.ctrl = WpaCtrl(name)
        except OSError as e:
            log(f"{name}: no wpa_supplicant control socket ({e})")
            self.wpa.terminate()
            self.wpa = None
            return False
        self.iface = name
        self.gen += 1
        log(f"started on {name}: {ad['description']}")
        threading.Thread(target=self.events, args=(name, self.gen), daemon=True).start()
        return True

    def supervise(self):
        while True:
            try:
                ad, _ = self.choose()
                with self.lock:
                    alive = self.wpa is not None and self.wpa.poll() is None
                    if self.iface and (ad is None or ad["name"] != self.iface or not alive):
                        if not alive:
                            log("wpa_supplicant exited; restarting")
                        self.stop_iface()
                    if ad and not self.iface:
                        self.start_iface(ad)
                    if self.dhcp is not None and self.dhcp.poll() is not None:
                        self.dhcp = None
                    p = self.pending
                    if p and time.monotonic() > p["until"]:
                        self.fail_pending("couldn't connect (no answer, or out of range)")
                    elif self.iface:
                        self.nudge()
            except Exception as e:      # never die: this is the only way in without Ethernet
                log("supervisor error:", e)
            time.sleep(3)

    # --- wpa_supplicant events ---------------------------------------------------------
    def events(self, ifname, gen):
        try:
            ev = WpaCtrl(ifname)
        except OSError as e:
            log("event socket:", e)
            return
        try:
            if ev.request("ATTACH").strip() != "OK":
                log("ATTACH failed")
                return
            while self.gen == gen:
                try:
                    msg = ev.recv_event(2)
                except socket.timeout:
                    continue
                except OSError:
                    break
                msg = re.sub(r"^<\d>", "", msg)
                self.on_event(ifname, msg)
        finally:
            ev.close()

    def on_event(self, ifname, msg):
        if msg.startswith("CTRL-EVENT-SCAN-RESULTS"):
            self.fetch_results()
            self.scan_done.set()
        elif msg.startswith("CTRL-EVENT-CONNECTED"):
            st = self.wpa_status()
            ssid = st.get("ssid", "")
            log(f"{ifname}: connected to {ssid!r} ({st.get('freq', '?')} MHz)")
            self.last_error = ""
            self.start_dhcp(ifname, ssid)
            p = self.pending
            if p and p["ssid"] == ssid:
                self.pending = None
                with self.lock:
                    others = [e for e in self.conf["networks"] if e["ssid"] != ssid]
                    self.conf["networks"] = [p["entry"]] + others
                # Keep the association: no RECONFIGURE, just re-enable the others
                # (SELECT_NETWORK disabled them) and write the file for next time.
                self.write_wpa_conf()
                self.wpa_req("ENABLE_NETWORK all")
                threading.Thread(target=self.save, args=(f"Wi-Fi network {ssid!r}",),
                                 daemon=True).start()
        elif msg.startswith("CTRL-EVENT-SSID-TEMP-DISABLED"):
            m = re.search(r'ssid="(.*?)" auth_failures=\d+ duration=\d+ reason=(\S+)', msg)
            reason = m.group(2) if m else ""
            ssid = unescape(m.group(1)) if m else ""
            if reason == "WRONG_KEY":
                if self.pending:
                    self.fail_pending("wrong password", ssid)
                else:
                    self.last_error = f"wrong password for {ssid}"
            elif reason:
                log(f"{ifname}: {ssid!r} temporarily disabled ({reason})")
        elif msg.startswith("CTRL-EVENT-DISCONNECTED"):
            m = re.search(r"reason=(\d+)", msg)
            log(f"{ifname}: disconnected (reason {m.group(1) if m else '?'})")
        elif msg.startswith("CTRL-EVENT-NETWORK-NOT-FOUND") and self.pending:
            pass   # keeps scanning until the pending timeout

    def nudge(self):
        """Keep trying. After an auth/assoc timeout wpa_supplicant ignores the BSSID and
        can sit in SCANNING for a long time without another attempt; on a weak link
        (the WL1271 shares its antenna with Bluetooth) a later try often gets through."""
        p = self.pending
        every = RETRY_PENDING if p else RETRY_SAVED
        now = time.monotonic()
        if now - self.nudged < every or not (p or self.conf["networks"]):
            return
        st = self.wpa_status().get("wpa_state", "")
        if st not in ("SCANNING", "DISCONNECTED", "INACTIVE"):
            return                   # connected, or an attempt is under way
        self.nudged = now
        if p:
            p["tries"] = p.get("tries", 0) + 1
        self.wpa_req("BSSID_IGNORE clear")
        self.wpa_req("REASSOCIATE")

    def fail_pending(self, why, ssid=None):
        p = self.pending
        if not p or (ssid and ssid != p["ssid"]):
            return
        log(f"connecting to {p['ssid']!r} failed: {why}")
        self.pending = None
        self.last_error = f"{p['ssid']}: {why}"
        self.reload()

    def start_dhcp(self, ifname, ssid):
        with self.lock:
            if self.iface != ifname:
                return
            if self.dhcp and self.dhcp.poll() is None:
                if ssid == self.dhcp_ssid:
                    self.dhcp.send_signal(signal.SIGUSR1)      # same network: renew
                    return
                self.dhcp.terminate()                          # another network: start over
                try:
                    self.dhcp.wait(10)
                except subprocess.TimeoutExpired:
                    self.dhcp.kill()
            host = socket.gethostname()
            self.dhcp = subprocess.Popen(["udhcpc", "-i", ifname, "-f", "-S", "-R",
                                          "-x", f"hostname:{host}"],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
            self.dhcp_ssid = ssid

    # --- queries -------------------------------------------------------------------------
    def wpa_req(self, cmd, timeout=5):
        c = self.ctrl
        if c is None:
            return ""
        try:
            return c.request(cmd, timeout)
        except OSError:
            return ""

    def wpa_status(self):
        st = kv(self.wpa_req("STATUS"))
        if "ssid" in st:
            st["ssid"] = unescape(st["ssid"])
        return st

    def fetch_results(self):
        raw = self.wpa_req("SCAN_RESULTS")
        saved = {e["ssid"] for e in self.conf["networks"]}
        best = {}
        for line in raw.splitlines()[1:]:
            f = line.split("\t")
            if len(f) < 5:
                continue
            bssid, freq, sig, flags, ssid = f[0], int(f[1] or 0), int(f[2] or -100), f[3], unescape(f[4])
            if not ssid or "\x00" in ssid:
                continue
            cur = best.get(ssid)
            band = "5 GHz" if 4900 < freq < 5900 else "6 GHz" if freq > 5900 else "2.4 GHz"
            if cur:
                if band not in cur["bands"]:
                    cur["bands"].append(band)
                if sig <= cur["signal"]:
                    continue
            best[ssid] = {"ssid": ssid, "bssid": bssid, "freq": freq, "signal": sig,
                          "security": security(flags), "saved": ssid in saved,
                          "bands": sorted(set((cur or {}).get("bands", []) + [band]))}
        self.results = sorted(best.values(), key=lambda r: -r["signal"])[:60]
        self.results_at = time.time()

    def net_id(self, ssid):
        for l in self.wpa_req("LIST_NETWORKS").splitlines()[1:]:
            f = l.split("\t")
            if len(f) > 1 and unescape(f[1]) == ssid:
                return f[0]
        return None

    def reload(self):
        self.write_wpa_conf()
        self.wpa_req("RECONFIGURE", 10)

    def write_wpa_conf(self):
        with self.lock:
            c = dict(self.conf)
            nets = list(c["networks"])
            p = self.pending
            if p:
                nets = [p["entry"]] + [e for e in nets if e["ssid"] != p["ssid"]]
            c["networks"] = nets
        fd = os.open(WPA_CONF + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(wpa_conf_text(c))
        os.replace(WPA_CONF + ".new", WPA_CONF)

    def status(self):
        ad, ads = self.choose()
        pend = self.pending
        onboard_bands = self.board.get("WIFI_ONBOARD_BANDS", "")
        for a in ads:
            if a["kind"] == "onboard" and onboard_bands:
                a["bands"] = [b.strip() for b in onboard_bands.split(",") if b.strip()]
            else:
                a["bands"] = bands(a["phy"])
            a["active"] = a["name"] == self.iface
        out = {"adapters": ads, "adapter_setting": self.conf.get("adapter", "auto"),
               "country": self.conf.get("country", ""),
               "saved": [{"ssid": e["ssid"], "security": "wpa3" if e.get("sae_password")
                          else "wpa2" if e.get("psk") else "open", "hidden": bool(e.get("hidden"))}
                         for e in self.conf["networks"]],
               "iface": self.iface, "state": "off" if ad is None else "starting",
               "pending": pend["ssid"] if pend else None,
               "pending_tries": pend.get("tries", 0) if pend else 0,
               "error": self.last_error}
        if self.iface and self.ctrl:
            st = self.wpa_status()
            out["state"] = st.get("wpa_state", "unknown").lower()
            if st.get("wpa_state") == "COMPLETED":
                sig = kv(self.wpa_req("SIGNAL_POLL"))
                out["connection"] = {
                    "ssid": st.get("ssid", ""), "bssid": st.get("bssid", ""),
                    "freq": int(st.get("freq", 0) or 0), "security": st.get("key_mgmt", ""),
                    "signal": int(sig["RSSI"]) if sig.get("RSSI", "").lstrip("-").isdigit() else None,
                    "speed": int(sig["LINKSPEED"]) if sig.get("LINKSPEED", "").isdigit() else None,
                    "ip": st.get("ip_address", ""), "mac": st.get("address", "")}
        return out

    # --- commands ------------------------------------------------------------------------
    def scan(self):
        if not self.iface:
            raise ValueError("no Wi-Fi adapter")
        self.scan_done.clear()
        r = self.wpa_req("SCAN").strip()
        if r not in ("OK", "FAIL-BUSY"):
            raise ValueError(f"scan failed ({r or 'no answer'})")
        self.scan_done.wait(12)
        self.fetch_results()
        return {"networks": self.results, "at": self.results_at}

    def connect(self, req):
        if not self.iface:
            raise ValueError("no Wi-Fi adapter")
        ssid = check_ssid(req.get("ssid"))
        hidden = bool(req.get("hidden"))
        sec = next((r["security"] for r in self.results if r["ssid"] == ssid), None)
        if sec in ("enterprise", "wep"):
            raise ValueError({"enterprise": "WPA-Enterprise (802.1X) networks aren't supported",
                              "wep": "WEP networks aren't supported (insecure)"}[sec])
        pw = req.get("password") or None
        if pw is None and sec not in (None, "open", "owe"):
            saved = next((e for e in self.conf["networks"] if e["ssid"] == ssid), None)
            if not saved:
                raise ValueError("this network needs a password")
            entry = dict(saved)                     # reconnect with what we have
        else:
            entry = make_entry(ssid, pw, hidden, sae=(sec == "wpa3"))
        log(f"connecting to {ssid!r} ({sec or 'not in the scan'})")
        self.last_error = ""
        self.pending = {"ssid": ssid, "entry": entry, "until": time.monotonic() + PENDING_TIMEOUT}
        self.nudged = time.monotonic()
        self.reload()
        nid = self.net_id(ssid)
        if nid is not None:
            self.wpa_req(f"SELECT_NETWORK {nid}")
        return {"ok": True, "pending": ssid}

    def forget(self, req):
        ssid = check_ssid(req.get("ssid"))
        with self.lock:
            before = len(self.conf["networks"])
            self.conf["networks"] = [e for e in self.conf["networks"] if e["ssid"] != ssid]
            if len(self.conf["networks"]) == before:
                raise ValueError("not a saved network")
        log(f"forgetting {ssid!r}")
        self.write_wpa_conf()
        nid = self.net_id(ssid)
        if nid is not None:
            self.wpa_req(f"REMOVE_NETWORK {nid}")
        threading.Thread(target=self.save, args=(f"forgot Wi-Fi network {ssid!r}",),
                         daemon=True).start()
        return {"ok": True}

    def settings(self, req):
        with self.lock:
            if "country" in req:
                c = str(req.get("country") or "").upper()
                if c and not re.fullmatch(r"[A-Z]{2}", c):
                    raise ValueError("country must be a 2-letter code")
                self.conf["country"] = c
                if c:
                    sh("iw", "reg", "set", c)
                    self.wpa_req(f"SET country {c}")
            if "adapter" in req:
                a = str(req.get("adapter"))
                if a not in ("auto", "usb", "onboard", "off") and \
                        not any(x["name"] == a for x in adapters(self.board)):
                    raise ValueError("unknown adapter")
                self.conf["adapter"] = a          # the supervisor switches
        self.write_wpa_conf()
        threading.Thread(target=self.save, args=("Wi-Fi settings",), daemon=True).start()
        return {"ok": True}

    def control(self, req):
        cmd = req.get("cmd")
        if cmd == "status":
            return self.status()
        if cmd == "scan":
            return self.scan()
        if cmd == "results":
            return {"networks": self.results, "at": self.results_at}
        if cmd == "connect":
            return self.connect(req)
        if cmd == "forget":
            return self.forget(req)
        if cmd == "settings":
            return self.settings(req)
        raise ValueError(f"unknown command {cmd!r}")

    def serve(self):
        wifi = self

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(20)
                try:
                    line = self.rfile.readline(65536)
                    try:
                        reply = wifi.control(json.loads(line))
                    except (ValueError, TypeError, KeyError) as e:
                        reply = {"error": str(e)}
                    self.wfile.write(json.dumps(reply).encode() + b"\n")
                except OSError:
                    pass

        class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        os.makedirs(RUN, exist_ok=True)
        try:
            os.unlink(CONTROL_SOCK)
        except FileNotFoundError:
            pass
        srv = S(CONTROL_SOCK, H)
        try:
            os.chown(CONTROL_SOCK, 0, grp.getgrnam(CONTROL_GROUP).gr_gid)
            os.chmod(CONTROL_SOCK, 0o660)
        except KeyError:
            os.chmod(CONTROL_SOCK, 0o600)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    def run(self):
        os.makedirs(RUN, exist_ok=True)
        self.import_boot_media()
        self.serve()
        signal.signal(signal.SIGTERM, lambda *a: (self.stop_iface(), sys.exit(0)))
        log("running; saved networks:", len(self.conf["networks"]))
        self.supervise()


def convert(src, dst):
    """Build host: wifi.json with passwords -> saved-network file (hashed PSKs)."""
    with open(src) as f:
        country, nets = parse_import(json.load(f))
    write_conf({"country": country, "adapter": "auto", "networks": nets}, dst)
    print(f"{dst}: {len(nets)} network(s)" + (f", country {country}" if country else ""))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--convert":
        try:
            convert(sys.argv[2], sys.argv[3])
        except (OSError, ValueError) as e:
            sys.exit(f"wifi.json: {e}")
    else:
        Wifi().run()
