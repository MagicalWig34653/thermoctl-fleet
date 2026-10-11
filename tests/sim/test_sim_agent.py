import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from protocol.heartbeat import Heartbeat
from protocol.version import PROTOCOL_VERSION
from tools.sim import sim_agent
from tools.sim.sim_agent import FaultFile, SimAgent, healthy_sections, make_health

TOKEN = "tok-SECRET-123"
NOW = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


def _client(handler):  # type: ignore[no-untyped-def]
    return httpx.Client(base_url="http://127.0.0.1:8100", transport=httpx.MockTransport(handler))


def _agent(handler, health=None, tmp_path=None):  # type: ignore[no-untyped-def]
    return SimAgent("sim-a03", _client(handler), TOKEN, health or healthy_sections, now=lambda: NOW)


def test_tick_posts_valid_heartbeat_with_bearer():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(204)

    assert _agent(handler).tick() is True
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/v1/heartbeat"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    hb = Heartbeat.model_validate_json(req.content)
    assert hb.apartment == "sim-a03"
    assert hb.protocol_version == PROTOCOL_VERSION
    assert hb.sent_at == NOW
    assert hb.open_faults == []


def test_200_is_true():
    assert _agent(lambda r: httpx.Response(200)).tick() is True


def test_401_is_false_without_raising(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.DEBUG):
        assert _agent(lambda r: httpx.Response(401, text="no")).tick() is False
    assert TOKEN not in caplog.text


def test_network_error_is_false_without_raising(caplog: pytest.LogCaptureFixture):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach (auth {req.headers['authorization']})")

    with caplog.at_level(logging.DEBUG):
        assert _agent(handler).tick() is False
    assert TOKEN not in caplog.text


def test_payload_contains_no_forbidden_categories():
    seen: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.content)
        return httpx.Response(204)

    _agent(handler).tick()
    text = seen[0].decode().lower()
    for word in ("setpoint", "schedule", "absence", "tenant", "room", "temperature", "contact"):
        assert word not in text


def test_fault_file_absent_is_healthy(tmp_path: Path):
    assert FaultFile(tmp_path, "sim-a03").read() == {}


def test_fault_file_invalid_json_is_healthy_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    (tmp_path / "faults").mkdir()
    (tmp_path / "faults" / "sim-a03.json").write_text("{not json")
    with caplog.at_level(logging.WARNING):
        assert FaultFile(tmp_path, "sim-a03").read() == {}
    assert "sim-a03" in caplog.text
    (tmp_path / "faults" / "sim-a03.json").write_text("[1, 2]")
    assert FaultFile(tmp_path, "sim-a03").read() == {}


def _write_fault(tmp_path: Path, content: object) -> None:
    (tmp_path / "faults").mkdir(exist_ok=True)
    (tmp_path / "faults" / "sim-a03.json").write_text(json.dumps(content))


def test_make_health_maps_fault_file_onto_heartbeat(tmp_path: Path):
    _write_fault(
        tmp_path,
        {
            "devices": {"zigbee_bridge": "offline", "weakest_battery_percent": 4, "bogus": 1},
            "thermoctl": {"reachable": False},
            "open_faults": [
                {"kind": "sensor_fault", "since": "2026-01-04T08:00:00Z", "zone": "z1"}
            ],
            "unknown_top_key": {"x": 1},
        },
    )
    seen: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.content)
        return httpx.Response(204)

    agent = _agent(handler, make_health(tmp_path, "sim-a03"))
    assert agent.tick() is True
    hb = Heartbeat.model_validate_json(seen[0])
    assert hb.devices.zigbee_bridge == "offline"
    assert hb.devices.weakest_battery_percent == 4
    assert hb.devices.worst_signal_quality == 100  # untouched default
    assert hb.thermoctl.reachable is False
    assert [f.kind.value for f in hb.open_faults] == ["sensor_fault"]
    assert "bogus" not in seen[0].decode() and "unknown_top_key" not in seen[0].decode()


def test_make_health_rereads_file_every_call(tmp_path: Path):
    health = make_health(tmp_path, "sim-a03")
    assert health().get("open_faults", []) == []
    _write_fault(
        tmp_path,
        {"open_faults": [{"kind": "window_alarm", "since": "2026-01-04T08:00:00Z", "zone": "z"}]},
    )
    assert len(health()["open_faults"]) == 1


def test_make_health_schema_invalid_content_falls_back_healthy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    _write_fault(tmp_path, {"devices": {"weakest_battery_percent": 500}})
    with caplog.at_level(logging.WARNING):
        health = make_health(tmp_path, "sim-a03")()
    assert health["devices"]["weakest_battery_percent"] == 100
    assert "sim-a03" in caplog.text


def test_main_refuses_non_local_fleet(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    tok = tmp_path / "tok"
    tok.write_text(TOKEN)
    rc = sim_agent.main(
        [
            "--apartment", "sim-a03",
            "--fleet", "https://example.com",
            "--token-file", str(tok),
            "--state-dir", str(tmp_path),
        ]
    )  # fmt: skip
    err = capsys.readouterr().err
    assert rc == 2 and "refused" in err and TOKEN not in err


def test_main_missing_or_empty_token_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    base = ["--apartment", "a", "--fleet", "http://127.0.0.1:1", "--state-dir", str(tmp_path)]
    assert sim_agent.main([*base, "--token-file", str(tmp_path / "nope")]) == 2
    empty = tmp_path / "empty"
    empty.write_text("\n")
    assert sim_agent.main([*base, "--token-file", str(empty)]) == 2


def test_parse_args_defaults_and_no_token_argument(tmp_path: Path):
    ns = sim_agent.parse_args(
        ["--apartment", "a", "--fleet", "http://127.0.0.1:1",
         "--token-file", "t", "--state-dir", str(tmp_path)]
    )  # fmt: skip
    assert ns.interval_s == 5.0 and ns.apartment == "a"
    with pytest.raises(SystemExit):
        sim_agent.parse_args(["--apartment", "a", "--fleet", "x", "--token", "abc"])


def test_main_runs_until_stop(tmp_path: Path):
    tok = tmp_path / "tok"
    tok.write_text(TOKEN + "\n")
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.headers["authorization"])
        return httpx.Response(204)

    rc = sim_agent.main(
        ["--apartment", "a", "--fleet", "http://127.0.0.1:1", "--token-file", str(tok),
         "--state-dir", str(tmp_path), "--interval-s", "0.01"],
        client=_client(handler),
        max_ticks=2,
    )  # fmt: skip
    assert rc == 0 and calls == [f"Bearer {TOKEN}"] * 2


def test_fault_file_unreadable_is_healthy(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    (tmp_path / "faults" / "sim-a03.json").mkdir(parents=True)  # a directory: OSError
    with caplog.at_level(logging.WARNING):
        assert FaultFile(tmp_path, "sim-a03").read() == {}
    assert "unreadable" in caplog.text


def test_tick_with_invalid_health_is_false_and_sends_nothing():
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not be called")

    assert _agent(handler, lambda: {}).tick() is False
