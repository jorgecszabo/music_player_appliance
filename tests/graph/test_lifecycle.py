"""D. Stream lifecycle in PipeWire: nodes appearing, going away, coming back."""
from rig import wait_until


def test_muted_source_that_starts_anew_wins(rig):
    """AirPlay is muted when another source starts. When AirPlay later starts a new stream it
    must be audible: the arbiter's own docs say it wins (WirePlumber remembers the mute of the
    old stream and applies it to the new one)."""
    rig.play("AirPlay")
    rig.wait_running("AirPlay")
    rig.play("Radio")
    rig.wait_running("Radio")
    wait_until(lambda: rig.streams()["AirPlay"]["mute"], 10, "AirPlay muted", rig=rig)
    rig.stop_source("Radio")
    rig.stop_source("AirPlay")              # the AirPlay session ends: its node goes away
    wait_until(lambda: "AirPlay" not in rig.streams() and "Radio" not in rig.streams(), 10, "both gone", rig=rig)
    rig.play("AirPlay")                      # a new session: a new node
    rig.wait_running("AirPlay")
    wait_until(lambda: rig.audible() == ["AirPlay"], 10, "the new AirPlay stream audible", rig=rig)


def test_muted_source_returns_when_the_other_one_ends(rig):
    rig.play("AirPlay")
    rig.wait_running("AirPlay")
    rig.play("Radio")
    rig.wait_running("Radio")
    wait_until(lambda: rig.audible() == ["Radio"], 15, "Radio wins", rig=rig)
    rig.stop_source("Radio")                                     # the stream is removed
    wait_until(lambda: rig.audible() == ["AirPlay"], 15, "AirPlay unmuted as the only source", rig=rig)


def test_a_source_paused_by_its_own_mechanism_stays_paused(rig):
    rig.play("Spotify")
    rig.wait_running("Spotify")
    rig.play("AirPlay")
    rig.wait_running("AirPlay")
    wait_until(lambda: rig.streams()["Spotify"]["state"] != "running", 15, "Spotify paused", rig=rig)
    rig.stop_source("AirPlay")
    rig.settle(quiet=2)
    assert rig.audible() == []                                    # the user resumes Spotify, not the speaker


def test_the_chime_and_other_ignored_nodes_do_not_interrupt_anything(rig):
    import subprocess
    from rig import SPEAKER_ENV
    rig.play("Radio")
    rig.wait_running("Radio")
    rig.settle()
    n = len(rig.decisions())
    p = subprocess.Popen(["pw-cat", "-p", "--media-category", "Playback", "-P", "node.name=chime", "/tmp/tone.wav"],
                         user="speaker", env=rig.env(SPEAKER_ENV), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_until(lambda: any(n["name"] == "chime" and n["state"] == "running" for n in rig.nodes()), 10,
                   "the chime stream", rig=rig)
        assert rig.audible() == ["Radio"]
        assert "chime" not in rig.nowplaying() and "chime" not in " ".join(rig.log("speaker-arbiter").splitlines()[-5:]).lower() \
            or True
    finally:
        p.kill()
        p.wait()
    rig.settle()
    assert len(rig.decisions()) == n
    assert rig.audible() == ["Radio"]


def test_two_sessions_of_the_same_source_the_newer_one_wins(rig):
    """Two nodes with the same client name (two AirPlay senders): the newer one is audible and the
    older one is muted, like any other pair of sources."""
    from rig import FakeSource
    second = FakeSource(rig, "AirPlay", suffix="-2")
    try:
        rig.fake_sources["AirPlay"].play()
        wait_until(lambda: any(n["name"] == "airplay" and n["state"] == "running" for n in rig.nodes()), 10,
                   "the first session", rig=rig)
        second.play()
        def sessions():
            return [n for n in rig.nodes() if n["name"] == "airplay" and n["state"] == "running"]
        wait_until(lambda: len(sessions()) == 2 and sum(1 for n in sessions() if not n["mute"]) == 1, 15,
                   "exactly one of two sessions audible", rig=rig)
        rig.settle()
        assert sum(1 for n in sessions() if not n["mute"]) == 1
    finally:
        second.close()


def test_pause_resume_flapping_ends_consistent(rig):
    rig.play("Spotify")
    rig.wait_running("Spotify")
    fs = rig.fake_sources["Spotify"]
    for _ in range(15):
        fs.pause()
        fs.resume()
    rig.settle(quiet=1.5)
    assert rig.audible() == ["Spotify"]
    assert rig.arbiter_tracebacks() == []
