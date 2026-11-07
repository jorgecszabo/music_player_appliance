"""C. A pause that does not work: the arbiter falls back to muting, and never gives up."""
import os

from rig import RIG_RUN, wait_until


def test_spotify_ignores_pause_is_muted_then_unmuted(rig):
    open(f"{RIG_RUN}/fake/soloist/ignore-pause", "w").close()
    os.chmod(f"{RIG_RUN}/fake/soloist/ignore-pause", 0o644)
    rig.play("Spotify")
    rig.wait_running("Spotify")
    rig.play("AirPlay")
    rig.wait_running("AirPlay")
    wait_until(lambda: rig.audible() == ["AirPlay"], 15, "AirPlay audible, Spotify muted", rig=rig)
    assert any(c.startswith("pause") for c in rig.calls("soloist"))
    assert rig.streams()["Spotify"]["state"] == "running" and rig.streams()["Spotify"]["mute"]
    # AirPlay ends: Spotify is the only stream again and must not stay muted (I3).
    rig.stop_source("AirPlay")
    wait_until(lambda: rig.audible() == ["Spotify"], 15, "Spotify unmuted when alone", rig=rig)


def _bluez_mode(mode):
    p = f"{RIG_RUN}/fake/bluez/mode"
    open(p, "w").write(mode)
    os.chmod(p, 0o644)


def test_bluetooth_pause_fails_is_muted(rig):
    _bluez_mode("fail")
    rig.play("Bluetooth")
    rig.wait_running("Bluetooth")
    rig.play("Radio")
    rig.wait_running("Radio")
    wait_until(lambda: rig.audible() == ["Radio"], 15, "Radio audible, Bluetooth muted", rig=rig)
    assert "Pause" in rig.calls("bluez")


def test_bluetooth_has_no_media_player_is_muted(rig):
    _bluez_mode("noplayer")
    rig.play("Bluetooth")
    rig.wait_running("Bluetooth")
    rig.play("Spotify")
    rig.wait_running("Spotify")
    wait_until(lambda: rig.audible() == ["Spotify"], 15, "Spotify audible, Bluetooth muted", rig=rig)


def test_bluetooth_pause_hangs_does_not_leave_two_sources_audible(rig):
    """BlueZ never answers the AVRCP Pause. The arbiter's own timeout is 15 s; afterwards the
    older source must still be silenced, and the arbiter must keep working meanwhile."""
    _bluez_mode("hang")
    rig.play("Bluetooth")
    rig.wait_running("Bluetooth")
    rig.play("Radio")
    rig.wait_running("Radio")
    # Meanwhile a third source is handled normally: the hung pause blocks nothing else.
    rig.play("AirPlay")
    rig.wait_running("AirPlay")
    wait_until(lambda: rig.audible() == ["AirPlay"], 30, "AirPlay alone audible after the hang", rig=rig)


def test_mpv_hung_when_the_arbiter_stops_it_does_not_block_the_arbiter(rig):
    """A hung player cannot be told to stop (nor muted: its client side applies the mute). The
    arbiter must carry on: the new source plays, a third source is still arbitrated, and when the
    player wakes up it executes the stop it was sent."""
    rig.play("Radio")
    rig.wait_running("Radio")
    rig.hang("speaker-radio")
    try:
        rig.play("Spotify")
        rig.wait_running("Spotify")
        rig.play("AirPlay")                              # arrives while the arbiter is still waiting on mpv
        rig.wait_running("AirPlay")
        wait_until(lambda: any(c.startswith("pause") for c in rig.calls("soloist")), 10,
                   "Spotify paused by the second arbitration", rig=rig)
    finally:
        rig.unhang("speaker-radio")
    wait_until(lambda: "Radio" not in rig.streams(), 10, "the stop reached mpv once it woke up", rig=rig)


def test_each_start_stops_the_old_source_once(rig):
    rig.play("Spotify")
    rig.wait_running("Spotify")
    rig.play("Radio")
    rig.wait_running("Radio")
    rig.settle(quiet=1.5)
    pauses = [c for c in rig.calls("soloist") if c.startswith("pause")]
    assert len(pauses) == 1, pauses
