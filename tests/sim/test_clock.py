from datetime import timedelta

import pytest

from tools.sim.clock import SIM_START, SimClock


def test_write_uses_faketime_absolute_format(tmp_path):
    clock = SimClock(tmp_path / "ft")
    clock.write()
    assert (tmp_path / "ft").read_text() == "2026-01-05 08:00:00\n"


def test_advance_moves_time_and_rewrites_file(tmp_path):
    clock = SimClock(tmp_path / "ft")
    clock.advance(timedelta(days=1, hours=2))
    assert (tmp_path / "ft").read_text() == "2026-01-06 10:00:00\n"
    assert clock.now == SIM_START + timedelta(days=1, hours=2)


def test_advance_rejects_non_positive_delta(tmp_path):
    with pytest.raises(ValueError):
        SimClock(tmp_path / "ft").advance(timedelta(0))


def test_week_index(tmp_path):
    clock = SimClock(tmp_path / "ft")
    assert clock.week_index() == 0
    clock.advance(timedelta(days=15))
    assert clock.week_index() == 2


def test_clock_path_lives_in_its_own_directory_created_on_write(tmp_path):
    from tools.sim.clock import CLOCK_DIRNAME, clock_path

    assert clock_path(tmp_path) == tmp_path / CLOCK_DIRNAME / "clock"
    SimClock(clock_path(tmp_path)).write()
    assert clock_path(tmp_path).is_file()


def test_rewrite_keeps_the_same_file(tmp_path):
    # A rename-based swap makes libfaketime in the containers fall back to real time
    # for a moment (virtiofs); the file must be overwritten in place.
    import os

    clock = SimClock(tmp_path / "clock" / "clock")
    clock.write()
    inode = os.stat(clock.path).st_ino
    clock.advance(timedelta(hours=5))
    assert os.stat(clock.path).st_ino == inode
    assert clock.path.read_text() == "2026-01-05 13:00:00\n"
    assert [p.name for p in clock.path.parent.iterdir()] == ["clock"]  # no temp file left
