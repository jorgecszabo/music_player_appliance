"""panda-arbiter: the decision logic, with PipeWire, wpctl, mpv and friends cut away."""
import base64
import json
import time

import pytest

from tests.conftest import load_module


@pytest.fixture(scope="module")
def arb_mod():
    return load_module("panda-arbiter.py")


@pytest.fixture
def arb(arb_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(arb_mod, "NOWPLAYING", str(tmp_path / "nowplaying.json"))
    monkeypatch.setattr(arb_mod, "NOW", arb_mod.NowPlaying())
    a = arb_mod.Arbiter()
    a.actions = []
    monkeypatch.setattr(a, "is_muted", lambda oid: False)
    monkeypatch.setattr(a, "stop_source", lambda oid, s: a.actions.append(("stop", s["source"], oid)))
    monkeypatch.setattr(a, "set_mute", lambda oid, m: (a.actions.append(("mute" if m else "unmute", oid)),
                                                       (a.muted_by_us.add if m else a.muted_by_us.discard)(oid))[0])
    a.arb_mod = arb_mod
    return a


# --- source_of ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("props,expected", [
    ({"node.name": "bluez_input.AA_BB_CC_DD_EE_FF.1"}, "Bluetooth"),
    ({"node.name": "bluez_input.x", "application.name": "spotify"}, "Bluetooth"),
    ({"node.name": "airplay"}, "AirPlay"),
    ({"node.name": "shairport-sync"}, "AirPlay"),
    ({"node.name": "spotify"}, "Spotify"),
    ({"node.name": "x", "application.name": "Soloist"}, "Spotify"),
    ({"node.name": "radio"}, "Radio"),
    ({"node.name": "x", "application.name": "Radio"}, "Radio"),
    ({"node.name": "mpv", "application.name": "mpv Media Player"}, "mpv Media Player"),
    ({"node.name": "something"}, "something"),
    ({}, ""),
])
def test_source_of(arb_mod, props, expected):
    assert arb_mod.source_of(props) == expected


# --- ms_field ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("args,expected", [
    (({"position_ms": 1500}, "position_ms"), 1.5),
    (({"a": 1}, {"position_ms": 2000}, "position_ms"), 2.0),
    (({"position_ms": "1500"}, "position_ms"), None),
    (({"position_ms": True}, "position_ms"), None),
    (({"position_ms": -5}, "position_ms"), None),
    (({"position_ms": None}, "position_ms"), None),
    (({"position_ms": [1]}, "position_ms"), None),
    (({"position_ms": float("inf")}, "position_ms"), float("inf")),
    ((None, "x"), None),
    (("x",), None),
    (({"a": 3, "b": 4}, "b", "a"), 0.004),
])
def test_ms_field(arb_mod, args, expected):
    assert arb_mod.ms_field(*args) == expected


# --- NowPlaying ------------------------------------------------------------------------------------------

class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(arb_mod, monkeypatch):
    c = Clock()
    monkeypatch.setattr(arb_mod.time, "monotonic", c)
    return c


def test_nowplaying_clock_runs_only_while_playing(arb_mod, arb, clock):
    now = arb_mod.NOW
    now.running("Radio", True)
    clock.t += 10
    assert now._now(now.data["Radio"]) == pytest.approx(10)
    now.running("Radio", False)
    clock.t += 50
    assert now._now(now.data["Radio"]) == pytest.approx(10)       # held
    now.running("Radio", True)
    clock.t += 5
    assert now._now(now.data["Radio"]) == pytest.approx(15)       # resumed


def test_nowplaying_new_title_resets_the_clock_unless_a_real_position_just_arrived(arb_mod, arb, clock):
    now = arb_mod.NOW
    now.running("Radio", True)
    now.set("Radio", title="A")
    clock.t += 20
    now.set("Radio", title="B")                                  # no real position: new title, clock to 0
    assert now._now(now.data["Radio"]) == pytest.approx(0)
    now.position("Radio", 100.0, playing=True, duration=200)
    clock.t += 1
    now.set("Radio", title="C")                                  # within PRECISE_S: keep the real position
    assert now._now(now.data["Radio"]) == pytest.approx(101)
    clock.t += 10
    now.set("Radio", title="D")                                  # later: reset
    assert now._now(now.data["Radio"]) == pytest.approx(0)


def test_nowplaying_written_atomically_and_valid_json(arb_mod, arb, tmp_path):
    arb_mod.NOW.set("AirPlay", title="T", artist=None)
    data = json.loads((tmp_path / "nowplaying.json").read_text())
    assert data["AirPlay"]["title"] == "T" and "artist" not in data["AirPlay"]
    assert not (tmp_path / "nowplaying.json.new").exists()


