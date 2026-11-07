"""panda-web: GET endpoints, the Host check, static files, OTA upload plumbing."""
import http.client
import json
import stat
import threading
import time
from http.server import ThreadingHTTPServer

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.conftest import load_module


@pytest.fixture(scope="module")
def web():
    return load_module("panda-web.py")


@pytest.fixture
def srv(web, monkeypatch, tmp_path):
    monkeypatch.setattr(web, "run", lambda cmd, *a, **k: "" if cmd[0] == "ip" else "log text\n")
    monkeypatch.setattr(web.COLLECTOR, "refresh", lambda *a: None)
    monkeypatch.setattr(web.COLLECTOR, "touch", lambda: None)
    monkeypatch.setattr(web.COLLECTOR, "status", {"time": 1, "radio": {}})
    monkeypatch.setattr(web.COLLECTOR, "wait", lambda seq, timeout: (1, {"time": 1}))
    monkeypatch.setattr(web, "rb_get", lambda path, params=None, **k: [{"name": "S", "stationuuid": "u", "tags": "t"}]
                        if "search" in path else [{"name": "jazz", "stationcount": 3}] if "tags" in path
                        else [{"iso_3166_1": "DE", "name": "Germany", "stationcount": 2}])
    web.Hosts.names, web.Hosts.at = set(), 0.0
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    for n in ("index.html", "style.css", "app.js"):
        (web_dir / n).write_text(f"<!-- {n} -->")
    monkeypatch.setattr(web, "WEB_DIR", str(web_dir))
    s = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    s.daemon_threads = True
    threading.Thread(target=s.serve_forever, daemon=True).start()
    s.port = s.server_address[1]
    yield s
    s.shutdown()
    s.server_close()


def get(srv, path, host="127.0.0.1", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    try:
        c.putrequest("GET", path, skip_host=True)
        if host is not None:
            c.putheader("Host", host)
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        return r.status, dict(r.getheaders()), body
    finally:
        c.close()


# --- the Host check (DNS rebinding) ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.0.0.1:8080", "[::1]", "[::1]:8080", "LOCALHOST:80"])
def test_local_host_names_are_accepted(srv, host):
    assert get(srv, "/api/status", host=host)[0] == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8080", "127.0.0.1.evil.example", "", None, "localhost.evil.com",
                                  "[::1", "0.0.0.0", "a" * 5000, "localhost\x00.evil"])
def test_other_host_names_are_refused(srv, host):
    try:
        code = get(srv, "/api/status", host=host)[0]
    except (http.client.HTTPException, ConnectionError, ValueError):
        return                                                        # refused at the HTTP layer: fine
    assert code in (400, 421), host


# --- static pages ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path,ctype", [("/", "text/html"), ("/style.css", "text/css"), ("/app.js", "text/javascript")])
def test_static_files(srv, path, ctype):
    code, headers, body = get(srv, path)
    assert code == 200 and headers["Content-Type"].startswith(ctype) and body.startswith(b"<!--")


@pytest.mark.parametrize("path", ["/../etc/passwd", "/%2e%2e/etc/passwd", "/index.html", "//etc/passwd", "/app.js/../../x",
                                  "/.git/config", "/api/../etc/passwd"])
def test_no_other_files_are_served(srv, path):
    code, _, body = get(srv, path)
    assert code in (404, 400) and b"root:" not in body


# --- GET /api -----------------------------------------------------------------------------------------------------

def test_logs_only_known_names(srv, web):
    assert get(srv, "/api/logs?name=radio")[0] == 200
    for bad in ("", "nope", "../../etc/passwd", "radio;id", "radio%0Amessages", "RADIO", "radio%00"):
        assert get(srv, f"/api/logs?name={bad}")[0] == 400, bad
    assert get(srv, "/api/logs")[0] == 400
    assert get(srv, "/api/logs?name=radio&name=messages")[0] == 200       # only the first counts


@pytest.mark.parametrize("query", ["", "q=jazz", "q=%00", "q=" + "a" * 500, "tag=Jazz&country=de", "country=D", "country=%C3%A9%C3%A9",
                                   "order=bogus", "order=name", "page=3", "page=-1", "page=abc", "page=999999999999",
                                   "page=%C2%B2", "page=%E2%91%A0", "page=%D9%A3", "q=a&q=b", "%ff=1", "page=1e3"])
def test_radio_search_never_fails_on_odd_queries(srv, query):
    code, _, body = get(srv, "/api/radio/search?" + query)
    assert code == 200, (query, code, body)
    assert json.loads(body)["page"] >= 0


