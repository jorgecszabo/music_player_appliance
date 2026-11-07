"""panda-wifi: the parts that handle input from the air and from the web page."""
import json
import os
import stat

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.conftest import load_module


@pytest.fixture(scope="module")
def wifi():
    return load_module("panda-wifi.py")


# --- validation --------------------------------------------------------------------------------------

@pytest.mark.parametrize("ssid", ["a", "x" * 32, "Café", "日本語", "with space", "😀", "q\"uote", "back\\slash", "new\nline",
                                  "hash#tag", "network={", "}"])
def test_ssid_valid_up_to_32_bytes(wifi, ssid):
    if len(ssid.encode()) <= 32:
        assert wifi.check_ssid(ssid) == ssid


@pytest.mark.parametrize("ssid", ["", "x" * 33, "日" * 11, None, 5, ["a"], b"a", {"a": 1}])
def test_ssid_invalid(wifi, ssid):
    with pytest.raises(ValueError):
        wifi.check_ssid(ssid)


@pytest.mark.parametrize("pw", ["12345678", "a" * 63, "pass word!", "x" * 8, "0" * 64, "A" * 64])
def test_password_valid(wifi, pw):
    assert wifi.check_password(pw) == pw


@pytest.mark.parametrize("pw", ["", "short", "g" * 65, "tab\there1", "new\nline12", "naïve1234", "日本語日本語日本語",
                                None, 12345678, ["abcdefgh"], "g" * 64, "ab\x00cdefgh", "\x7fabcdefgh"])
def test_password_invalid(wifi, pw):
    with pytest.raises(ValueError):
        wifi.check_password(pw)


def test_psk_matches_the_ieee_test_vector(wifi):
    # IEEE 802.11i-2004 Annex H.4.1: passphrase "password", SSID "IEEE"
    assert wifi.psk_hash("IEEE", "password") == "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e"
    assert wifi.psk_hash("anything", "AB" * 32) == ("ab" * 32)


def test_entries_never_keep_a_clear_password_unless_wpa3(wifi):
    e = wifi.make_entry("Home", "supersecret1")
    assert "password" not in e and "supersecret1" not in json.dumps(e) and len(e["psk"]) == 64
    e3 = wifi.make_entry("Home3", "supersecret1", sae=True)
    assert e3["sae_password"] == "supersecret1"
    with pytest.raises(ValueError):
        wifi.make_entry("Home3", "ab" * 32, sae=True)           # SAE needs the password itself
    assert "psk" not in wifi.make_entry("Open", None)


# --- wpa_supplicant configuration: nothing in an SSID or password can add a line -----------------------

HOSTILE = ["a\nb", 'q"uote', "back\\slash", "}\nnetwork={", "ssid=evil", "# comment", "x\ty", "\r\n", "日本語", "😀",
           "ctrl_interface=DIR=/tmp GROUP=0", "\x00", "a b"]


def parse_networks(text):
    """A strict reader of the file format we write: top-level key=value lines and network={...} blocks."""
    top, nets, cur = [], [], None
    for line in text.split("\n")[:-1]:
        if cur is None:
            if line == "network={":
                cur = []
            else:
                assert "=" in line and not line.startswith(("\t", " ")), repr(line)
                top.append(line)
        elif line == "}":
            nets.append(cur)
            cur = None
        else:
            assert line.startswith("\t") and "=" in line, repr(line)
            cur.append(line[1:])
    assert cur is None, "unterminated network block"
    return top, nets


@pytest.mark.parametrize("ssid", HOSTILE)
def test_wpa_conf_ssid_is_hex_encoded(wifi, ssid):
    if not 1 <= len(ssid.encode()) <= 32:
        return
    e = wifi.make_entry(ssid, "password123")
    top, nets = parse_networks(wifi.wpa_conf_text({"country": "DE", "networks": [e]}))
    assert len(nets) == 1
    assert f"ssid={ssid.encode().hex()}" in nets[0]
    assert not any(l.startswith("ctrl_interface") for l in nets[0])


