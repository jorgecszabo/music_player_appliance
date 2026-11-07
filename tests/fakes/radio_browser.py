"""A fake radio-browser.info API (the few endpoints panda-web uses), plain HTTP."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        srv.requests.append(self.path)
        if srv.fail:
            self.send_error(503)
            return
        path = self.path.split("?")[0]
        if path.startswith("/json/stations/byuuid/"):
            uuid = path.rsplit("/", 1)[1]
            body = [s for s in srv.stations if s["stationuuid"] == uuid]
        elif path.startswith("/json/url/"):
            uuid = path.rsplit("/", 1)[1]
            st = next((s for s in srv.stations if s["stationuuid"] == uuid), None)
            body = {"ok": bool(st), "url": (st or {}).get("url_resolved", "")}
        elif path.startswith("/json/stations/search"):
            body = srv.stations
        elif path.startswith("/json/tags"):
            body = [{"name": "jazz", "stationcount": 3}]
        elif path.startswith("/json/countries"):
            body = [{"name": "Germany", "iso_3166_1": "DE", "stationcount": 3}]
        else:
            self.send_error(404)
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class RadioBrowser(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, stations):
        super().__init__(("127.0.0.1", 0), Handler)
        self.stations = stations
        self.requests = []
        self.fail = False
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def base(self):
        return "http://%s:%d" % self.server_address

    def close(self):
        self.shutdown()
        self.server_close()
