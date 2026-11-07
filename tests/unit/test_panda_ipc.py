"""panda_ipc: the peers being down, hung or lying is an ordinary answer."""
import os
import socket
import time

import pytest

from tests.fakes.peer import (close_silently, mpv_player, partial_then_stall, reply_json,
                              reply_raw, stall)


# --- line_call ------------------------------------------------------------------------

def test_line_call_normal(ipc, peers, tmp_path):
    p = peers(tmp_path / "bt.sock", reply_json({"ok": True, "n": 1}))
    assert ipc.bt_call({"cmd": "status"}) == {"ok": True, "n": 1}
    assert p.requests == [b'{"cmd": "status"}']


def test_line_call_socket_missing(ipc):
    r = ipc.bt_call({"cmd": "status"})
    assert r == {"error": "panda-bt unavailable (FileNotFoundError)"}


def test_line_call_names_the_peer(ipc):
    assert ipc.wifi_call({"cmd": "status"})["error"].startswith("panda-wifi unavailable")


def test_line_call_nobody_listening(ipc, tmp_path):
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(tmp_path / "bt.sock"))        # bound, never listen()ed: connection refused
    try:
        assert "unavailable" in ipc.bt_call({"cmd": "x"})["error"]
    finally:
        s.close()


def test_line_call_permission_denied(ipc, peers, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores socket permissions")
    peers(tmp_path / "bt.sock", reply_json({"ok": True}))
    os.chmod(tmp_path / "bt.sock", 0o000)
    assert "unavailable (PermissionError)" in ipc.bt_call({"cmd": "x"})["error"]


def test_line_call_closed_without_reply(ipc, peers, tmp_path):
    peers(tmp_path / "bt.sock", close_silently)
    assert "unavailable" in ipc.bt_call({"cmd": "x"})["error"]


@pytest.mark.parametrize("garbage", [b"not json\n", b"\xff\xfe\n", b"\n", b"{\n", b""])
def test_line_call_garbage_reply(ipc, peers, tmp_path, garbage):
    peers(tmp_path / "bt.sock", reply_raw(garbage))
    r = ipc.bt_call({"cmd": "x"})
    assert "unavailable" in r["error"]


def test_line_call_hung_peer_times_out(ipc, peers, tmp_path):
    peers(tmp_path / "bt.sock", stall())
    t0 = time.monotonic()
    r = ipc.bt_call({"cmd": "x"}, timeout=0.3)
    assert "unavailable" in r["error"]
    assert time.monotonic() - t0 < 2


def test_line_call_partial_reply_then_hang(ipc, peers, tmp_path):
    peers(tmp_path / "bt.sock", partial_then_stall())
    t0 = time.monotonic()
    assert "unavailable" in ipc.bt_call({"cmd": "x"}, timeout=0.3)["error"]
    assert time.monotonic() - t0 < 2


def test_line_call_big_reply(ipc, peers, tmp_path):
    big = {"data": "x" * 2_000_000}
    peers(tmp_path / "wifi.sock", reply_json(big))
    assert ipc.wifi_call({"cmd": "scan"}, timeout=10) == big


def test_line_call_non_object_json_is_returned_as_is(ipc, peers, tmp_path):
    # A peer answering a bare list is a protocol violation, but not a crash.
    peers(tmp_path / "bt.sock", reply_json([1, 2]))
    assert ipc.bt_call({"cmd": "x"}) == [1, 2]


# --- mpv ------------------------------------------------------------------------------

def test_mpv_success(ipc, peers, tmp_path):
    calls = []
    peers(tmp_path / "radio.sock", mpv_player({"pause": True}, calls=calls))
    assert ipc.mpv("get_property", "pause") is True
    assert calls == [["get_property", "pause"]]


def test_mpv_property_unavailable_is_a_reply_not_an_exception(ipc, peers, tmp_path):
    peers(tmp_path / "radio.sock", mpv_player({}))
    msg = ipc.mpv_raw("get_property", "metadata")
    assert msg["error"] == "property unavailable"
    assert ipc.mpv("get_property", "metadata") is None


def test_mpv_skips_interleaved_events(ipc, peers, tmp_path):
    peers(tmp_path / "radio.sock", mpv_player({"pause": False}, events=5))
    assert ipc.mpv("get_property", "pause") is False


def test_mpv_unreachable_raises_oserror_raw_and_none_wrapped(ipc):
    with pytest.raises(OSError):
        ipc.mpv_raw("get_property", "pause")
    assert ipc.mpv("get_property", "pause") is None


def test_mpv_closed_without_reply(ipc, peers, tmp_path):
    peers(tmp_path / "radio.sock", close_silently)
    with pytest.raises(OSError):
        ipc.mpv_raw("stop")
    assert ipc.mpv("stop") is None


def test_mpv_reply_with_other_request_id_is_ignored(ipc, peers, tmp_path):
    def fn(conn, line):
        conn.sendall(b'{"request_id": 99, "error": "success", "data": "wrong"}\n')
        conn.sendall(b'{"request_id": 1, "error": "success", "data": "right"}\n')
    peers(tmp_path / "radio.sock", fn)
    assert ipc.mpv("get_property", "x") == "right"


def test_mpv_hung_peer_times_out(ipc, peers, tmp_path):
    peers(tmp_path / "radio.sock", stall())
    t0 = time.monotonic()
    with pytest.raises(OSError):
        ipc.mpv_raw("get_property", "pause", timeout=0.3)
    assert time.monotonic() - t0 < 2


# --- radio_probe ----------------------------------------------------------------------

def test_probe_unreachable_is_none(ipc):
    assert ipc.radio_probe() is None


def test_probe_idle(ipc, peers, tmp_path):
    peers(tmp_path / "radio.sock", mpv_player({"idle-active": True}))
    assert ipc.radio_probe() == {"idle": True}


def test_probe_playing_collects_properties(ipc, peers, tmp_path):
    props = {"idle-active": False, "pause": False, "audio-codec-name": "mp3",
             "audio-bitrate": 128000, "paused-for-cache": False,
             "metadata": {"icy-title": "Song"}, "audio-params": {"samplerate": 44100}}
    peers(tmp_path / "radio.sock", mpv_player(props))
    got = ipc.radio_probe()
    assert got["idle"] is False
    assert got["metadata"] == {"icy-title": "Song"}
    assert got["audio-bitrate"] == 128000
    assert got["audio-params"] == {"samplerate": 44100}


def test_probe_missing_properties_are_none(ipc, peers, tmp_path):
    peers(tmp_path / "radio.sock", mpv_player({"idle-active": False}))
    got = ipc.radio_probe()
    assert got["idle"] is False and got["metadata"] is None and got["pause"] is None


def test_probe_idle_query_error_means_unreachable(ipc, peers, tmp_path):
    peers(tmp_path / "radio.sock", mpv_player({}))     # idle-active -> property unavailable
    assert ipc.radio_probe() is None


def test_probe_stops_asking_when_player_dies_midway(ipc, peers, tmp_path):
    """A player that stops answering costs about 2 timeouts, not one per property."""
    n = {"c": 0}

    def fn(conn, line):
        n["c"] += 1
        if n["c"] == 1:
            return mpv_player({"idle-active": False})(conn, line)
        time.sleep(30)                                  # hung from the second query on
    peers(tmp_path / "radio.sock", fn)
    t0 = time.monotonic()
    got = ipc.radio_probe(timeout=0.3)
    elapsed = time.monotonic() - t0
    assert got == {"idle": False}
    assert elapsed < 1.5, elapsed
    assert n["c"] == 2                                  # it did not ask for the other five
