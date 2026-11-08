"""panda-web's HTTP API: validation, and what reaches the root helper.

The handler runs for real on a loopback port; everything it would call (the root helper,
wpctl, the peers, mpv, radio-browser) is replaced by recorders.
"""
import http.client
import json
import re
import threading
from http.server import ThreadingHTTPServer

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.conftest import load_module

PATHS = ["/api/volume", "/api/bt", "/api/service", "/api/system", "/api/name", "/api/spotify-key",
         "/api/audio", "/api/radio", "/api/wifi", "/api/ota", "/api/transport"]
HDR = {"X-Panda-Panel": "1", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def web():
    return load_module("panda-web.py")


@pytest.fixture
def srv(web, monkeypatch, tmp_path):
    calls = []          # (kind, args) of everything the handler tried to run or call

    def run(cmd, timeout=10, env=None, stdin=None):
        if cmd[0] == "ip":                  # the Host-header check asks for the local addresses
            return ""
        calls.append(("run", list(cmd), stdin))
        if cmd[-1:] == ["factory-reset-dry"]:
            return "ok"
        return "ok\n"

    monkeypatch.setattr(web, "run", run)
    monkeypatch.setattr(web, "bt_call", lambda req, timeout=5: calls.append(("bt", req)) or {"ok": True})
    monkeypatch.setattr(web, "wifi_call", lambda req, timeout=5: calls.append(("wifi", req)) or {"ok": True})
    monkeypatch.setattr(web, "mpv", lambda *a, **k: calls.append(("mpv", list(a))))
    monkeypatch.setattr(web, "mpv_raw", lambda *a, **k: calls.append(("mpv_raw", list(a))) or {"error": "success"})
    monkeypatch.setattr(web, "rb_get", lambda path, *a, **k: [] if "byuuid" in path else {})
    monkeypatch.setattr(web, "schedule_save", lambda *a, **k: None)
    monkeypatch.setattr(web, "remember_master", lambda *a, **k: calls.append(("master", a)))
    monkeypatch.setattr(web, "FAVORITES", str(tmp_path / "favs.json"))
    monkeypatch.setattr(web.COLLECTOR, "refresh", lambda *a: None)
    monkeypatch.setattr(web.COLLECTOR, "status",
                        {"audio": {"streams": [{"id": 5, "source": "Radio", "state": "running", "mute": False}],
                                   "sink": {"id": 9}}})
    s = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    s.daemon_threads = True
    threading.Thread(target=s.serve_forever, daemon=True).start()
    s.calls = calls
    s.port = s.server_address[1]
    yield s
    s.shutdown()
    s.server_close()


def post(srv, path, body, headers=None, raw=None, timeout=5):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=timeout)
    try:
        data = raw if raw is not None else json.dumps(body).encode()
        h = dict(HDR if headers is None else headers)
        c.request("POST", path, body=data, headers=h)
        r = c.getresponse()
        txt = r.read()
        try:
            return r.status, json.loads(txt)
        except ValueError:
            return r.status, txt
    finally:
        c.close()


# --- what is allowed to reach the root helper --------------------------------------------------

ADMIN_OK = [
    r"restart (speaker-soloist|speaker-shairport|speaker-radio|panda-bt|panda-wifi|bluetooth|avahi-daemon|chronyd)",
    r"restart-audio", r"reboot", r"poweroff", r"save", r"factory-reset", r"factory-reset-dry",
    r"set-name [A-Za-z0-9][A-Za-z0-9 ._'-]{0,31}",
    r"set-audio -?\d+ -?\d+ -?\d+ (onboard|usb) -?\d+ [A-Za-z0-9][A-Za-z0-9:+._-]{0,79}",
    r"set-master (0(\.\d{1,3})?|1(\.0{1,3})?)", r"ota-activate [ab]", r"ota-install", r"set-soloist-key",
]


def check_calls(web, calls):
    """Every command line the handler ran is one the helper documents, with safe arguments."""
    for kind, *rest in calls:
        if kind != "run":
            continue
        cmd = rest[0]
        if cmd[:len(web.ADMIN)] == web.ADMIN:
            line = " ".join(cmd[len(web.ADMIN):])
            assert any(re.fullmatch(p, line) for p in ADMIN_OK), f"unexpected admin call: {cmd!r}"
            assert all("\n" not in a and "\0" not in a for a in cmd), cmd
        else:
            assert cmd[0] == "wpctl" and cmd[1] in ("set-mute", "set-volume") and cmd[2].isdigit(), cmd
            assert re.fullmatch(r"[01]|\d\.\d{3}|1\.000", cmd[3]), cmd