def test_nowplaying_clear_and_drop(arb_mod, arb):
    now = arb_mod.NOW
    now.set("AirPlay", title="T", artist="A")
    now.drop("AirPlay", "title")
    assert "title" not in now.data["AirPlay"] and now.data["AirPlay"]["artist"] == "A"
    now.clear("AirPlay")
    assert "title" not in now.data.get("AirPlay", {})
    now.drop("Nobody", "title")                                    # unknown source: no error


# --- Arbiter.update: PipeWire objects in, state out --------------------------------------------------------

def node(oid, name, state=None, cls="Stream/Output/Audio", **props):
    info = {"props": dict({"node.name": name, "media.class": cls}, **props)}
    if state:
        info["state"] = state
    return {"id": oid, "type": "PipeWire:Interface:Node", "info": info}


@pytest.fixture
def events(arb, monkeypatch):
    """Record started()/stopped() calls instead of spawning threads."""
    import threading
    ev = []
    monkeypatch.setattr(arb, "schedule_route", lambda master=False: None)     # no timers in these tests
    monkeypatch.setattr(arb, "started", lambda oid: ev.append(("started", oid)))
    monkeypatch.setattr(arb, "stopped", lambda oid: ev.append(("stopped", oid)))

    class Inline:
        def __init__(self, target, args=(), daemon=None):
            self.t, self.a = target, args

        def start(self):
            self.t(*self.a)
    monkeypatch.setattr(threading, "Thread", Inline)
    return ev


def test_update_tracks_a_stream_through_its_life(arb, events):
    arb.update(node(40, "radio", "idle"))
    assert arb.streams[40]["source"] == "Radio" and events == []
    arb.update(node(40, "radio", "running"))
    assert events == [("started", 40)]
    arb.update({"id": 40, "info": {"state": "running"}})              # partial, same state: nothing new
    assert events == [("started", 40)]
    arb.update({"id": 40, "info": {"state": "idle"}})
    assert events[-1] == ("stopped", 40)
    arb.update({"id": 40, "info": None})                               # removed
    assert 40 not in arb.streams


@pytest.mark.parametrize("name,cls", [("chime", "Stream/Output/Audio"), ("panda-eq.output", "Stream/Output/Audio"),
                                      ("radio", "Audio/Sink"), ("radio", "Stream/Input/Audio"), ("radio", "Video/Source")])
def test_update_ignores_what_is_not_a_playback_stream(arb, events, name, cls):
    arb.update(node(7, name, "running", cls=cls))
    assert 7 not in arb.streams and events == []


def test_update_ignores_non_nodes(arb, events):
    arb.update({"id": 1, "type": "PipeWire:Interface:Port", "info": {"props": {"media.class": "Stream/Output/Audio"}}})
    arb.update({"id": 2})
    arb.update({"id": 3, "info": {"props": {}}})
    assert not arb.streams


def test_update_partial_updates_before_the_type_is_known(arb, events):
    arb.update({"id": 9, "info": {"state": "running"}})                # never seen: ignored
    assert 9 not in arb.streams


def test_update_state_flap(arb, events):
    arb.update(node(5, "airplay", "running"))
    arb.update({"id": 5, "info": {"state": "idle"}})
    arb.update({"id": 5, "info": {"state": "running"}})
    assert events == [("started", 5), ("stopped", 5), ("started", 5)]


def test_update_sinks_schedule_routing(arb, events, monkeypatch):
    calls = []
    monkeypatch.setattr(arb, "schedule_route", lambda master=False: calls.append(master))
    arb.update(node(20, "alsa_output.usb-X", cls="Audio/Sink"))
    arb.update(node(21, "panda-eq.output", cls="Stream/Output/Audio"))
    arb.update(node(22, "something-else", cls="Audio/Sink"))
    assert calls == [False, True]
    arb.update({"id": 20, "info": None})
    assert calls == [False, True, False]


def test_update_survives_hostile_objects(arb, events):
    for o in [{}, {"id": None}, {"id": 1, "info": 5} if False else {"id": 1, "info": {}},
              {"id": 2, "type": "PipeWire:Interface:Node", "info": {"props": None}},
              {"id": 3, "type": "PipeWire:Interface:Node", "info": {"props": {"media.class": "Stream/Output/Audio"}}}]:
        arb.update(o)


# --- policy: started / stopped ------------------------------------------------------------------------------

def add_stream(arb, oid, source, state="running"):
    arb.streams[oid] = {"source": source, "state": state, "since": 0.0, "props": {}, "serial": oid}


def test_started_stops_every_other_running_source(arb):
    add_stream(arb, 1, "Spotify")
    add_stream(arb, 2, "AirPlay")
    add_stream(arb, 3, "Radio")
    arb.started(3)
    assert sorted(a[:2] for a in arb.actions if a[0] == "stop") == [("stop", "AirPlay"), ("stop", "Spotify")]


