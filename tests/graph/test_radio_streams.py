"""H. The radio against a stream server that behaves like real ones (and badly)."""
import time

import pytest

from conftest import STATIONS, BAD_SCHEMES, station_uuid
from rig import api, wait_until

NAMES = list(STATIONS)


def play(name):
    return api("/api/radio", {"cmd": "play", "uuid": station_uuid(NAMES.index(name) + 1)})


def status():
    return api("/api/status")[1]["radio"]


def test_play_a_station_end_to_end(rig):
    code, body = play("good")
    assert code == 200 and body["station"]["name"] == "Station good", body
    rig.wait_running("Radio")
    wait_until(lambda: status().get("playing") and status().get("station", {}).get("name") == "Station good", 10,
               "status shows the station", rig=rig)
    wait_until(lambda: status().get("codec") and status().get("rate"), 15, "codec and rate in the status", rig=rig)
    assert status()["rate"] == 48000
    assert any("/json/url/" in r for r in rig.rb.requests)            # the click was counted, as the API asks


def test_icy_titles_reach_the_api(rig):
    rig.icy.title = "First Artist - First Song"
    play("good")
    rig.wait_running("Radio")
    wait_until(lambda: status().get("title") == "First Artist - First Song", 20, "the first title", rig=rig)
    rig.icy.title = "Second Artist - Second Song"
    wait_until(lambda: status().get("title") == "Second Artist - Second Song", 30, "the second title", rig=rig)


def test_stop_from_the_api(rig):
    play("good")
    rig.wait_running("Radio")
    assert api("/api/radio", {"cmd": "stop"})[0] == 200
    wait_until(lambda: "Radio" not in rig.streams(), 10, "stream gone", rig=rig)
    wait_until(lambda: status().get("playing") is False, 10, "status idle", rig=rig)


@pytest.mark.parametrize("name", ["notfound", "loop", "garbage", "slow"])
def test_a_bad_stream_leaves_the_player_up_and_the_api_responsive(rig, name):
    t0 = time.monotonic()
    code, body = play(name)
    assert code in (200, 400), (code, body)
    assert time.monotonic() - t0 < 10                                  # I8
    time.sleep(2)
    assert status()["available"] is True                               # the player itself is fine
    assert rig.services["speaker-radio"].starts == 1 or rig.services["speaker-radio"].state == "started"
    t0 = time.monotonic()
    api("/api/status")
    assert time.monotonic() - t0 < 5
    # and a good station still plays afterwards
    assert play("good")[0] == 200
    rig.wait_running("Radio")


def test_redirects_are_followed(rig):
    assert play("redirect")[0] == 200
    rig.wait_running("Radio")


def test_a_dropped_stream_is_reconnected_by_mpv(rig):
    play("drop")
    rig.wait_running("Radio")
    hits0 = rig.icy.hits.count("/drop")
    wait_until(lambda: rig.icy.hits.count("/drop") > hits0, 40, "mpv to reconnect after the drop", rig=rig)


@pytest.mark.parametrize("scheme", list(BAD_SCHEMES))
def test_non_http_urls_never_reach_mpv(rig, scheme):
    uuid = station_uuid(100 + list(BAD_SCHEMES).index(scheme))
    code, body = api("/api/radio", {"cmd": "play", "uuid": uuid})
    assert code == 400 and "http" in body["error"], (code, body)
    time.sleep(1)
    assert "Radio" not in rig.streams()


def test_unknown_station_and_radio_browser_outage(rig):
    assert api("/api/radio", {"cmd": "play", "uuid": station_uuid(9999)})[0] == 400
    rig.rb.fail = True
    code, body = api("/api/radio", {"cmd": "play", "uuid": station_uuid(1)})
    assert code in (400, 502) and "error" in body
    code, body = api("/api/radio/search?q=jazz")
    assert code == 502 and "radio-browser" in body["error"]


def test_favourites_survive_a_web_restart_and_are_walked_in_order(rig):
    for i in (1, 2, 3):
        assert api("/api/radio", {"cmd": "fav", "uuid": station_uuid(i), "add": True})[0] == 200
    rig.restart("panda-web")
    wait_until(lambda: api("/api/radio/favorites")[0] == 200, 20, "web back", rig=rig)
    names = [s["name"] for s in api("/api/radio/favorites")[1]["stations"]]
    assert names == ["Station good", "Station notfound", "Station redirect"]
    play("good")
    rig.wait_running("Radio")
    code, body = api("/api/transport", {"action": "next"})
    assert code == 200, body
    wait_until(lambda: status().get("station", {}).get("name") == "Station notfound", 10, "next favourite", rig=rig)
    for i in (1, 2, 3):
        api("/api/radio", {"cmd": "fav", "uuid": station_uuid(i), "add": False})
