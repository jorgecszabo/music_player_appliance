"""panda-web's radio logic: status mapping, the radio-browser client, favourites, summaries."""
import json
import threading
import time

import pytest

from tests.conftest import load_module
from tests.fakes.peer import mpv_player, stall
from tests.fakes.radio_browser import RadioBrowser


@pytest.fixture(scope="module")
def web():
    return load_module("panda-web.py")


@pytest.fixture
def radio(web, ipc, peers, tmp_path, monkeypatch):
    """panda-web pointed at fake sockets; OpenRC state set by the test."""
    monkeypatch.setattr(web, "mpv", ipc.mpv)
    state = {"rc": "started"}
    monkeypatch.setattr(web, "rc_states", lambda: {"speaker-radio": state["rc"]})
    web._radio_state["at"] = 0.0
    monkeypatch.setitem(web.RADIO, "station", None)

    class R:
        sock = tmp_path / "radio.sock"
        rc = state
    r = R()
    r.peers = peers
    return r


def fresh(web):
    web._radio_state["at"] = 0.0


# --- radio_status -------------------------------------------------------------------------------------

@pytest.mark.parametrize("rc,state", [("started", "stuck"), ("starting", "starting"), ("stopping", "starting"),
                                      ("inactive", "starting"), ("stopped", "stopped"), ("crashed", "stopped"),
                                      ("weird", "stopped"), (None, "stopped")])
def test_unreachable_player_state_follows_openrc(web, radio, rc, state):
    radio.rc["rc"] = rc
    web.rc_states = (lambda: {} if rc is None else {"speaker-radio": rc})
    fresh(web)
    st = web.radio_status()
    assert st["available"] is False and st["state"] == state
    assert st["message"] == web.RADIO_DOWN_TEXT[state]


def test_the_openrc_probe_is_cached_for_two_seconds(web, radio, monkeypatch):
    calls = []
    monkeypatch.setattr(web, "rc_states", lambda: calls.append(1) or {"speaker-radio": "stopped"})
    fresh(web)
    for _ in range(5):
        web.radio_status()
    assert len(calls) == 1
    web._radio_state["at"] -= 3
    web.radio_status()
    assert len(calls) == 2


def test_idle_player(web, radio):
    radio.peers(radio.sock, mpv_player({"idle-active": True}))
    assert web.radio_status() == {"available": True, "playing": False, "station": None}


def test_playing_player_reports_what_mpv_says(web, radio):
    web.RADIO["station"] = {"stationuuid": "u", "name": "S"}
    radio.peers(radio.sock, mpv_player({
        "idle-active": False, "pause": False, "audio-codec-name": "mp3", "audio-bitrate": 128000,
        "paused-for-cache": True, "metadata": {"icy-title": "Artist - Song"}, "audio-params": {"samplerate": 44100}}))
    st = web.radio_status()
    assert st["playing"] is True and st["paused"] is False and st["buffering"] is True
    assert st["title"] == "Artist - Song" and st["codec"] == "mp3" and st["rate"] == 44100 and st["station"]["name"] == "S"


@pytest.mark.parametrize("meta,expect", [({"StreamTitle": "T"}, "T"), ({"icy-title": ""}, ""), ({}, ""),
                                         ({"icy-title": "x" * 1000}, "x" * 200)])
def test_title_comes_from_icy_or_streamtitle_and_is_bounded(web, radio, meta, expect):
    radio.peers(radio.sock, mpv_player({"idle-active": False, "metadata": meta}))
    assert web.radio_status()["title"] == expect


def test_hung_player_status_is_bounded(web, radio, monkeypatch):
    radio.peers(radio.sock, stall())
    t0 = time.monotonic()
    st = web.radio_status()
    assert st["available"] is False and time.monotonic() - t0 < 3
    assert st["state"] == "stuck"


def test_player_that_dies_midway_still_reports_playing(web, radio):
    n = {"c": 0}

    def fn(conn, line):
        n["c"] += 1
        if n["c"] <= 1:
            return mpv_player({"idle-active": False})(conn, line)
        time.sleep(30)
    radio.peers(radio.sock, fn)
    t0 = time.monotonic()
    st = web.radio_status()
    assert st["available"] is True and st["playing"] is True and time.monotonic() - t0 < 3


# --- station_summary & favourites ---------------------------------------------------------------------------------

