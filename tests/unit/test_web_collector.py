"""panda-web's status collector: every probe may fail, the snapshot still comes out."""
import json
import time

import pytest

from tests.conftest import load_module


@pytest.fixture(scope="module")
def web():
    return load_module("panda-web.py")


@pytest.fixture
def col(web, monkeypatch, ipc):
    monkeypatch.setattr(web, "run", lambda cmd, *a, **k: "")
    monkeypatch.setattr(web, "CONF_DIR", "/nonexistent")
    return web.Collector()


def test_a_snapshot_of_a_system_where_nothing_answers(web, col):
    snap = col.collect()
    json.dumps(snap)
    for key in ("time", "host", "cpu", "mem", "audio", "bt", "wifi", "services", "restartable", "tone", "radio", "nowplaying"):
        assert key in snap, key
    assert snap["radio"]["available"] is False
    assert "unavailable" in snap["bt"]["error"] and "unavailable" in snap["wifi"]["error"]


def test_restartable_in_the_snapshot_is_the_servers_list(web, col):
    assert col.collect()["restartable"] == sorted(web.RESTARTABLE)


def test_the_slow_probes_are_cached_between_snapshots(web, col, monkeypatch):
    calls = []
    monkeypatch.setattr(col, "services", lambda: calls.append(1) or {"x": "started"})
    col.probes = [("services", 15, col.services)]
    col.slow_at.clear()
    now = time.monotonic()
    for name, period, fn in col.probes:
        col.slow[name] = fn()
        col.slow_at[name] = now
    assert col.collect()["services"] == {"x": "started"} and len(calls) == 1


def test_services_probe_marks_unlisted_ones(web, col, monkeypatch):
    monkeypatch.setattr(web, "rc_states", lambda: {"speaker-radio": "started", "sshd": "crashed"})
    s = col.services()
    assert s["speaker-radio"] == "started" and s["sshd"] == "crashed" and s["panda-web"] == "not in a runlevel"


def test_play_clock_arithmetic(web):
    snap = {"audio": {"streams": [{"source": "Radio", "state": "running"}, {"source": "Spotify", "state": "idle"}]},
            "nowplaying": {"Radio": {"pos": 10.0, "pos_at": time.monotonic() - 5, "playing": True},
                           "Spotify": {"pos": 3.0, "pos_at": time.monotonic() - 100, "playing": False}},
            "bt": {}}
    web.Collector.play_clocks(snap)
    radio, spotify = snap["audio"]["streams"]
    assert radio["elapsed"] == pytest.approx(15, abs=1) and spotify["elapsed"] == pytest.approx(3, abs=0.1)


@pytest.mark.parametrize("nowplaying", [{}, {"Radio": None}, {"Radio": {}}, {"Radio": {"pos": None, "playing": True}}])
def test_play_clock_survives_odd_nowplaying(web, nowplaying):
    snap = {"audio": {"streams": [{"source": "Radio", "state": "running"}]}, "nowplaying": nowplaying, "bt": {}}
    try:
        web.Collector.play_clocks(snap)
    except (TypeError, AttributeError, ValueError):
        pytest.fail(f"play_clocks crashed on {nowplaying!r}")