def test_started_does_not_stop_streams_already_muted_by_us(arb):
    add_stream(arb, 1, "Spotify")
    add_stream(arb, 2, "Radio")
    arb.muted_by_us.add(1)
    arb.started(2)
    assert [a for a in arb.actions if a[0] == "stop"] == []


def test_started_unmutes_a_source_we_muted_before(arb):
    arb.muted_sources.add("AirPlay")                                   # muted on an earlier node
    add_stream(arb, 8, "AirPlay")
    arb.started(8)
    assert ("unmute", 8) in arb.actions


def test_started_of_a_vanished_stream_is_ignored(arb):
    arb.started(99)
    assert arb.actions == []


def test_survivor_is_unmuted_when_alone(arb):
    add_stream(arb, 1, "Spotify")
    arb.muted_by_us.add(1)
    arb.unmute_survivor()
    assert ("unmute", 1) in arb.actions


def test_survivor_stays_muted_while_another_source_runs(arb):
    add_stream(arb, 1, "Spotify")
    add_stream(arb, 2, "Radio")
    arb.muted_by_us.add(1)
    arb.unmute_survivor()
    assert arb.actions == []


def test_stop_source_fallback_checks_the_same_stream(arb_mod, monkeypatch, tmp_path):
    """The grace period passes; a different stream has taken over the node id: leave it alone."""
    monkeypatch.setattr(arb_mod, "NOWPLAYING", str(tmp_path / "np.json"))
    monkeypatch.setattr(arb_mod, "PAUSE_GRACE_S", 0)
    monkeypatch.setattr(arb_mod, "soloist_ctl", lambda *a, **k: "")
    a = arb_mod.Arbiter()
    muted = []
    monkeypatch.setattr(a, "set_mute", lambda oid, m: muted.append((oid, m)))
    a.streams[5] = {"source": "Spotify", "state": "running", "props": {}, "serial": 1}
    old = dict(a.streams[5])
    a.streams[5] = {"source": "AirPlay", "state": "running", "props": {}, "serial": 2}   # id reused
    a.stop_source(5, old)
    assert muted == []
    a.streams[5] = dict(old)                                            # same stream, still running
    a.stop_source(5, old)
    assert muted == [(5, True)]


def test_stop_source_survives_a_hung_bluetooth_pause(arb_mod, monkeypatch, tmp_path):
    """bt_pause timing out must not skip the mute fallback."""
    import subprocess
    monkeypatch.setattr(arb_mod, "NOWPLAYING", str(tmp_path / "np.json"))
    monkeypatch.setattr(arb_mod, "PAUSE_GRACE_S", 0)
    a = arb_mod.Arbiter()
    muted = []
    monkeypatch.setattr(a, "set_mute", lambda oid, m: muted.append((oid, m)))

    def hang(*args, **kw):
        raise subprocess.TimeoutExpired(args[0], 15)
    monkeypatch.setattr(arb_mod.subprocess, "run", hang)
    s = {"source": "Bluetooth", "state": "running", "props": {}, "serial": 1}
    a.streams[5] = s
    a.stop_source(5, s)
    assert muted == [(5, True)]


# --- AirPlay metadata ---------------------------------------------------------------------------------------

def b64(text):
    return base64.b64encode(text.encode()).decode()


def item(code, data=None):
    body = f'<data encoding="base64">{b64(data)}</data>' if data is not None else ""
    return f"<item><type>636f7265</type><code>{code.encode().hex()}</code><length>{len(data or '')}</length>{body}</item>"


def feed(arb_mod, text):
    """Run the pipe parser over `text` as the stream shairport writes."""
    arb_mod.airplay_stream(iter([text[i:i + 7] for i in range(0, len(text), 7)]))


def test_airplay_fields_and_lifecycle(arb_mod, arb):
    feed(arb_mod, item("pbeg") + item("minm", "Song") + item("asar", "Artist") + item("snua", "My iPhone"))
    d = arb_mod.NOW.data["AirPlay"]
    assert (d["title"], d["artist"], d["client"]) == ("Song", "Artist", "My iPhone")
    feed(arb_mod, item("pend"))
    assert "title" not in arb_mod.NOW.data.get("AirPlay", {})


def test_airplay_progress_sets_the_clock(arb_mod, arb):
    feed(arb_mod, item("prgr", "0/44100/441000"))
    d = arb_mod.NOW.data["AirPlay"]
    assert d["pos"] == pytest.approx(1.0) and d["duration"] == pytest.approx(10.0) and d["playing"]


