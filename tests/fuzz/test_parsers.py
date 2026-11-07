"""Property-based tests of everything that parses data the speaker does not control."""
import base64
import json
import os
import tempfile

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.conftest import load_module
from tests.fakes.peer import reply_raw

EXAMPLES = int(os.environ.get("FUZZ_EXAMPLES", 200))
FAST = settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


@pytest.fixture(scope="module")
def arb_mod():
    m = load_module("panda-arbiter.py")
    m.NOWPLAYING = os.path.join(tempfile.mkdtemp(), "nowplaying.json")
    m.NOW = m.NowPlaying()
    return m


@pytest.fixture(scope="module")
def web():
    return load_module("panda-web.py")


@pytest.fixture(scope="module")
def wifi():
    return load_module("panda-wifi.py")


json_value = st.recursive(st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=40),
                          lambda c: st.lists(c, max_size=4) | st.dictionaries(st.text(max_size=8), c, max_size=5),
                          max_leaves=15)


# --- AirPlay metadata pipe ------------------------------------------------------------------------------------

def item(code, payload):
    return (f"<item><type>636f7265</type><code>{code.encode().hex()}</code><length>{len(payload)}</length>"
            f'<data encoding="base64">{payload}</data></item>')


pieces = st.one_of(
    st.text(max_size=60),
    st.builds(item, st.sampled_from(["minm", "asar", "asal", "snua", "clip", "prgr", "pbeg", "pfls", "prsm", "pend", "xxxx"]),
              st.one_of(st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/= \n", max_size=80),
                        st.binary(max_size=40).map(lambda b: base64.b64encode(b).decode()))),
    st.sampled_from(["<item>", "</item>", "<data encoding=\"base64\">", "</data>", "<type>", "<code>", "<length>"]),
)


@FAST
@given(chunks=st.lists(pieces, max_size=12), size=st.integers(min_value=1, max_value=40))
def test_airplay_stream_never_raises(arb_mod, chunks, size):
    text = "".join(chunks)
    arb_mod.airplay_stream(iter([text[i:i + size] for i in range(0, len(text), size)]))
    for source in arb_mod.NOW.data.values():
        json.dumps(source)                                   # whatever ends up there is serialisable
        for k in ("title", "artist", "album", "client", "client_ip"):
            assert len(str(source.get(k, ""))) <= 300


@FAST
@given(text=st.text(max_size=80))
def test_airplay_progress_never_raises(arb_mod, text):
    arb_mod.airplay_progress(text)
    for source in arb_mod.NOW.data.values():
        pos = source.get("pos", 0)
        assert pos >= 0 and pos == pos


# --- Spotify / generic number fields -------------------------------------------------------------------------------

@FAST
@given(dicts=st.lists(json_value, max_size=3), keys=st.lists(st.text(max_size=10), min_size=1, max_size=3))
def test_ms_field_never_raises_and_returns_a_non_negative_number_or_none(arb_mod, dicts, keys):
    out = arb_mod.ms_field(*dicts, *keys)
    assert out is None or (isinstance(out, (int, float)) and out >= 0)


@FAST
@given(props=st.dictionaries(st.sampled_from(["node.name", "application.name", "media.class"]),
                             st.text(max_size=30) | st.none() | st.integers(), max_size=3))
def test_source_of_never_raises(arb_mod, props):
    try:
        arb_mod.source_of(props)
    except (TypeError, AttributeError):
        pytest.fail(f"source_of crashed on {props!r}")


@FAST
@given(obj=json_value)
def test_arbiter_update_survives_any_json_object(arb_mod, obj):
    a = arb_mod.Arbiter()
    a.schedule_route = lambda master=False: None
    if isinstance(obj, dict):
        try:
            a.update(obj)
        except (TypeError, AttributeError, KeyError):
            pytest.fail(f"update crashed on {obj!r}")


# --- panda-web: radio-browser data and mpv replies ------------------------------------------------------------------

@FAST
@given(st=json_value)
def test_station_summary_never_raises(web, st):
    if isinstance(st, dict):
        out = web.station_summary(st)
        assert isinstance(out["name"], str) and len(out["name"]) <= 120 and len(out["tags"]) <= 200
        json.dumps(out)


@FAST
@given(raw=st.binary(max_size=300))
def test_line_call_survives_any_reply(ipc, peers, tmp_path_factory, raw):
    d = tmp_path_factory.mktemp("s")
    ipc.BT_SOCK = str(d / "bt.sock")
    peers(d / "bt.sock", reply_raw(raw))
    out = ipc.bt_call({"cmd": "x"}, timeout=1)
    json.dumps(out)


# --- panda-wifi ---------------------------------------------------------------------------------------------------------

@FAST
@given(s=st.text(max_size=60))
def test_unescape_never_raises(wifi, s):
    out = wifi.unescape(s)
    assert isinstance(out, str)


@FAST
@given(lines=st.lists(st.tuples(st.text(max_size=18), st.integers(0, 7000).map(str), st.integers(-120, 0).map(str),
                                st.text(max_size=30), st.text(max_size=50)), max_size=8))
def test_fetch_results_survives_any_scan_output(wifi, lines):
    w = wifi.Wifi.__new__(wifi.Wifi)
    w.conf = {"networks": []}
    raw = "header\n" + "\n".join("\t".join(t).replace("\n", " ") for t in lines)
    w.wpa_req = lambda cmd, timeout=5: raw
    w.fetch_results()
    for r in w.results:
        assert r["ssid"] and "\x00" not in r["ssid"]
        json.dumps(r)


@FAST
@given(obj=json_value)
def test_wifi_import_never_crashes_unexpectedly(wifi, obj):
    try:
        wifi.parse_import(obj)
    except ValueError:
        pass
    except (TypeError, AttributeError, KeyError) as e:
        pytest.fail(f"parse_import crashed with {e!r} on {obj!r}")
