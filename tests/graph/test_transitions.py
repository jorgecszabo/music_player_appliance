"""B. Source transition matrix: the last source to start wins; the older one is stopped by
its own mechanism."""
import itertools

import pytest

from rig import SOURCES, wait_until

# How each source is stopped when another starts, and how to see that it happened.
def stopped_by_mechanism(rig, src):
    if src == "Radio":
        return "Radio" not in rig.streams()                    # mpv stop removes the stream
    if src == "Spotify":
        return any(c.startswith("pause") for c in rig.calls("soloist"))
    if src == "Bluetooth":
        return "Pause" in rig.calls("bluez")
    if src == "AirPlay":
        return rig.streams().get("AirPlay", {}).get("mute") is True   # no remote control: muted
    raise AssertionError(src)


@pytest.mark.parametrize("first,second", [p for p in itertools.permutations(SOURCES, 2)],
                         ids=lambda v: v)
def test_last_source_wins(rig, first, second):
    rig.play(first)
    rig.wait_running(first)
    rig.play(second)
    rig.wait_running(second)
    wait_until(lambda: rig.audible() == [second], 15, f"only {second} audible", rig=rig)
    wait_until(lambda: stopped_by_mechanism(rig, first), 10, f"{first} stopped by its own mechanism", rig=rig)
    rig.settle()
    assert rig.audible() == [second]                           # I1, I2