# --- the gate ------------------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [{}, {"Content-Type": "application/json"}, {"X-Panda-Panel": "1"},
                                      {"X-Panda-Panel": "1", "Content-Type": "text/plain"},
                                      {"X-Panda-Panel": "0", "Content-Type": "application/json"}])
def test_post_needs_the_panel_header_and_json(srv, headers):
    code, _ = post(srv, "/api/service", {"name": "speaker-radio"}, headers=headers)
    assert code == 403
    assert srv.calls == []


def test_unknown_post_path_is_404(srv):
    assert post(srv, "/api/nope", {})[0] == 404


@pytest.mark.parametrize("raw", [b"[]", b"1", b'"x"', b"null", b"{", b"\xff", b"{" * 100000])
def test_non_object_bodies_are_rejected(srv, raw):
    code, _ = post(srv, "/api/service", None, raw=raw)
    assert code == 400
    assert srv.calls == []


def test_negative_content_length_does_not_hang_a_thread(srv):
    """A client that lies about the length must get an answer, not hold a thread forever."""
    import socket
    s = socket.create_connection(("127.0.0.1", srv.port), timeout=3)
    s.sendall(b"POST /api/service HTTP/1.1\r\nHost: x\r\nX-Panda-Panel: 1\r\nContent-Type: application/json\r\n"
              b"Content-Length: -1\r\n\r\n")
    s.settimeout(3)
    try:
        data = s.recv(4096)
    except socket.timeout:
        pytest.fail("the server is waiting for a body that was never announced")
    finally:
        s.close()
    assert data.startswith(b"HTTP/1.")


# --- /api/service ---------------------------------------------------------------------------------

def test_service_restart_runs_the_helper(srv, web):
    assert post(srv, "/api/service", {"name": "speaker-radio"})[0] == 200
    assert ("run", web.ADMIN + ["restart", "speaker-radio"], None) in srv.calls


def test_service_audio_restarts_the_stack(srv, web):
    assert post(srv, "/api/service", {"name": "audio"})[0] == 200
    assert ("run", web.ADMIN + ["restart-audio"], None) in srv.calls


@pytest.mark.parametrize("name", ["sshd", "panda-web", "speaker-arbiter", "", "speaker-radio ", "../x",
                                  "speaker-radio;reboot", None, 5, ["speaker-radio"], {"a": 1}])
def test_service_refuses_other_names_cleanly(srv, name):
    code, body = post(srv, "/api/service", {"name": name})
    assert code == 400, (code, body)
    assert srv.calls == []


# --- /api/radio -----------------------------------------------------------------------------------

UUID = "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("uuid", ["", "x", UUID + "0", UUID[:-1], "../" * 12, "g" * 36, None, 5, [UUID]])
def test_radio_bad_ids(srv, uuid):
    for cmd in ("play", "fav"):
        code, body = post(srv, "/api/radio", {"cmd": cmd, "uuid": uuid})
        assert code == 400, (cmd, uuid, code, body)


def test_radio_unknown_station(srv):
    code, body = post(srv, "/api/radio", {"cmd": "play", "uuid": UUID})
    assert code == 400 and "no longer listed" in body["error"]


def test_radio_stop(srv):
    assert post(srv, "/api/radio", {"cmd": "stop"})[0] == 200
    assert ("mpv", ["stop"]) in srv.calls


@pytest.mark.parametrize("url", ["file:///etc/passwd", "rtsp://x/y", "edit://x", "", "javascript:alert(1)",
                                 "ftp://x/y", "  http://x/y", "HTTP://x/y"])
def test_radio_play_only_http_streams(srv, web, monkeypatch, url):
    station = {"stationuuid": UUID, "name": "S", "url_resolved": url, "url": url}
    monkeypatch.setattr(web, "rb_get", lambda path, *a, **k: [station] if "byuuid" in path else {"url": url})
    code, body = post(srv, "/api/radio", {"cmd": "play", "uuid": UUID})
    assert code == 400, (url, code, body)
    assert not [c for c in srv.calls if c[0] == "mpv_raw"]


