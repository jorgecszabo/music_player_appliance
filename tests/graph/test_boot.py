"""A. Boot and convergence (the rig started everything: check the result)."""
from rig import api as _api, wait_until


def api(path, body=None):
    return _api(path, body)[1]


def test_everything_is_up(rig):
    assert all(s == "started" for s in rig.states().values()), rig.states()


def test_status_reports_the_graph(rig):
    st = wait_until(lambda: api("/api/status"), 10, "status")
    assert st["radio"]["available"] is True
    assert st["restartable"], "no restartable list"
    assert "speaker-radio" in st["restartable"]


def test_nothing_is_playing_after_boot(rig):
    assert rig.audible() == []
