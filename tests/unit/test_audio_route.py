"""audio-route.py: output choice, USB card setup, master volume (PipeWire and ALSA are stubbed)."""
import math

import pytest

from tests.conftest import load_module


@pytest.fixture(scope="module")
def ar():
    return load_module("audio-route.py")


@pytest.mark.parametrize("v,expected", [("0", 0), ("-6", -6), ("-60", -60), ("-100", -60), ("5", 0), ("x", 0), ("", 0), ("-3.5", 0)])
def test_usb_cap_db(ar, v, expected):
    assert ar.usb_cap_db({"USB_MAX_DB": v}) == expected
    assert ar.usb_cap_db({}) == 0


@pytest.mark.parametrize("v,expected", [("0.5", 0.5), ("2", 1.0), ("-1", 0.0), ("abc", 1.0), ("nan", 0.0), ("", 1.0)])
def test_master_volume(ar, v, expected):
    got = ar.master_volume({"MASTER_VOLUME": v})
    assert got == expected or (math.isnan(float(v)) and 0.0 <= got <= 1.0)
    assert ar.master_volume({}) == 1.0


def node(oid, name, bus="usb", cls="Audio/Sink", card=None, **extra):
    props = {"node.name": name, "media.class": cls, "device.bus": bus}
    if card:
        props["api.alsa.card.name"] = card
    props.update(extra)
    return {"id": oid, "props": props, "params": {}}


def test_usb_outputs_are_usb_alsa_sinks_oldest_first_and_not_the_board_card(ar):
    ns = [node(30, "alsa_output.usb-B"), node(20, "alsa_output.usb-A"), node(5, "alsa_output.platform-onboard", bus="platform"),
          node(40, "alsa_input.usb-C", cls="Audio/Source"), node(50, "bluez_output.x", bus="bluetooth"),
          node(60, "alsa_output.usb-board", card="Board"), node(70, "something-else")]
    got = ar.usb_outputs(ns, "Board")
    assert [n["id"] for n in got] == [20, 30]
    assert ar.usb_outputs([], "") == []


def test_usb_devices_filters_on_bus_and_api(ar):
    ds = [{"id": 3, "props": {"device.bus": "usb", "device.api": "alsa"}, "params": {}},
          {"id": 2, "props": {"device.bus": "usb", "device.api": "bluez5"}, "params": {}},
          {"id": 1, "props": {"device.bus": "pci", "device.api": "alsa"}, "params": {}},
          {"id": 4, "props": {"device.bus": "usb", "device.api": "alsa", "api.alsa.card.name": "Board"}, "params": {}}]
    assert [d["id"] for d in ar.usb_devices(ds, "Board")] == [3]
    assert [d["id"] for d in ar.usb_devices(ds, "")] == [3, 4]


def test_set_profile_picks_the_best_available_for_auto(ar, monkeypatch):
    calls = []
    monkeypatch.setattr(ar, "run", lambda cmd, timeout=10: calls.append(cmd) or (0, ""))
    dev = {"id": 9, "props": {}, "params": {
        "EnumProfile": [{"index": 0, "name": "off", "priority": 0}, {"index": 1, "name": "output:analog-stereo", "priority": 6500},
                        {"index": 2, "name": "pro-audio", "priority": 1}, {"index": 3, "name": "output:iec958", "priority": 9999,
                                                                           "available": "no"}],
        "Profile": [{"name": "off"}]}}
    assert ar.set_profile(dev, "auto") is True
    assert calls == [["wpctl", "set-profile", "9", "1"]]
    dev["params"]["Profile"] = [{"name": "output:analog-stereo"}]
    calls.clear()
    assert ar.set_profile(dev, "auto") is False and calls == []                  # already there
    assert ar.set_profile(dev, "pro-audio") is True and calls == [["wpctl", "set-profile", "9", "2"]]
    calls.clear()
    assert ar.set_profile(dev, "no-such-profile") is False and calls == []


def test_set_profile_without_profiles_does_nothing(ar, monkeypatch):
    monkeypatch.setattr(ar, "run", lambda *a, **k: pytest.fail("must not run anything"))
    assert ar.set_profile({"id": 1, "props": {}, "params": {}}, "auto") is False


AMIXER_CONTENTS = """numid=3,iface=MIXER,name='PCM Playback Volume'
  ; type=INTEGER,access=rw---R--,values=2,min=0,max=87,step=0
  : values=87,87
  | dBminmax-min=-65.25dB,max=0.00dB
numid=4,iface=MIXER,name='Speaker Playback Volume'
  ; type=INTEGER,access=rw---R--,values=2,min=0,max=100,step=0
  | dBscale-min=-50.00dB,step=0.50dB,mute=0
numid=5,iface=MIXER,name='Cheap Playback Volume'
  ; type=INTEGER,access=rw---R--,values=1,min=0,max=999,step=0
  | dBminmax-min=0.00dB,max=3.90dB
numid=6,iface=MIXER,name='Mic Capture Volume'
  ; type=INTEGER,values=1,min=0,max=10,step=0
"""
SCONTENTS = """Simple mixer control 'PCM',0
  Capabilities: pvolume pswitch
Simple mixer control 'Speaker',0
  Capabilities: pvolume
Simple mixer control 'Cheap',0
  Capabilities: pvolume
Simple mixer control 'Mic',0
  Capabilities: cvolume
"""