def test_radio_play_http_stream_loads_it(srv, web, monkeypatch):
    url = "http://example.test/stream"
    monkeypatch.setattr(web, "rb_get", lambda path, *a, **k: [{"stationuuid": UUID, "name": "S"}] if "byuuid" in path else {"url": url})
    code, body = post(srv, "/api/radio", {"cmd": "play", "uuid": UUID})
    assert code == 200 and body["station"]["name"] == "S"
    assert ("mpv_raw", ["loadfile", url, "replace"]) in srv.calls


def test_radio_play_when_the_player_is_down_says_so_in_plain_words(srv, web, monkeypatch):
    monkeypatch.setattr(web, "rb_get", lambda path, *a, **k: [{"stationuuid": UUID, "name": "S"}] if "byuuid" in path else {"url": "http://x/y"})

    def down(*a, **k):
        raise FileNotFoundError("radio.sock")
    monkeypatch.setattr(web, "mpv_raw", down)
    monkeypatch.setattr(web, "rc_states", lambda: {"speaker-radio": "stopped"})
    web._radio_state["at"] = 0.0
    code, body = post(srv, "/api/radio", {"cmd": "play", "uuid": UUID})
    assert code == 400
    assert body["error"] == web.RADIO_DOWN_TEXT["stopped"]
    assert "mpv" not in body["error"].lower() and "speaker-radio" not in body["error"]


# --- the numeric endpoints ----------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"id": "abc"}, {"id": None}, {"id": []}, {"id": {}}, {"id": 1e999}, {"id": 5, "volume": "x"},
    {"id": 5, "volume": []}, {"id": 5, "volume": None}, {"id": 99}, {"id": -1}, {"id": True},
])
def test_volume_bad_input_is_a_400_not_a_500(srv, body):
    code, resp = post(srv, "/api/volume", body)
    assert code in (200, 400, 409), (body, code, resp)


@pytest.mark.parametrize("body", [{"id": 99}, {"id": -1}, {"id": 5000, "mute": True}])
def test_volume_on_a_node_that_is_gone_is_stale(srv, body):
    code, resp = post(srv, "/api/volume", body)
    assert code == 409 and resp["stale"] is True
    assert not [c for c in srv.calls if c[0] == "run"]


def test_volume_unparseable_id_is_not_stale(srv):
    code, resp = post(srv, "/api/volume", {"id": "abc"})
    assert code == 400 and "stale" not in resp


@pytest.mark.parametrize("v,expect", [(0.5, "0.500"), (2, "1.000"), (-3, "0.000"), ("0.25", "0.250")])
def test_volume_is_clamped(srv, web, v, expect):
    assert post(srv, "/api/volume", {"id": 5, "volume": v})[0] == 200
    assert ("run", ["wpctl", "set-volume", "5", expect], None) in srv.calls


def test_volume_nan_and_inf_are_safe(srv, web):
    for v in ("nan", "inf", "-inf"):
        code, _ = post(srv, "/api/volume", {"id": 5, "volume": v})
        assert code in (200, 400)
    check_calls(web, srv.calls)


@pytest.mark.parametrize("body", [
    {"bass": 1, "treble": 1, "cap": 1, "usb_cap": 1, "output": "onboard"},
    {"bass": float("inf"), "treble": 1, "cap": 1, "usb_cap": 1, "output": "onboard"},
    {"bass": [1], "treble": 1, "cap": 1, "usb_cap": 1, "output": "onboard"},
    {"bass": 1, "treble": 1, "cap": 1, "usb_cap": 1, "output": ["usb"]},
    {"bass": 1, "treble": 1, "cap": 1, "usb_cap": 1, "output": "onboard", "usb_profile": "a b"},
    {"bass": 1, "treble": 1, "cap": 1, "usb_cap": 1, "output": "onboard", "usb_profile": "x\nreboot"},
    {"bass": "1; reboot", "treble": 1, "cap": 1, "usb_cap": 1, "output": "onboard"},
    {"bass": 10 ** 400, "treble": 1, "cap": 1, "usb_cap": 1, "output": "onboard"},
])
def test_audio_settings_never_500(srv, web, body):
    code, resp = post(srv, "/api/audio", body)
    assert code in (200, 400), (body, code, resp)
    check_calls(web, srv.calls)


