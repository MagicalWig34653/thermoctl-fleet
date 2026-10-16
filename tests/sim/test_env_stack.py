"""Stack control: order of the steps and the sim-scoped scope of --down (injected runner)."""

import json

from tools.sim.env import stack


class Recorder:
    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(argv)
        return 3 if self.fail_on and self.fail_on in argv else 0

    def tails(self) -> list[tuple[str, ...]]:
        return [tuple(c[2:]) for c in self.calls]


def test_up_runs_the_pieces_in_the_right_order():
    run = Recorder()
    assert stack.up(run, python="py", log=lambda m: None) == 0
    assert run.calls[0] == ["py", "-m", "tools.sim.env.up_fleet", "--agents"]
    assert run.tails() == [
        ("tools.sim.env.up_fleet", "--agents"),
        ("tools.sim.env.vms", "--up"),
        ("tools.sim.env.tenant", "--up"),
        ("tools.sim.env.up_fleet", "--logins"),
        ("tools.sim.env.tenant", "--logins"),
    ]


def test_up_stops_at_the_first_failing_step():
    run = Recorder(fail_on="tools.sim.env.vms")
    messages = []
    assert stack.up(run, python="py", log=messages.append) == 3
    assert [c[2] for c in run.calls] == ["tools.sim.env.up_fleet", "tools.sim.env.vms"]
    assert any("step failed (VM base stations)" in m for m in messages)


def test_down_is_the_reverse_and_only_calls_the_sim_scoped_downs():
    run = Recorder()
    assert stack.down(run, python="py", log=lambda m: None) == 0
    assert run.tails() == [
        ("tools.sim.env.tenant", "--down"),
        ("tools.sim.env.vms", "--down"),
        ("tools.sim.env.up_fleet", "--down"),
    ]
    flat = [part for call in run.calls for part in call]
    assert not {"docker", "limactl", "rm", "delete", "prune"} & set(flat)


def test_down_keeps_going_after_a_failure_and_reports_it():
    run = Recorder(fail_on="tools.sim.env.vms")
    assert stack.down(run, python="py", log=lambda m: None) == 3
    assert len(run.calls) == 3  # the fleet is still taken down


def test_down_table_is_the_reverse_of_the_up_modules():
    assert stack.UP_STEPS[0][1][0] == "tools.sim.env.up_fleet"  # the fleet comes first
    down_modules = [m for _, (m, *_a) in stack.DOWN_STEPS]
    assert down_modules == ["tools.sim.env.tenant", "tools.sim.env.vms", "tools.sim.env.up_fleet"]


def test_status_filters_to_the_sim_resources():
    ps = "thermoctl-sim-fleet\tUp 5 minutes\nnot-thermoctl-sim-x\tUp\nthermoctl-sim-net-x\tUp\n"
    lima = "\n".join(
        json.dumps({"name": n, "status": s})
        for n, s in {
            "thermoctl-test-vm": "Running",
            "thermoctl-sim-a01": "Running",
            "thermoctl-sim-a02": "Stopped",
            "thermoctl-sim-a03": "Running",
        }.items()
    )
    seen = []

    def capture(argv):
        seen.append(argv)
        return (0, ps if argv[0] == "docker" else lima)

    text = stack.status(capture)
    assert "thermoctl-sim-fleet  Up 5 minutes" in text and "not-thermoctl-sim-x" not in text
    assert "thermoctl-sim-a01  Running" in text and "thermoctl-sim-a02  Stopped" in text
    assert "thermoctl-test-vm" not in text and "thermoctl-sim-a03" not in text
    assert seen[0][:2] == ["docker", "ps"] and "name=thermoctl-sim" in seen[0]
    assert seen[1] == ["limactl", "list", "--json"]


def test_status_when_nothing_runs_or_tools_are_missing():
    assert "none" in stack.status(lambda argv: (0, "")) and "absent" in stack.status(
        lambda argv: (0, "")
    )
    text = stack.status(lambda argv: (1, ""))
    assert "docker unavailable" in text and "limactl unavailable" in text
