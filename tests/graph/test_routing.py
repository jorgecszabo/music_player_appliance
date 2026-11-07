"""E. Output routing: sinks and the tone filter appearing and going away."""
import subprocess
import time

from rig import SPEAKER_ENV, wait_until


def make_sink(rig, name):
    props = ("{ factory.name=support.null-audio-sink node.name=%s media.class=Audio/Sink "
             "audio.position=[FL,FR] object.linger=true }" % name)
    r = subprocess.run(["pw-cli", "create-node", "adapter", props], user="speaker", env=rig.env(SPEAKER_ENV),
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    return wait_until(lambda: next((n["id"] for n in rig.nodes() if n["name"] == name), None), 10, f"sink {name}", rig=rig)


def destroy(rig, oid):
    subprocess.run(["pw-cli", "destroy", str(oid)], user="speaker", env=rig.env(SPEAKER_ENV), capture_output=True, timeout=10)


def routes(rig):
    return rig.calls("route")


def test_a_new_output_triggers_one_routing_run_and_removal_another(rig):
    n = len(routes(rig))
    oid = make_sink(rig, "alsa_output.usb-test")
    wait_until(lambda: len(routes(rig)) == n + 1, 10, "one routing run", rig=rig)
    time.sleep(1.5)
    assert len(routes(rig)) == n + 1
    destroy(rig, oid)
    wait_until(lambda: len(routes(rig)) == n + 2, 10, "a routing run after removal", rig=rig)


def test_a_burst_of_output_events_is_one_routing_run(rig):
    n = len(routes(rig))
    ids = [make_sink(rig, f"alsa_output.burst{i}") for i in range(4)]
    wait_until(lambda: len(routes(rig)) >= n + 1, 10, "a routing run", rig=rig)
    time.sleep(1.5)
    assert len(routes(rig)) == n + 1, routes(rig)
    for oid in ids:
        destroy(rig, oid)
    time.sleep(1.5)


def test_the_tone_filter_restarting_restores_the_master_volume(rig):
    n = len(routes(rig))
    oid = make_sink(rig, "panda-eq.output")
    wait_until(lambda: any("--master" in c for c in routes(rig)[n:]), 10, "a routing run with --master", rig=rig)
    destroy(rig, oid)
    time.sleep(1.5)                                    # the removal schedules a run: let it finish


def test_other_nodes_do_not_trigger_routing(rig):
    n = len(routes(rig))
    rig.play("Radio")
    rig.wait_running("Radio")
    rig.stop_source("Radio")
    time.sleep(1.5)
    assert len(routes(rig)) == n
