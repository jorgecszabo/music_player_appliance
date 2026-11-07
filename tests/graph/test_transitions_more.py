"""B (continued). Triples, repeats and rapid alternation."""
import itertools
import os

import pytest

from rig import SOURCES, wait_until


@pytest.mark.parametrize("order", list(itertools.permutations(SOURCES, 3)), ids=lambda o: "-".join(o))
def test_three_sources_only_the_last_is_audible(rig, order):
    for src in order:
        rig.play(src)
        rig.wait_running(src)
    wait_until(lambda: rig.audible() == [order[-1]], 20, f"only {order[-1]} audible", rig=rig)
    rig.settle()
    assert rig.audible() == [order[-1]]


@pytest.mark.parametrize("src", ["Radio", "AirPlay", "Spotify", "Bluetooth"])
def test_restarting_the_same_source_does_not_interrupt_itself(rig, src):
    rig.play(src)
    rig.wait_running(src)
    rig.play(src)                                                    # loadfile again (a new track)
    rig.settle()
    assert rig.audible() == [src]
    assert not any(d["action"] == "stop" and d["source"] == src for d in rig.decisions()), rig.decisions()
    rig.stop_source(src)
    wait_until(lambda: src not in rig.running(), 10, f"{src} stopped", rig=rig)
    rig.play(src)
    rig.wait_running(src)
    wait_until(lambda: rig.audible() == [src], 10, f"{src} audible again", rig=rig)


def threads_and_fds(rig):
    pid = rig.services["speaker-arbiter"].proc.pid
    with open(f"/proc/{pid}/status") as f:
        threads = int(next(l for l in f if l.startswith("Threads:")).split()[1])
    return threads, len(os.listdir(f"/proc/{pid}/fd"))


def test_rapid_alternation_ends_consistent_and_does_not_leak(rig):
    """Sources taking over from each other 2-3 times a second. (A pause request takes a few hundred
    milliseconds to execute: restarting a source faster than that can have the late pause hit the
    new run. See TESTING.md, known limitations.)"""
    import time
    seq = ["Radio", "AirPlay", "Spotify"]
    rig.play("Radio")
    rig.wait_running("Radio")
    rig.settle()
    t0, f0 = threads_and_fds(rig)
    last = None
    for i in range(30):
        last = seq[i % 3]
        rig.play(last)
        time.sleep(0.4)
    rig.wait_running(last)
    rig.settle(quiet=2, timeout=40)
    assert rig.audible() == [last], rig.describe()
    t1, f1 = threads_and_fds(rig)
    assert t1 <= t0 + 4 and f1 <= f0 + 4, (t0, t1, f0, f1)
    assert rig.arbiter_tracebacks() == []


def test_alternation_with_bluetooth_at_human_speed(rig):
    """The AVRCP pause takes about a second (a D-Bus round trip): a phone is not restarted faster."""
    import time
    seq = ["Bluetooth", "Radio", "Bluetooth", "AirPlay", "Bluetooth", "Spotify", "Bluetooth"]
    for src in seq:
        rig.play(src)
        rig.wait_running(src)
        time.sleep(2.0)
    rig.settle(quiet=2, timeout=40)
    assert rig.audible() == ["Bluetooth"], rig.describe()


def test_two_sources_starting_together_leave_exactly_one_audible(rig):
    import threading
    for _ in range(5):
        a = threading.Thread(target=rig.play, args=("Spotify",))
        b = threading.Thread(target=rig.play, args=("AirPlay",))
        a.start()
        b.start()
        a.join()
        b.join()
        wait_until(lambda: len(rig.audible()) == 1 and len(rig.running()) >= 1, 20, "exactly one audible", rig=rig)
        rig.settle()
        assert len(rig.audible()) == 1, rig.describe()
        for s in ("Spotify", "AirPlay"):
            rig.stop_source(s)
        wait_until(lambda: not rig.running(), 10, "all stopped", rig=rig)
