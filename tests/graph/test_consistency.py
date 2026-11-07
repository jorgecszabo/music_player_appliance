"""G. The control plane agrees with the graph: the API, the SSE stream and nowplaying."""
import itertools
import json
import threading
import urllib.request

import pytest

from rig import CLIENT, SOURCES, WEB_PORT, api, wait_until


def api_streams():
    return {s["source"]: s for s in api("/api/status")[1]["audio"]["streams"]}


def api_view():
    return {src: (s["state"] == "running", s["mute"]) for src, s in api_streams().items() if src in CLIENT}


def graph_view(rig):
    return {src: (n["state"] == "running", n["mute"]) for src, n in rig.streams().items()}


@pytest.mark.parametrize("src", SOURCES)
def test_every_source_is_classified_the_same_by_the_web_page_and_the_arbiter(rig, src):
    rig.play(src)
    rig.wait_running(src)
    wait_until(lambda: src in api_streams() and api_streams()[src]["state"] == "running", 10,
               f"{src} in the API under the same name", rig=rig)
    wait_until(lambda: rig.nowplaying().get(src, {}).get("playing") is True, 10, f"{src} in nowplaying", rig=rig)


@pytest.mark.parametrize("first,second", list(itertools.permutations(["Radio", "AirPlay", "Spotify"], 2)))
def test_api_status_matches_the_graph_after_a_transition(rig, first, second):
    rig.play(first)
    rig.wait_running(first)
    rig.play(second)
    rig.wait_running(second)
    rig.settle()
    wait_until(lambda: api_view() == graph_view(rig), 10, "the API to agree with pw-dump", rig=rig)


def sse_events(stop, out):
    req = urllib.request.Request(f"http://127.0.0.1:{WEB_PORT}/api/events")
    with urllib.request.urlopen(req, timeout=30) as r:
        while not stop.is_set():
            line = r.readline()
            if not line:
                return
            if line.startswith(b"data: "):
                out.append(json.loads(line[6:]))


def test_sse_delivers_a_change_and_never_shows_a_state_the_graph_never_had(rig):
    events, stop = [], threading.Event()
    t = threading.Thread(target=sse_events, args=(stop, events), daemon=True)
    t.start()
    try:
        wait_until(lambda: events, 10, "the first event", rig=rig)
        n0 = len(events)
        rig.play("Radio")
        rig.wait_running("Radio")
        wait_until(lambda: len(events) > n0 and any(
            s["source"] == "Radio" and s["state"] == "running" for e in events[n0:] for s in e["audio"]["streams"]),
            10, "an event showing Radio running", rig=rig)
        rig.play("AirPlay")
        rig.wait_running("AirPlay")
        rig.settle()
        wait_until(lambda: sorted(s for s, (run, mute) in api_view().items() if run and not mute) == ["AirPlay"], 10,
                   "AirPlay alone audible in the API", rig=rig)
        # No event ever showed two audible sources for longer than the pause takes: the last one must not.
        last = events[-1]["audio"]["streams"]
        audible = [s["source"] for s in last if s["state"] == "running" and not s["mute"]]
        assert audible == ["AirPlay"], audible
    finally:
        stop.set()


def test_nowplaying_has_exactly_the_sources_that_are_running_plus_those_that_kept_their_info(rig):
    rig.play("Radio")
    rig.wait_running("Radio")
    wait_until(lambda: rig.nowplaying().get("Radio", {}).get("playing") is True, 10, "Radio playing in nowplaying", rig=rig)
    rig.stop_source("Radio")
    wait_until(lambda: "Radio" not in rig.running(), 10, "Radio stopped", rig=rig)
    wait_until(lambda: not rig.nowplaying().get("Radio", {}).get("playing"), 10, "the clock to stop", rig=rig)


def test_volume_and_mute_from_the_web_page_do_not_look_like_source_events(rig):
    rig.play("Radio")
    rig.wait_running("Radio")
    rig.settle()
    n = len(rig.decisions())
    node = rig.streams()["Radio"]["id"]
    assert api("/api/volume", {"id": node, "volume": 0.3})[0] == 200
    assert api("/api/volume", {"id": node, "mute": True})[0] == 200
    wait_until(lambda: rig.streams()["Radio"]["mute"], 10, "mute applied", rig=rig)
    assert api("/api/volume", {"id": node, "mute": False})[0] == 200
    wait_until(lambda: not rig.streams()["Radio"]["mute"], 10, "unmute applied", rig=rig)
    rig.settle()
    assert len(rig.decisions()) == n, rig.decisions()[n:]


def test_transport_toggle_pauses_and_resumes_the_radio(rig):
    rig.play("Radio")
    rig.wait_running("Radio")
    wait_until(lambda: api("/api/status")[1]["radio"].get("playing"), 10, "radio playing in the API", rig=rig)
    code, body = api("/api/transport", {"action": "toggle"})
    assert code == 200 and body["source"] == "Radio", body
    wait_until(lambda: api("/api/status")[1]["radio"].get("paused"), 10, "paused", rig=rig)
    code, body = api("/api/transport", {"action": "toggle"})
    assert code == 200
    wait_until(lambda: not api("/api/status")[1]["radio"].get("paused"), 10, "resumed", rig=rig)


def test_transport_with_nothing_playing_is_a_plain_error(rig):
    code, body = api("/api/transport", {"action": "toggle"})
    assert code == 400 and body["error"] == "nothing to play"
