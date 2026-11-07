"""Small IPC helpers shared by the panda services (stdlib only).

Two peers are reached over Unix sockets, and every caller (panda-web,
panda-arbiter) must treat both the same way: short timeouts, and a peer that
is down, restarting or hung is an ordinary answer, never an exception that
escapes or a wait that piles up.

- line_call(): the line-JSON control sockets of panda-bt and panda-wifi (one
  JSON request per line, one JSON reply per line).
- mpv_raw() / mpv() / radio_probe(): the radio player's (mpv) JSON IPC.

Scripts in /etc/panda import this as `import panda_ipc` (the script's own
directory is first on sys.path).
"""
import json
import os
import socket

# PANDA_RUN_DIR / PANDA_SPEAKER_RUN_DIR are for the test suite; nothing sets them on the appliance.
_RUN = os.environ.get("PANDA_RUN_DIR", "/run/panda")
_SPEAKER_RUN = os.environ.get("PANDA_SPEAKER_RUN_DIR", "/run/speaker")
BT_SOCK = _RUN + "/bt.sock"
WIFI_SOCK = _RUN + "/wifi.sock"
MPV_SOCK = _SPEAKER_RUN + "/radio.sock"


def line_call(sock, req, timeout=5, name="service"):
    """One request on a line-JSON control socket; the reply dict.

    Never raises: if the peer can't be reached or answers garbage, the reply is
    {"error": "<name> unavailable (<reason>)"}, the same shape as the peer's own errors.
    """
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


def bt_call(req, timeout=5):
    return line_call(BT_SOCK, req, timeout, "panda-bt")


def wifi_call(req, timeout=5):
    return line_call(WIFI_SOCK, req, timeout, "panda-wifi")


def mpv_raw(*cmd, timeout=3):
    """One command over mpv's JSON IPC; mpv's reply ({"error": "success", "data": ...}).

    Raises OSError if the player can't be reached (socket missing: not running or
    restarting; refused: wrong permissions; timeout: hung; closed with no reply).
    An error mpv itself reports (e.g. "property unavailable") is a normal reply.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(MPV_SOCK)
        s.sendall(json.dumps({"command": list(cmd), "request_id": 1}).encode() + b"\n")
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                raise ConnectionResetError("mpv closed the socket")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("request_id") == 1:
                    return msg


def mpv(*cmd, timeout=3):
    """mpv's "data" for one command; None if it failed or the player is unreachable."""
    try:
        msg = mpv_raw(*cmd, timeout=timeout)
    except OSError:
        return None
    return msg.get("data") if msg.get("error") == "success" else None


def radio_probe(timeout=1):
    """What the player is doing, or None if it can't be reached.

    {"idle": True} when nothing is loaded, else {"idle": False, "metadata", "audio-params",
    "pause", "audio-codec-name", "audio-bitrate", "paused-for-cache"}. Within one probe, a
    player that stops answering midway isn't asked again (the properties asked so far are
    kept), so a hung mpv costs about two timeouts, not one per property.
    """
    try:
        idle = mpv_raw("get_property", "idle-active", timeout=timeout)
    except OSError:
        return None
    if idle.get("error") != "success":
        return None
    if idle.get("data"):
        return {"idle": True}
    out = {"idle": False}
    for prop in ("metadata", "audio-params", "pause", "audio-codec-name", "audio-bitrate",
                 "paused-for-cache"):
        try:
            msg = mpv_raw("get_property", prop, timeout=timeout)
        except OSError:
            break
        out[prop] = msg.get("data") if msg.get("error") == "success" else None
    return out