def test_station_summary_keeps_known_fields_and_bounds_text(web):
    s = web.station_summary({"stationuuid": "u", "name": "  " + "n" * 500 + "  ", "tags": "t" * 900, "evil": "x",
                             "votes": 5, "bitrate": 128, "codec": "MP3"})
    assert "evil" not in s and len(s["name"]) == 120 and len(s["tags"]) == 200 and s["votes"] == 5


@pytest.mark.parametrize("st", [{}, {"name": None}, {"name": 5}, {"name": ["x"]}, {"tags": None}, {"tags": {"a": 1}},
                                {"stationuuid": None}])
def test_station_summary_never_raises(web, st):
    out = web.station_summary(st)
    assert isinstance(out["name"], str) and isinstance(out["tags"], str)


@pytest.mark.parametrize("content", ["", "not json", "{}", "null", "5", '"x"', '[1, 2]', '[{"name": "a"}, 5, null]',
                                     '[{"name": "a"}]' * 3])
def test_favourites_file_garbage(web, tmp_path, monkeypatch, content):
    f = tmp_path / "favs.json"
    f.write_text(content)
    monkeypatch.setattr(web, "FAVORITES", str(f))
    favs = web.load_favorites()
    assert isinstance(favs, list) and all(isinstance(x, dict) for x in favs)


def test_favourites_missing_file_and_cap(web, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "FAVORITES", str(tmp_path / "nope.json"))
    assert web.load_favorites() == []
    f = tmp_path / "many.json"
    f.write_text(json.dumps([{"stationuuid": str(i), "name": "n"} for i in range(300)]))
    monkeypatch.setattr(web, "FAVORITES", str(f))
    assert len(web.load_favorites()) == 100


def test_saving_favourites_is_atomic_and_round_trips(web, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "FAVORITES", str(tmp_path / "favs.json"))
    monkeypatch.setattr(web, "schedule_save", lambda *a, **k: None)
    web.save_favorites([{"stationuuid": "u", "name": "A"}])
    assert not (tmp_path / "favs.json.new").exists()
    assert web.load_favorites()[0]["name"] == "A"


# --- the radio-browser client ---------------------------------------------------------------------------------------

@pytest.fixture
def rb(web, monkeypatch):
    srv = RadioBrowser([{"stationuuid": "00000000-0000-4000-8000-000000000001", "name": "S", "url_resolved": "http://x/y"}])
    monkeypatch.setattr(web, "RB_BASE", srv.base)
    web._radio_cache.clear()
    yield srv
    srv.close()


def test_rb_get_returns_json_and_caches(web, rb):
    a = web.rb_get("/json/stations/search", {"name": "S"})
    n = len(rb.requests)
    b = web.rb_get("/json/stations/search", {"name": "S"})
    assert a == b and len(rb.requests) == n


def test_rb_get_does_not_cache_click_counting_urls(web, rb):
    web.rb_get("/json/url/00000000-0000-4000-8000-000000000001")
    n = len(rb.requests)
    web.rb_get("/json/url/00000000-0000-4000-8000-000000000001")
    assert len(rb.requests) == n + 1


def test_rb_get_cache_expires_and_is_bounded(web, rb):
    web.rb_get("/json/stations/search", {"name": "S"}, ttl=0)
    n = len(rb.requests)
    web.rb_get("/json/stations/search", {"name": "S"}, ttl=0)
    assert len(rb.requests) == n + 1
    for i in range(400):
        web._radio_cache[f"/k{i}"] = (time.monotonic(), [])
    web.rb_get("/json/tags", {"q": "x"})
    assert len(web._radio_cache) < 400


def test_rb_get_server_error_raises_oserror(web, rb):
    rb.fail = True
    with pytest.raises(OSError):
        web.rb_get("/json/stations/search", {"name": "boom"})


@pytest.mark.parametrize("body", [b"not json", b"", b"\xff\xfe", b"{"])
def test_rb_get_garbage_body_is_an_error_not_a_crash(web, monkeypatch, body):
    import http.server
    import socketserver

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(web, "RB_BASE", "http://%s:%d" % srv.server_address)
        web._radio_cache.clear()
        with pytest.raises((OSError, ValueError)):
            web.rb_get("/json/x")
    finally:
        srv.shutdown()
        srv.server_close()