@pytest.mark.parametrize("pw", ['pass"word1', "pass\\word1", "pass=word1", "pass#word1", "a b c d e f g", "}}}}}}}}",
                               "'; rm -rf /;", '"""""""""'])
def test_wpa_conf_sae_password_cannot_break_out(wifi, pw):
    e = wifi.make_entry("Net", pw, sae=True)
    top, nets = parse_networks(wifi.wpa_conf_text({"country": "", "networks": [e]}))
    assert len(nets) == 1
    sae = [l for l in nets[0] if l.startswith("sae_password=")]
    assert len(sae) == 1 and sae[0] == f'sae_password="{pw}"'


@settings(max_examples=300, deadline=None)
@given(ssid=st.text(min_size=1, max_size=32), pw=st.text(min_size=1, max_size=80))
def test_fuzz_config_stays_well_formed(wifi, ssid, pw):
    try:
        e = wifi.make_entry(ssid, pw, hidden=True, sae=len(pw) % 2 == 0)
    except ValueError:
        return                                                   # refused: fine
    top, nets = parse_networks(wifi.wpa_conf_text({"country": "DE", "networks": [e, wifi.make_entry("Two", None)]}))
    assert len(nets) == 2


def test_wpa_conf_priorities_follow_list_order_and_country(wifi):
    a, b = wifi.make_entry("A", "password123"), wifi.make_entry("B", None)
    top, nets = parse_networks(wifi.wpa_conf_text({"country": "DE", "networks": [a, b]}))
    assert "country=DE" in top
    assert "priority=2" in nets[0] and "priority=1" in nets[1]
    assert "key_mgmt=NONE" in nets[1]


# --- the boot-partition import --------------------------------------------------------------------------

def test_import_single_and_list(wifi):
    c, nets = wifi.parse_import({"country": "de", "ssid": "Home", "password": "password123"})
    assert c == "DE" and nets[0]["ssid"] == "Home" and "password" not in nets[0]
    c, nets = wifi.parse_import({"networks": [{"ssid": "A"}, {"ssid": "B", "password": "password123", "hidden": True}]})
    assert [n["ssid"] for n in nets] == ["A", "B"] and nets[1]["hidden"] is True


@pytest.mark.parametrize("obj", [[], "x", 5, None, {}, {"networks": "x"}, {"networks": [5]}, {"country": "DEU", "ssid": "a"},
                                 {"country": "1", "ssid": "a"}, {"ssid": ""}, {"ssid": "a", "password": "short"},
                                 {"networks": [{"ssid": "a", "psk": "nothex"}]}])
def test_import_rejects(wifi, obj):
    with pytest.raises(ValueError):
        wifi.parse_import(obj)


def test_import_already_converted_entries_roundtrip(wifi):
    _, nets = wifi.parse_import({"ssid": "Home", "password": "password123"})
    _, again = wifi.parse_import({"networks": nets})
    assert again[0]["psk"] == nets[0]["psk"]


def test_saved_config_is_private(wifi, tmp_path):
    path = str(tmp_path / "wifi.json")
    wifi.write_conf({"country": "DE", "adapter": "auto", "networks": [wifi.make_entry("A", "password123")]}, path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# --- scan results -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (r"a\x20b", "a b"), (r"tab\tx", "tab\tx"), (r"nl\nx", "nl\nx"), (r"back\\slash", "back\\slash"),
    (r"\xe6\x97\xa5", "日"), (r"\x", "x"), ("trailing\\", "trailing\\"), (r"\xZZ", "xZZ"), (r"\xff", "�"),
    ("", ""), ("plain", "plain"),
])
def test_unescape(wifi, raw, expected):
    assert wifi.unescape(raw) == expected


@pytest.mark.parametrize("flags,expected", [
    ("[WPA2-PSK-CCMP][ESS]", "wpa2"), ("[RSN-PSK-CCMP]", "wpa2"), ("[WPA-PSK-TKIP]", "wpa"),
    ("[RSN-SAE-CCMP]", "wpa3"), ("[RSN-PSK+SAE-CCMP]", "wpa2/wpa3"), ("[WPA2-EAP-CCMP]", "enterprise"),
    ("[WEP]", "wep"), ("[ESS]", "open"), ("", "open"), ("[OWE]", "owe"),
])
def test_security(wifi, flags, expected):
    assert wifi.security(flags) == expected


