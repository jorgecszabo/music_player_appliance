"""FakePeer: a Unix-socket server with scripted behaviour, for the services' peers.

Each accepted connection is handled in its own thread by a behaviour function
`fn(conn, request)`, where `request` is the first line the client sent (bytes, without
the newline; b"" if the client sent nothing before the peer acted). Ready-made
behaviours cover the failure modes the callers must survive.
"""
import json
import os
import socket
import threading
import time


class FakePeer:
    def __init__(self, path, behaviour):
        self.path = str(path)
        self.behaviour = behaviour
        self.requests = []             # every request line received, in order
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if os.path.exists(self.path):
            os.unlink(self.path)
        self.sock.bind(self.path)
        self.sock.listen(16)
        self.sock.settimeout(0.1)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(5)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            line = buf.split(b"\n", 1)[0]
            self.requests.append(line)
            self.behaviour(conn, line)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


# --- behaviours ----------------------------------------------------------------------

def reply_json(obj):
    """Answer every request with one JSON line."""
    def fn(conn, line):
        conn.sendall(json.dumps(obj).encode() + b"\n")
    return fn


def reply_raw(data):
    def fn(conn, line):
        conn.sendall(data)
    return fn


def close_silently(conn, line):
    """Accept, read the request, close without a reply."""


def stall(seconds=30):
    """Read the request and never answer (a hung peer)."""
    def fn(conn, line):
        time.sleep(seconds)
    return fn


def partial_then_stall(data=b'{"ok": tr', seconds=30):
    def fn(conn, line):
        conn.sendall(data)
        time.sleep(seconds)
    return fn


def mpv_player(props, events=0, calls=None):
    """A fake mpv JSON IPC: get_property from `props` (missing -> property unavailable),
    anything else succeeds with null. `events` interleaved event lines before the reply."""
    def fn(conn, line):
        req = json.loads(line)
        cmd = req["command"]
        if calls is not None:
            calls.append(cmd)
        for _ in range(events):
            conn.sendall(b'{"event": "tick"}\n')
        if cmd[0] == "get_property":
            if cmd[1] in props:
                out = {"error": "success", "data": props[cmd[1]]}
            else:
                out = {"error": "property unavailable"}
        else:
            out = {"error": "success", "data": None}
        out["request_id"] = req["request_id"]
        conn.sendall(json.dumps(out).encode() + b"\n")
    return fn
