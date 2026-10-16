from tools.sim.scenario import EventKind, build_schedule, due_events

APTS = [f"a{i:02d}" for i in range(12)]


def test_schedule_is_deterministic() -> None:
    assert build_schedule(7, 156, APTS) == build_schedule(7, 156, APTS)


def test_schedule_covers_every_kind_and_stays_in_range() -> None:
    sched = build_schedule(7, 156, APTS)
    assert {e.kind for e in sched} == set(EventKind)
    assert all(0 <= e.week < 156 and e.apartment in APTS for e in sched)
    assert len({e.id for e in sched}) == len(sched)


def test_heartbeat_loss_is_followed_by_restore() -> None:
    sched = build_schedule(7, 156, APTS)
    losses = [e for e in sched if e.kind is EventKind.HEARTBEAT_LOSS]
    restores = {(e.apartment, e.week) for e in sched if e.kind is EventKind.HEARTBEAT_RESTORE}
    assert losses
    for loss in losses:
        assert any(a == loss.apartment and w > loss.week for a, w in restores)


def test_due_events_skips_applied_ones() -> None:
    sched = build_schedule(7, 156, APTS)
    week = sched[0].week
    first = due_events(sched, week, [])
    assert first and all(e.week == week for e in first)
    assert due_events(sched, week, [e.id for e in first]) == []


def test_schedule_is_sorted_by_week_then_id() -> None:
    sched = build_schedule(3, 52, APTS)
    assert sched == sorted(sched, key=lambda e: (e.week, e.id))


def test_tiny_horizons_stay_valid() -> None:
    for weeks in (1, 2, 3, 4):
        sched = build_schedule(1, weeks, ["a00", "a01"])
        assert all(0 <= e.week < weeks for e in sched)
        assert len({e.id for e in sched}) == len(sched)
        for loss in (e for e in sched if e.kind is EventKind.HEARTBEAT_LOSS):
            assert any(
                r.kind is EventKind.HEARTBEAT_RESTORE
                and r.apartment == loss.apartment
                and r.week > loss.week
                for r in sched
            )


def test_restore_follows_loss_for_many_seeds() -> None:
    for seed in range(30):
        sched = build_schedule(seed, 156, APTS)
        assert len({e.id for e in sched}) == len(sched)
        for loss in (e for e in sched if e.kind is EventKind.HEARTBEAT_LOSS):
            n = loss.id.rsplit(":", 1)[1]
            restore = next(e for e in sched if e.id == f"restore:{loss.apartment}:{n}")
            assert restore.week > loss.week


def test_loss_windows_never_overlap_per_apartment() -> None:
    weeks = 156
    for seed in range(30):
        sched = build_schedule(seed, weeks, APTS)
        for apt in APTS:
            mine = sorted(
                (e for e in sched if e.apartment == apt),
                key=lambda e: (e.week, e.kind is EventKind.HEARTBEAT_LOSS),
            )
            open_loss = False
            for e in mine:
                if e.kind is EventKind.HEARTBEAT_LOSS:
                    assert not open_loss, (seed, apt, e)
                    open_loss = True
                elif e.kind is EventKind.HEARTBEAT_RESTORE:
                    assert open_loss, (seed, apt, e)
                    open_loss = False
            assert not open_loss, (seed, apt)


def test_events_can_land_in_the_last_weeks() -> None:
    weeks = 156
    late = [
        e
        for seed in range(5)
        for e in build_schedule(seed, weeks, APTS)
        if e.week >= weeks - 3 and e.kind is not EventKind.HEARTBEAT_RESTORE
    ]
    assert late


def test_silent_apartments_are_losses_without_their_restore():
    from tools.sim.scenario import silent_apartments

    applied = [
        "heartbeat_loss:sim-a02:0",
        "restore:sim-a02:0",  # window closed
        "heartbeat_loss:sim-a02:1",  # open again
        "heartbeat_loss:sim-a07:0",  # open
        "restore:sim-a09:0",  # restore without loss in the list: nothing
        "backup:sim-a03:1",
    ]
    assert silent_apartments(applied) == {"sim-a02", "sim-a07"}
    assert silent_apartments([]) == frozenset()
