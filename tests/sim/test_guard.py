from datetime import UTC, datetime, timedelta

from tools.sim.guard import (
    MIN_FREE_BYTES,
    LoginOutcome,
    build_health_check,
    parse_df_avail,
)

NOW = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
APTS = ("a01", "a02", "a03")
DF_OK = (
    "Filesystem     1024-blocks     Used Available Capacity Mounted on\n"
    "/dev/vdb1         41152736 20000000  19000000      52% /data\n"
)


def test_parse_df_avail_returns_bytes():
    assert parse_df_avail(DF_OK) == 19000000 * 1024


def test_parse_df_avail_handles_wrapped_filesystem_name():
    text = (
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "longname\n  100 40 60 40% /data\n"
    )
    assert parse_df_avail(text) == 60 * 1024


def test_parse_df_avail_garbage_is_none():
    assert parse_df_avail("") is None
    assert parse_df_avail("df: /data: No such file") is None
    assert parse_df_avail("Filesystem 1024-blocks\n/dev/x a b c d /data\n") is None


def make(*, seen=None, healthz=None, disk=MIN_FREE_BYTES * 5, silent=frozenset(), outcome=None):
    seen = seen if seen is not None else {a: NOW for a in APTS}
    return build_health_check(
        healthz=lambda: healthz,
        heartbeats=lambda: seen,
        now=lambda: NOW,
        silent=lambda: silent,
        apartments=APTS,
        disk_avail=lambda: disk,
        logins=outcome or LoginOutcome(),
    )


def test_healthy_fleet_has_no_problems():
    assert make()() == []


def test_healthz_problem_is_reported():
    assert make(healthz="fleet /healthz answers 500")() == ["fleet /healthz answers 500"]


def test_stale_apartment_is_a_problem_but_silent_one_is_not():
    seen = {"a01": NOW, "a02": NOW - timedelta(minutes=16), "a03": None}
    problems = make(seen=seen)()
    assert len(problems) == 1
    assert "only 1 of 3" in problems[0] and "a02" in problems[0] and "a03" in problems[0]
    assert make(seen=seen, silent=frozenset({"a02", "a03"}))() == []


def test_heartbeat_just_inside_15_minutes_counts_as_fresh():
    seen = {a: NOW - timedelta(minutes=15) for a in APTS}
    assert make(seen=seen)() == []


def test_unreadable_heartbeats_are_a_problem():
    def boom():
        raise RuntimeError("no db")

    check = build_health_check(
        healthz=lambda: None,
        heartbeats=boom,
        now=lambda: NOW,
        silent=frozenset,
        apartments=APTS,
        disk_avail=lambda: None,
        logins=LoginOutcome(),
    )
    assert "cannot read heartbeats" in check()[0]


def test_low_disk_is_a_problem_unknown_disk_is_not():
    assert "free" in make(disk=MIN_FREE_BYTES - 1)()[0]
    assert make(disk=None)() == []


def test_all_fleet_roles_failing_to_log_in_is_an_outage():
    outcome = LoginOutcome()
    outcome.record_failure(["tenant1", "landlord", "caretaker", "admin"])
    assert "login" in make(outcome=outcome)()[0]
    outcome.record_failure(["landlord", "tenant2"])  # newer refresh: only one fleet role
    assert make(outcome=outcome)() == []
    outcome.record_failure(["landlord", "caretaker", "admin"])
    outcome.record_success()
    assert make(outcome=outcome)() == []