def test_fetch_results_merges_bssids_and_survives_hostile_ssids(wifi, monkeypatch):
    w = wifi.Wifi.__new__(wifi.Wifi)
    w.conf = {"networks": [{"ssid": "Home"}]}
    lines = ["bssid / frequency / signal level / flags / ssid",
             "aa:bb:cc:00:00:01\t2437\t-70\t[WPA2-PSK-CCMP][ESS]\tHome",
             "aa:bb:cc:00:00:02\t5180\t-50\t[WPA2-PSK-CCMP][ESS]\tHome",
             "aa:bb:cc:00:00:03\t2412\t-60\t[ESS]\t",                                # hidden: no name
             "aa:bb:cc:00:00:04\t2412\t-60\t[ESS]\t" + r"evil\x00name",             # NUL: dropped
             "aa:bb:cc:00:00:05\t2412\t-61\t[ESS]\t" + r"ctrl\nnetwork={",
             "short\tline"]
    monkeypatch.setattr(w, "wpa_req", lambda cmd, timeout=5: "\n".join(lines), raising=False)
    w.fetch_results()
    by = {r["ssid"]: r for r in w.results}
    assert set(by) == {"Home", "ctrl\nnetwork={"}
    assert by["Home"]["signal"] == -50 and by["Home"]["bands"] == ["2.4 GHz", "5 GHz"] and by["Home"]["saved"]


# --- the control socket ---------------------------------------------------------------------------------------

def test_control_rejects_unknown_and_malformed_commands(wifi):
    w = wifi.Wifi.__new__(wifi.Wifi)
    for req in [{}, {"cmd": "nope"}, {"cmd": None}, {"cmd": ["scan"]}]:
        with pytest.raises(ValueError):
            w.control(req)


@pytest.mark.parametrize("req", [{"cmd": "forget"}, {"cmd": "forget", "ssid": 5}, {"cmd": "forget", "ssid": ""},
                                 {"cmd": "connect"}, {"cmd": "connect", "ssid": ["a"]},
                                 {"cmd": "settings", "country": "deu"}, {"cmd": "settings", "country": 5},
                                 {"cmd": "settings", "adapter": "../x"}])
def test_control_bad_arguments_are_value_errors(wifi, req):
    w = wifi.Wifi.__new__(wifi.Wifi)
    w.iface = "wlan0"
    w.results = []
    w.conf = {"networks": [], "country": "", "adapter": "auto"}
    import threading
    w.lock = threading.RLock()
    w.board = {}
    with pytest.raises((ValueError, TypeError, KeyError)):
        w.control(req)


def test_socket_server_survives_non_object_requests(wifi, tmp_path, monkeypatch):
    """A line that is JSON but not an object, or garbage, gets an error reply and the server lives on."""
    import panda_ipc
    sock = str(tmp_path / "wifi.sock")
    monkeypatch.setattr(wifi, "RUN", str(tmp_path))
    monkeypatch.setattr(wifi, "CONTROL_SOCK", sock)
    monkeypatch.setattr(wifi, "CONTROL_GROUP", "no-such-group-xyz")
    w = wifi.Wifi.__new__(wifi.Wifi)
    monkeypatch.setattr(w, "status", lambda: {"state": "fake"}, raising=False)
    w.serve()
    import socket
    for raw in (b"[1,2]\n", b"5\n", b"null\n", b'"x"\n', b"{\n", b"\xff\xfe\n", b"\n", b'{"cmd": ["x"]}\n'):
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(3)
            s.connect(sock)
            s.sendall(raw)
            reply = s.recv(4096)
        assert reply.endswith(b"\n") and "error" in json.loads(reply), (raw, reply)
    assert panda_ipc.line_call(sock, {"cmd": "status"}) == {"state": "fake"}
