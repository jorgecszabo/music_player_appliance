"""J. Soak: hours of random use must not grow the processes. Nightly only (GRAPH_SOAK_MINUTES)."""
import os
import random
import time

import pytest

from rig import SOURCES, wait_until

pytestmark = pytest.mark.soak
MINUTES = float(os.environ.get("GRAPH_SOAK_MINUTES", "1"))


def sample(rig):
    out = {}
    for name in ("speaker-arbiter", "panda-web", "speaker-radio", "speaker-pipewire"):
        pid = rig.services[name].proc.pid
        with open(f"/proc/{pid}/status") as f:
            rss = int(next(l for l in f if l.startswith("VmRSS:")).split()[1])
        out[name] = (rss, len(os.listdir(f"/proc/{pid}/fd")))
    return out


def test_random_use_does_not_leak(rig):
    from rig import api
    rnd = random.Random(1)
    wait_until(lambda: api("/api/status")[0] == 200, 20, "web", rig=rig)
    for _ in range(20):                                       # warm up
        rig.play(rnd.choice(SOURCES))
        time.sleep(0.3)
    rig.settle(timeout=40)
    base = sample(rig)
    end = time.monotonic() + MINUTES * 60
    while time.monotonic() < end:
        src = rnd.choice(SOURCES)
        rig.play(src)
        api("/api/status")
        time.sleep(rnd.uniform(0.2, 2))
        if rnd.random() < 0.3:
            rig.stop_source(rnd.choice(SOURCES))
    rig.settle(timeout=60)
    now = sample(rig)
    for name, (rss, fds) in now.items():
        b_rss, b_fds = base[name]
        assert rss < b_rss * 1.5 + 8192, (name, base[name], now[name])
        assert fds <= b_fds + 6, (name, base[name], now[name])
    assert rig.arbiter_tracebacks() == []