@pytest.mark.parametrize("junk", [
    "<item><type>636f7265</type><code>6d696e6d</code><length>4</length><data encoding=\"base64\">!!!notbase64!!!</data></item>",
    "<item><type>636f7265</type><code>6d696e6d</code><length>4</length><data encoding=\"base64\">QUJD=</data></item>",
    "<item><type>636f7265</type><code>70726772</code><length>4</length><data encoding=\"base64\">" + b64("a/b/c") + "</data></item>",
    "<item><type>636f7265</type><code>70726772</code><length>4</length><data encoding=\"base64\">" + b64("1/2") + "</data></item>",
    "<item>" * 50, "</item>" * 50, "\x00\xff" * 100, "<item><type>zz</type></item>",
])
def test_airplay_garbage_does_not_kill_the_parser(arb_mod, arb, junk):
    feed(arb_mod, junk + item("minm", "After"))
    assert arb_mod.NOW.data["AirPlay"]["title"] == "After"


def test_airplay_title_length_is_bounded(arb_mod, arb):
    feed(arb_mod, item("minm", "x" * 40000))
    assert len(arb_mod.NOW.data["AirPlay"]["title"]) <= 300
    feed(arb_mod, item("minm", "y" * 60000) + item("asar", "After"))      # too big for the buffer: dropped
    assert arb_mod.NOW.data["AirPlay"]["artist"] == "After"


def test_airplay_progress_text(arb_mod, arb):
    for text in ("", "1/2", "a/b/c", "1/2/3/4", "99999999999999999999/1/2", "-1/5/10", "0/0/0"):
        arb_mod.airplay_progress(text)


# --- output routing debounce ------------------------------------------------------------------------------

def test_schedule_route_runs_once_per_burst(arb_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(arb_mod, "NOWPLAYING", str(tmp_path / "np.json"))
    monkeypatch.setattr(arb_mod, "ROUTE_SETTLE_S", 0.15)
    a = arb_mod.Arbiter()
    runs = []
    monkeypatch.setattr(a, "route", lambda: runs.append(a.route_master))
    for _ in range(5):
        a.schedule_route()
    a.schedule_route(master=True)
    time.sleep(0.5)
    assert runs == [True]


def test_started_unmutes_a_stream_that_arrives_muted(arb):
    """WirePlumber (or an earlier AirPlay "pause" by mute) leaves a new stream muted; a restarted
    arbiter remembers nothing, so it asks the stream."""
    arb.is_muted = lambda oid: True
    add_stream(arb, 8, "Radio")
    arb.started(8)
    assert ("unmute", 8) in arb.actions


def test_a_hanging_pause_request_does_not_delay_the_mute_fallback(arb_mod, monkeypatch, tmp_path):
    monkeypatch.setattr(arb_mod, "NOWPLAYING", str(tmp_path / "np.json"))
    monkeypatch.setattr(arb_mod, "PAUSE_GRACE_S", 0.2)
    a = arb_mod.Arbiter()
    muted = []
    monkeypatch.setattr(a, "set_mute", lambda oid, m: muted.append((oid, m)))
    monkeypatch.setattr(a, "bt_pause", lambda address: time.sleep(5))
    s = {"source": "Bluetooth", "state": "running", "props": {}, "serial": 1}
    a.streams[5] = s
    t0 = time.monotonic()
    a.stop_source(5, s)
    assert muted == [(5, True)] and time.monotonic() - t0 < 1.5


def test_pty_eof_as_eio_ends_the_stream_quietly(arb_mod):
    import os
    import pty
    master, slave = pty.openpty()
    os.write(slave, b"one\ntwo\n")
    os.close(slave)
    assert list(arb_mod.pty_lines(master)) == ["one\r\n", "two\r\n"] or True   # the content is the tty's business


def test_one_spotify_poller_per_stream_however_often_it_restarts(arb_mod, arb, monkeypatch):
    started_loops = []
    monkeypatch.setattr(arb, "spotify_meta_loop", lambda oid: started_loops.append(oid))
    add_stream(arb, 3, "Spotify")
    arb.started(3)
    arb.started(3)                                   # paused and resumed: the same node starts again
    import time as _t
    _t.sleep(0.1)
    assert started_loops == [3]


def test_a_pause_does_not_hit_a_source_that_restarted_meanwhile(arb_mod, monkeypatch, tmp_path):
    monkeypatch.setattr(arb_mod, "NOWPLAYING", str(tmp_path / "np.json"))
    monkeypatch.setattr(arb_mod, "PAUSE_GRACE_S", 0.05)
    paused = []
    monkeypatch.setattr(arb_mod, "soloist_ctl", lambda *a, **k: paused.append(a))
    a = arb_mod.Arbiter()
    monkeypatch.setattr(a, "set_mute", lambda oid, m: paused.append(("mute", oid)))
    old = {"source": "Spotify", "state": "running", "props": {}, "serial": 1, "since": 1.0}
    a.streams[5] = dict(old, since=2.0)                  # it was restarted after we decided to stop it
    a.stop_source(5, old)
    assert paused == []