@pytest.mark.parametrize("name", ["", " ", "a" * 33, "bad;name", "name\nreboot", "$(reboot)", "`reboot`", "-rf",
                                  "../x", "naïve", "a\0b", None, 5, ["x"]])
def test_speaker_name_is_validated(srv, name):
    code, _ = post(srv, "/api/name", {"name": name})
    assert code == 400 and srv.calls == [], name


@pytest.mark.parametrize("name", ["Kitchen", "Living Room", "a", "a.b_c-d'e", "A" * 32])
def test_speaker_name_valid(srv, web, name):
    code, _ = post(srv, "/api/name", {"name": name})
    assert code == 200
    assert ("run", web.ADMIN + ["set-name", name], None) in srv.calls


@pytest.mark.parametrize("action,expect", [("save", "save"), ("reboot", "reboot"), ("poweroff", "poweroff"),
                                           ("factory-reset-check", "factory-reset-dry")])
def test_system_actions(srv, web, action, expect):
    assert post(srv, "/api/system", {"action": action})[0] == 200
    assert ("run", web.ADMIN + [expect], None) in srv.calls


@pytest.mark.parametrize("action", ["", "format", "reboot;poweroff", None, ["reboot"], 5])
def test_system_unknown_action(srv, action):
    assert post(srv, "/api/system", {"action": action})[0] == 400
    assert srv.calls == []


@pytest.mark.parametrize("slot", ["c", "", "ab", "a;b", None, ["a"], 1])
def test_ota_activate_slot(srv, slot):
    assert post(srv, "/api/ota", {"cmd": "activate", "slot": slot})[0] == 400
    assert srv.calls == []


def test_transport_with_nothing_to_play(srv, web, monkeypatch):
    monkeypatch.setitem(web.RADIO, "station", None)
    monkeypatch.setattr(web.COLLECTOR, "status", {"audio": {"streams": []}})
    code, body = post(srv, "/api/transport", {"action": "toggle"})
    assert code == 400 and body["error"] == "nothing to play"


@pytest.mark.parametrize("action", ["", "stop", None, ["toggle"], 3])
def test_transport_bad_action(srv, action):
    assert post(srv, "/api/transport", {"action": action})[0] == 400


# --- fuzzing: nothing a LAN client sends may produce a 5xx or an unsafe helper call ------------------------

json_leaf = st.one_of(st.none(), st.booleans(), st.integers(min_value=-10 ** 30, max_value=10 ** 30),
                      st.floats(allow_nan=False), st.text(max_size=100))
json_value = st.recursive(json_leaf, lambda c: st.one_of(st.lists(c, max_size=4),
                                                         st.dictionaries(st.text(max_size=12), c, max_size=6)),
                          max_leaves=12)
KEYS = ["name", "id", "volume", "mute", "cmd", "action", "uuid", "add", "slot", "bass", "treble", "cap", "usb_cap",
        "output", "usb_profile", "key", "ssid", "password", "hidden", "country", "adapter", "path", "open"]
body_strategy = st.dictionaries(st.sampled_from(KEYS) | st.text(max_size=8), json_value, max_size=8)


@settings(max_examples=int(__import__('os').environ.get('FUZZ_EXAMPLES', 120)), deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(path=st.sampled_from(PATHS), body=body_strategy)
def test_fuzz_post_never_5xx_and_only_safe_calls(srv, web, path, body):
    del srv.calls[:]
    code, resp = post(srv, path, body)
    assert code < 500, (path, body, code, resp)
    check_calls(web, srv.calls)
    for kind, *rest in srv.calls:
        if kind == "mpv_raw":
            assert rest[0][0] == "loadfile" and re.match(r"https?://", rest[0][1])


def test_transport_toggle_with_a_stale_radio_stream_is_a_clean_error(srv, web, monkeypatch):
    """The page restarted while the radio plays: the status says Radio is audible, but the
    station name is gone. Toggle must not crash."""
    monkeypatch.setitem(web.RADIO, "station", None)
    monkeypatch.setattr(web.COLLECTOR, "status",
                        {"audio": {"streams": [{"id": 5, "source": "Radio", "state": "running", "mute": False}]}})
    monkeypatch.setattr(web, "radio_status", lambda: {"available": False})
    code, body = post(srv, "/api/transport", {"action": "toggle"})
    assert code == 400, (code, body)
