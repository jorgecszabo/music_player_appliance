"""Fixtures for the audio-graph suite. Only runs inside the rig container (RIG=1)."""
import os
import signal
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

if os.environ.get("RIG") != "1":
    collect_ignore_glob = ["test_*.py"]


STATIONS = {
    "good":     "/stream",
    "notfound": "/404",
    "redirect": "/redirect",
    "loop":     "/loop",
    "slow":     "/slow",
    "drop":     "/drop",
    "garbage":  "/garbage",
}
BAD_SCHEMES = {"file": "file:///etc/passwd", "rtsp": "rtsp://127.0.0.1/x", "edit": "edit://x", "empty": ""}


def station_uuid(n):
    return "00000000-0000-4000-8000-%012x" % n


@pytest.fixture(scope="session")
def _rig():
    from rig import Rig
    from tests.fakes.icy_server import IcyServer
    from tests.fakes.radio_browser import RadioBrowser
    icy = IcyServer()
    stations = []
    for i, (name, path) in enumerate(STATIONS.items(), 1):
        stations.append({"stationuuid": station_uuid(i), "name": f"Station {name}", "url_resolved": icy.base + path,
                         "url": icy.base + path, "codec": "WAV", "bitrate": 1536, "tags": "test"})
    for j, (name, url) in enumerate(BAD_SCHEMES.items(), 100):
        stations.append({"stationuuid": station_uuid(j), "name": f"Scheme {name}", "url_resolved": url, "url": url})
    rb = RadioBrowser(stations)
    os.environ["RIG_RADIO_BROWSER"] = rb.base
    r = Rig()
    r.icy, r.rb = icy, rb
    r.up()
    yield r
    r.down()
    icy.close()
    rb.close()


def _reset(r):
    """Between scenarios: every service up, every source idle and gone, nothing muted."""
    from rig import wait_until
    r.icy.title = "Fake Artist - Fake Song"
    r.rb.fail = False
    for sock in ("/run/speaker/radio.sock",):
        if os.path.exists(sock):
            os.chmod(sock, 0o660)
    for fs in r.fake_sources.values():
        try:
            fs.resume()                        # un-hang a source a previous test paused
        except OSError:
            fs.close()                         # a player that died: start a fresh one
            fs.start()
    for n in ("fake-dbus", "fake-bluez", "speaker-pipewire", "speaker-wireplumber", "speaker-radio",
              "speaker-arbiter", "panda-web"):
        svc = r.services[n]
        if svc.proc is not None and svc.proc.poll() is None:
            try:
                os.killpg(svc.proc.pid, signal.SIGCONT)      # a test may have hung it
            except ProcessLookupError:
                pass
        if svc.state != "started":
            r.start(n)
    for src in list(r.fake_sources) + ["Radio"]:
        try:
            r.stop_source(src)
        except OSError:
            pass
    wait_until(lambda: not r.streams() or all(n["state"] != "running" for n in r.streams().values()),
               10, "all sources stopped", rig=r)
    for d in ("soloist", "bluez", "route"):
        base = f"/run/rig/fake/{d}"
        for f in os.listdir(base):
            if f in ("calls", "mode", "ignore-pause", "now.json"):
                os.unlink(os.path.join(base, f))
    # forget decisions from earlier scenarios: remember where the arbiter log was
    r.log_mark = len(r.log("speaker-arbiter"))
    r.admin_calls.clear()


@pytest.fixture
def rig(_rig, request):
    _reset(_rig)
    yield _rig
    rep = getattr(request.node, "rep_call", None)
    if rep is not None and rep.failed:
        _rig.dump_artifacts(f"/artifacts/{request.node.name.replace('/', '_')}")
    tb = [ln for ln in _rig.log("speaker-arbiter")[_rig.log_mark:].splitlines() if "Traceback" in ln]
    assert not tb, "the arbiter logged a traceback (I7)\n" + _rig.log("speaker-arbiter")[_rig.log_mark:][-2000:]


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    setattr(item, "rep_" + call.when, outcome.get_result())
