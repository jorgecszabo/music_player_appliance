"""F. Failure injection: any one service dying, hanging or misbehaving must leave a system that
recovers without a reboot (I6), and an API that answers quickly (I8)."""
import os
import time

from rig import api, wait_until


def radio_status():
    return api("/api/status")[1]["radio"]


def test_radio_dead_is_reported_in_plain_words_and_restart_brings_it_back(rig):
    svc = rig.services["speaker-radio"]
    svc.respawn = False                                  # hold the supervisor back to see the outage
    try:
        rig.kill("speaker-radio")
        wait_until(lambda: radio_status()["available"] is False, 10, "radio unavailable", rig=rig)
        st = radio_status()
        assert st["state"] == "stopped" and st["message"], st
        assert "mpv" not in st["message"].lower() and "speaker-radio" not in st["message"]
        code, _ = api("/api/service", {"name": "speaker-radio"})      # the Restart button
        assert code == 200
        svc.wanted = True
    finally:
        svc.respawn = True
    wait_until(lambda: radio_status().get("available") is True, 25, "radio back after Restart", rig=rig)
    rig.play("Radio")
    rig.wait_running("Radio")


def test_radio_crash_is_respawned_and_plays_again(rig):
    svc = rig.services["speaker-radio"]
    before = svc.starts
    rig.kill("speaker-radio")
    wait_until(lambda: svc.starts > before and svc.state == "started", 20, "respawn", rig=rig)
    wait_until(lambda: radio_status().get("available") is True, 10, "radio available", rig=rig)
    rig.play("Radio")
    rig.wait_running("Radio")


def test_radio_hung_is_reported_stuck_quickly_and_restart_recovers(rig):
    rig.hang("speaker-radio")
    try:
        t0 = time.monotonic()
        wait_until(lambda: radio_status()["available"] is False, 15, "radio unavailable", rig=rig)
        st = radio_status()
        assert st["state"] == "stuck", st            # OpenRC says started, but it does not answer
        t0 = time.monotonic()
        api("/api/status")
        assert time.monotonic() - t0 < 5             # I8: a hung player never stalls the API
        code, _ = api("/api/service", {"name": "speaker-radio"})
        assert code == 200
    finally:
        rig.unhang("speaker-radio")
    wait_until(lambda: radio_status().get("available") is True, 25, "radio recovered by Restart", rig=rig)
    assert ["restart", "speaker-radio"] in rig.admin_calls


def test_radio_socket_with_wrong_permissions_is_stuck_and_restart_fixes_it(rig):
    os.chmod("/run/speaker/radio.sock", 0o600)       # as mpv creates it, before start_post chmods it
    wait_until(lambda: radio_status()["available"] is False, 15, "radio unavailable", rig=rig)
    assert radio_status()["state"] == "stuck"
    api("/api/service", {"name": "speaker-radio"})
    wait_until(lambda: radio_status().get("available") is True, 25, "radio recovered", rig=rig)


def test_play_while_radio_down_gives_a_plain_error(rig):
    rig.stop("speaker-radio")
    try:
        code, body = api("/api/radio", {"cmd": "play", "uuid": "00000000-0000-4000-8000-000000000001"})
        assert code == 400 or code == 200 and "error" in body, (code, body)
        text = body.get("error", "") if isinstance(body, dict) else str(body)
        assert "mpv" not in text.lower() and "speaker-radio" not in text
        assert text, "no message"
    finally:
        rig.start("speaker-radio")


def test_arbiter_killed_enforces_the_rules_again_after_respawn(rig):
    rig.play("Spotify")
    rig.wait_running("Spotify")
    arb = rig.services["speaker-arbiter"]
    before = arb.starts
    rig.kill("speaker-arbiter")
    rig.play("AirPlay")                                # starts while nobody is arbitrating
    rig.wait_running("AirPlay")
    wait_until(lambda: arb.starts > before and arb.state == "started", 20, "arbiter respawned", rig=rig)
    # The streams that started while it was away are not "started" events for the new arbiter;
    # the next start is arbitrated normally and both older ones are silenced.
    rig.play("Radio")
    rig.wait_running("Radio")
    wait_until(lambda: rig.audible() == ["Radio"], 20, "only Radio audible", rig=rig)


def test_pipewire_restart_everything_recovers(rig):
    rig.play("Radio")
    rig.wait_running("Radio")
    rig.restart("speaker-pipewire")
    # WirePlumber and the arbiter's pw-dump lose their connection; the services come back.
    wait_until(lambda: rig.services["speaker-arbiter"].state == "started", 30, "arbiter up", rig=rig)
    wait_until(lambda: any(n["name"] == "alsa_output.rig" for n in rig.nodes()), 20, "null sink", rig=rig)
    rig.restart("speaker-wireplumber")
    rig.restart("speaker-radio")
    rig.restart("speaker-arbiter")
    for fs in rig.fake_sources.values():
        fs.close()
        fs.start()
    rig.play("Radio")
    rig.wait_running("Radio")
    rig.play("AirPlay")
    rig.wait_running("AirPlay")
    wait_until(lambda: rig.audible() == ["AirPlay"], 20, "last source wins again", rig=rig)


def test_wireplumber_restart_does_not_leave_a_stuck_mute(rig):
    rig.play("Radio")
    rig.wait_running("Radio")
    rig.play("AirPlay")
    rig.wait_running("AirPlay")
    wait_until(lambda: rig.audible() == ["AirPlay"], 15, "AirPlay wins", rig=rig)
    rig.restart("speaker-wireplumber")
    rig.stop_source("AirPlay")
    rig.stop_source("Radio")
    wait_until(lambda: not rig.running(), 15, "all stopped", rig=rig)
    rig.play("Radio")
    rig.wait_running("Radio")
    wait_until(lambda: rig.audible() == ["Radio"], 15, "Radio audible", rig=rig)


def test_web_restart_does_not_disturb_playback(rig):
    rig.play("Radio")
    rig.wait_running("Radio")
    rig.restart("panda-web")
    wait_until(lambda: api("/api/status")[0] == 200, 20, "web back", rig=rig)
    assert rig.audible() == ["Radio"]
    st = api("/api/status")[1]
    assert st["radio"]["available"] is True


def test_bt_and_wifi_daemons_absent_do_not_break_the_status(rig):
    # panda-bt and panda-wifi are not part of the rig: their sockets do not exist.
    t0 = time.monotonic()
    code, st = api("/api/status")
    assert code == 200 and time.monotonic() - t0 < 5
    assert "unavailable" in str(st["bt"].get("error", "")) and "unavailable" in str(st["wifi"].get("error", ""))


def test_restart_button_hammered_is_harmless(rig):
    for _ in range(10):
        api("/api/service", {"name": "speaker-radio"})
    # rc-service runs one restart at a time; wait for the queue to drain, then for the player.
    wait_until(lambda: not rig.busy(), 60, "the restarts to finish", rig=rig)
    svc = rig.services["speaker-radio"]
    wait_until(lambda: svc.state == "started" and os.path.exists("/run/speaker/radio.sock"), 20,
               "the last restart to come up", rig=rig)
    # (/api/status is a snapshot a couple of seconds old: poll it, do not trust the first answer.)
    wait_until(lambda: radio_status().get("available") is True, 40, "radio settled", rig=rig)
    assert len([p for p in os.popen("pgrep -f 'audio-client-name=radio'").read().split()]) == 1, "duplicate players"
    rig.play("Radio")
    rig.wait_running("Radio")
