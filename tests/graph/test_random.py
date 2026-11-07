"""I. Randomised sequences against the live rig, checked against a small model of the policy.

Hypothesis generates sequences of user actions and faults; after each step the graph must have
settled into what the model predicts. A failing sequence is shrunk to a minimal, replayable list.

Model (the arbiter's documented policy): a source that starts while others play stops them, by
pausing (Radio, Spotify, Bluetooth: they stay stopped) or by muting (AirPlay: it keeps its stream);
a muted source comes back when it is the only stream left.
"""
import os

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test

from rig import RIG_RUN, SOURCES, wait_until

EXAMPLES = int(os.environ.get("GRAPH_FUZZ_EXAMPLES", 5))
STEPS = int(os.environ.get("GRAPH_FUZZ_STEPS", 6))


def make_machine(rig):
    class Model(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.playing = []            # sources with a running stream, oldest first
            self.muted = set()
            self.log = []
            for src in SOURCES:
                try:
                    rig.stop_source(src)
                except OSError:
                    pass
            wait_until(lambda: not rig.running(), 15, "a clean start", rig=rig)
            for f in ("ignore-pause",):
                try:
                    os.unlink(f"{RIG_RUN}/fake/soloist/{f}")
                except OSError:
                    pass

        def expected(self):
            return [s for s in self.playing if s not in self.muted]

        @rule(src=st.sampled_from(SOURCES))
        def start(self, src):
            self.log.append(("start", src))
            rig.play(src)
            rig.wait_running(src)
            for other in list(self.playing):
                if other == src:
                    continue
                if other == "AirPlay":
                    self.muted.add("AirPlay")
                else:
                    self.playing.remove(other)
            if src in self.playing:
                self.playing.remove(src)
            self.playing.append(src)
            self.muted.discard(src)
            self.settle()

        @rule(src=st.sampled_from(SOURCES))
        def stop(self, src):
            self.log.append(("stop", src))
            if src not in self.playing:
                return
            rig.stop_source(src)
            wait_until(lambda: src not in rig.running(), 10, f"{src} stopped", rig=rig)
            self.playing.remove(src)
            self.muted.discard(src)
            if self.playing == ["AirPlay"]:
                self.muted.discard("AirPlay")
            self.settle()

        def settle(self):
            rig.settle(quiet=1.2, timeout=30)

        @invariant()
        def audible_matches_the_model(self):
            if not self.log:
                return
            got = rig.audible()
            assert got == sorted(self.expected()), f"after {self.log}: graph audible={got} model={self.expected()}\n{rig.describe()}"
            assert len(got) <= 1

        def teardown(self):
            for src in SOURCES:
                try:
                    rig.stop_source(src)
                except OSError:
                    pass
    return Model


def test_random_sequences_follow_the_policy(rig):
    run_state_machine_as_test(
        make_machine(rig),
        settings=settings(max_examples=EXAMPLES, stateful_step_count=STEPS, deadline=None, database=None,
                          suppress_health_check=list(HealthCheck)))