def test_db_ranges_read_the_tlv(ar, monkeypatch):
    monkeypatch.setattr(ar, "run", lambda cmd, timeout=10: (0, AMIXER_CONTENTS))
    r = ar.db_ranges("1")
    assert r["PCM"] == (-65.25, 0.0) and r["Speaker"] == (-50.0, 0.0) and r["Cheap"] == (0.0, 3.9) and "Mic" not in r


def test_init_mixer_uses_0db_only_for_believable_ranges(ar, monkeypatch):
    calls = []

    def fake(cmd, timeout=10):
        if cmd[3:] == ["contents"]:
            return 0, AMIXER_CONTENTS
        if cmd[3:] == ["scontents"]:
            return 0, SCONTENTS
        calls.append(cmd)
        return 0, ""
    monkeypatch.setattr(ar, "run", fake)
    done = ar.init_mixer("1")
    assert "PCM,0 0dB" in done and "Speaker,0 0dB" in done and "Cheap,0 100%" in done
    assert not any("Mic" in " ".join(c) for c in calls)
    assert ["amixer", "-q", "-c", "1", "sset", "PCM,0", "unmute"] in calls


def test_init_mixer_when_amixer_fails(ar, monkeypatch):
    monkeypatch.setattr(ar, "run", lambda cmd, timeout=10: (1, ""))
    assert ar.init_mixer("1") == []


# --- main() ---------------------------------------------------------------------------------------------------------

def run_main(ar, monkeypatch, nodes, devices=(), board=None, audio=None, argv=()):
    calls = []
    monkeypatch.setattr(ar, "shell_conf", lambda p: dict(board or {}) if "board" in p else dict(audio or {}))
    monkeypatch.setattr(ar, "dump", lambda: (nodes, list(devices)))
    monkeypatch.setattr(ar, "run", lambda cmd, timeout=10: calls.append(cmd) or (0, ""))
    monkeypatch.setattr(ar, "init_mixer", lambda card: ["PCM,0 0dB"])
    monkeypatch.setattr(ar.sys, "argv", ["audio-route.py", *argv])
    return ar.main(), calls


EQ = node(10, "panda-eq.output", bus="", cls="Stream/Output/Audio")
SINK = node(11, "panda-eq", bus="", cls="Audio/Sink")
USB = node(20, "alsa_output.usb-X", card="USB", **{"api.alsa.card": "2"})


def test_usb_selected_and_present_targets_the_usb_sink(ar, monkeypatch):
    rc, calls = run_main(ar, monkeypatch, [EQ, USB], audio={"OUTPUT": "usb"})
    assert rc == 0 and ["pw-metadata", "10", "target.object", "alsa_output.usb-X", "Spa:String"] in calls


def test_usb_selected_but_absent_changes_nothing(ar, monkeypatch):
    rc, calls = run_main(ar, monkeypatch, [EQ], audio={"OUTPUT": "usb"})
    assert rc == 0 and not any(c[0] == "pw-metadata" for c in calls)


def test_onboard_clears_the_target(ar, monkeypatch):
    rc, calls = run_main(ar, monkeypatch, [EQ, USB], audio={"OUTPUT": "onboard"})
    assert ["pw-metadata", "-d", "10", "target.object"] in calls


def test_no_tone_filter_is_reported(ar, monkeypatch):
    rc, calls = run_main(ar, monkeypatch, [USB], audio={"OUTPUT": "usb"})
    assert rc == 1 and not any(c[0] == "pw-metadata" for c in calls)


def test_the_usb_volume_cap_is_applied_on_the_cubic_scale(ar, monkeypatch):
    rc, calls = run_main(ar, monkeypatch, [EQ, USB], audio={"OUTPUT": "onboard", "USB_MAX_DB": "-6"})
    v = (10 ** (-6 / 20)) ** (1 / 3)
    assert ["wpctl", "set-volume", "20", f"{v:.4f}"] in calls


def test_master_volume_is_restored_only_with_the_flag(ar, monkeypatch):
    rc, calls = run_main(ar, monkeypatch, [EQ, SINK], audio={"MASTER_VOLUME": "0.4"})
    assert not any(c[:2] == ["wpctl", "set-volume"] and c[2] == "11" for c in calls)
    rc, calls = run_main(ar, monkeypatch, [EQ, SINK], audio={"MASTER_VOLUME": "0.4"}, argv=["--master"])
    assert ["wpctl", "set-volume", "11", "0.400"] in calls


def test_a_profile_switch_ends_the_run_early(ar, monkeypatch):
    dev = {"id": 9, "props": {"device.bus": "usb", "device.api": "alsa"},
           "params": {"EnumProfile": [{"index": 1, "name": "pro-audio", "priority": 1}], "Profile": [{"name": "off"}]}}
    rc, calls = run_main(ar, monkeypatch, [EQ, USB], devices=[dev], audio={"OUTPUT": "usb"})
    assert rc == 0 and ["wpctl", "set-profile", "9", "1"] in calls and not any(c[0] == "pw-metadata" for c in calls)