@pytest.mark.parametrize("path", ["/api/radio/tags", "/api/radio/tags?q=", "/api/radio/tags?q=j", "/api/radio/tags?q=jazz",
                                  "/api/radio/tags?q=%2F..%2F", "/api/radio/tags?q=%00%00", "/api/radio/countries",
                                  "/api/radio/favorites", "/api/nope", "/api"])
def test_other_get_endpoints_answer(srv, path):
    code, _, body = get(srv, path)
    assert code in (200, 404), (path, code)
    json.loads(body)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(path=st.text(alphabet=st.characters(codec="ascii", exclude_characters="\r\n \x00#"), max_size=60))
def test_fuzz_get_paths_never_hang_or_leak(srv, path):
    try:
        code, _, body = get(srv, "/" + path)
    except (http.client.HTTPException, ConnectionError, OSError):
        return
    assert code < 500, (path, code)
    assert b"root:" not in body


# --- security headers -------------------------------------------------------------------------------------------------

def test_responses_carry_no_store_for_api_data(srv):
    _, headers, _ = get(srv, "/api/status")
    assert headers.get("Cache-Control") == "no-store"


# --- OTA upload --------------------------------------------------------------------------------------------------------

FAKE_ADMIN = """#!/usr/bin/env python3
import json, sys, time
mode = open(sys.argv[0] + ".mode").read().strip()
if mode == "reject":
    print(json.dumps({"ok": False, "error": "bad signature"}), flush=True)
    sys.exit(1)
print(json.dumps({"stage": "verified", "version": "9.9", "total": 1}), flush=True)
if mode == "slow":
    time.sleep(1.5)
n = 0
while True:
    b = sys.stdin.buffer.read(65536)
    if not b:
        break
    n += len(b)
print(json.dumps({"ok": True, "stage": "installed", "slot": "b", "version": "9.9", "bytes": n}), flush=True)
"""


@pytest.fixture
def ota(srv, web, monkeypatch, tmp_path):
    script = tmp_path / "admin"
    script.write_text(FAKE_ADMIN)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    def mode(m):
        (tmp_path / "admin.mode").write_text(m)
    mode("ok")
    monkeypatch.setattr(web, "ADMIN", [str(script)])
    srv.mode = mode
    return srv


def upload(srv, body=b"x" * 1000, length=None, headers=None, close_early=False):
    h = {"X-Panda-Panel": "1", "Content-Type": "application/x-panda-ota"}
    h.update(headers or {})
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=20)
    c.putrequest("POST", "/api/ota/upload")
    for k, v in h.items():
        c.putheader(k, v)
    if length is not False:
        c.putheader("Content-Length", str(len(body) if length is None else length))
    c.endheaders()
    if close_early:
        c.send(body[:10])
        c.close()
        return None
    c.send(body)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, json.loads(data)


def test_upload_success(ota):
    code, body = upload(ota, b"x" * 200000)
    assert code == 200 and body == {"ok": True, "slot": "b", "version": "9.9"}


def test_upload_rejected_by_the_installer(ota):
    ota.mode("reject")
    code, body = upload(ota, b"x" * 300000)
    assert code == 400 and "signature" in body["error"]


@pytest.mark.parametrize("headers", [{"X-Panda-Panel": "0"}, {"Content-Type": "application/json"}, {"Content-Type": "text/plain"}])
def test_upload_needs_the_panel_header_and_type(ota, headers):
    assert upload(ota, headers=headers)[0] == 403


def test_upload_needs_a_length(ota):
    assert upload(ota, length=False)[0] == 411


@pytest.mark.parametrize("length", [0, -5, (1 << 30) + 1])
def test_upload_length_limits(ota, length):
    assert upload(ota, body=b"", length=length)[0] == 413


def test_a_second_upload_is_refused_while_one_is_running(ota):
    ota.mode("slow")
    out = {}
    t = threading.Thread(target=lambda: out.update(first=upload(ota)))
    t.start()
    time.sleep(0.5)
    code, body = upload(ota)
    t.join()
    assert code == 409 and out["first"][0] == 200


def test_an_interrupted_upload_frees_the_lock(ota, web):
    upload(ota, b"x" * 1000, length=100000, close_early=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and web.UPLOAD.get("error") != "upload interrupted":
        time.sleep(0.05)
    assert web.UPLOAD.get("error") == "upload interrupted" and not web.UPLOAD.get("active")
    assert upload(ota)[0] == 200
