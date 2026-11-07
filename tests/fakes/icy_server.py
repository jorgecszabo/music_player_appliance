"""A fake internet-radio stream server: an endless 48 kHz stereo sine as a streamed WAV,
with ICY metadata, and the failure modes a real station has.

    /stream            normal (ICY metadata if the client asks for it)
    /slow              headers, then nothing (a stalled connect)
    /drop              1 second of audio, then the connection closes
    /404               not found
    /redirect          302 to /stream
    /loop              302 to itself (a redirect loop)
    /garbage           200 with bytes that are not audio

Titles: server.title = "Artist - Song" changes what the next metadata block says.
"""
import math
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RATE = 48000
CHUNK_S = 0.1
METAINT = 16000
BURST_S = 10


def wav_header():
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 2, RATE, RATE * 4, 4, 16)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


def pcm(n, phase):
    out = bytearray()
    for i in range(n):
        v = int(3000 * math.sin((phase + i) / 20))
        out += struct.pack("<hh", v, v)
    return bytes(out), phase + n


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        srv.hits.append(self.path)
        path = self.path.split("?")[0]
        if path == "/404":
            self.send_error(404)
        elif path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/stream")
            self.end_headers()
        elif path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.end_headers()
        elif path == "/garbage":
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.end_headers()
            self.wfile.write(b"this is not audio\n" * 1000)
        elif path == "/slow":
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.end_headers()
            self.wfile.flush()
            time.sleep(60)
        elif path in ("/stream", "/drop"):
            self.stream(path == "/drop")
        else:
            self.send_error(404)

    def stream(self, drop):
        srv = self.server
        icy = self.headers.get("Icy-MetaData") == "1"
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("icy-name", "Fake Radio")
        if icy:
            self.send_header("icy-metaint", str(METAINT))
        self.end_headers()
        header = wav_header()
        self.wfile.write(header)
        phase = 0
        until_meta = METAINT - len(header)          # the header counts towards the first metadata interval
        # Like a real station: a burst of audio on connect (mpv probes the stream before it plays),
        # then real time. Without it mpv needs many seconds to start.
        started = time.monotonic() - BURST_S
        sent = 0
        try:
            while not srv.stopping.is_set():
                data, phase = pcm(int(RATE * CHUNK_S), phase)
                while data:
                    if icy and until_meta == 0:
                        meta = ("StreamTitle='%s';" % srv.title).encode()
                        blocks = (len(meta) + 15) // 16
                        self.wfile.write(bytes([blocks]) + meta.ljust(blocks * 16, b"\0"))
                        until_meta = METAINT
                    n = min(len(data), until_meta) if icy else len(data)
                    self.wfile.write(data[:n])
                    data = data[n:]
                    until_meta -= n if icy else 0
                sent += RATE * CHUNK_S
                # pace to real time
                ahead = sent / RATE - (time.monotonic() - started)
                if ahead > 0:
                    time.sleep(ahead)
                if drop and time.monotonic() - started > 1:
                    return
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return


class IcyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host="127.0.0.1", port=0):
        super().__init__((host, port), Handler)
        self.title = "Fake Artist - Fake Song"
        self.hits = []
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return "http://%s:%d" % self.server_address

    def close(self):
        self.stopping.set()
        self.shutdown()
        self.server_close()
