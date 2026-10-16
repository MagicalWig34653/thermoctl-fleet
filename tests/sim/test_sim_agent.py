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


@pytest.mark.parametrize("bad", ["../x", "a/b", "a\\b", "..", "a..b"])
def test_fault_file_rejects_path_traversal_ids(tmp_path, bad):
    with pytest.raises(ValueError):
        FaultFile(tmp_path, bad)


def test_make_health_logs_invalid_fault_content_at_error(tmp_path, caplog):
    faults = tmp_path / "faults"
    faults.mkdir()
    (faults / "sim-a03.json").write_text(json.dumps({"system": {"memory_free_percent": 500}}))
    with caplog.at_level(logging.ERROR, logger="sim_agent"):
        health = make_health(tmp_path, "sim-a03")()
    assert health == healthy_sections()
    records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert records
    text = records[0].getMessage()
    assert "sim-a03" in text and str(faults / "sim-a03.json") in text


def test_read_drift_defaults_to_zero_and_never_raises(tmp_path: Path):
    from tools.sim.sim_agent import read_drift

    assert read_drift(tmp_path, "sim-a03") == 0.0
    (tmp_path / "faults").mkdir()
    (tmp_path / "faults" / "sim-a03.drift").write_text("not a number")
    assert read_drift(tmp_path, "sim-a03") == 0.0
    (tmp_path / "faults" / "sim-a03.drift").write_text("-300.5\n")
    assert read_drift(tmp_path, "sim-a03") == -300.5
    (tmp_path / "faults" / "sim-a03.drift").write_text("nan")
    assert read_drift(tmp_path, "sim-a03") == 0.0


def test_drift_shifts_sent_at_and_is_reported(tmp_path: Path):
    from datetime import timedelta

    from tools.sim.sim_agent import drifted_now

    (tmp_path / "faults").mkdir()
    (tmp_path / "faults" / "sim-a03.drift").write_text("600")
    shifted = drifted_now(tmp_path, "sim-a03")()
    assert timedelta(seconds=595) < shifted - datetime.now(UTC) < timedelta(seconds=605)
    assert make_health(tmp_path, "sim-a03")()["system"]["clock_drift_s"] == 600.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("nan", 0.0),
        ("inf", 0.0),
        ("-inf", 0.0),
        ("1e30", 86400.0),
        ("-1e30", -86400.0),
        ("99999999999", 86400.0),
        ("-12.5", -12.5),
        ("garbage \x00 text", 0.0),
        ("", 0.0),
    ],
)
def test_read_drift_clamps_and_ignores_non_finite(tmp_path: Path, text: str, expected: float):
    from tools.sim.sim_agent import read_drift

    (tmp_path / "faults").mkdir()
    (tmp_path / "faults" / "sim-a03.drift").write_text(text)
    assert read_drift(tmp_path, "sim-a03") == expected


def test_read_drift_missing_file_is_zero(tmp_path: Path):
    from tools.sim.sim_agent import read_drift

    assert read_drift(tmp_path, "sim-a09") == 0.0


def _args(tmp_path: Path, *extra: str):
    return sim_agent.parse_args(
        ["--apartment", "a", "--fleet", "https://host.lima.internal:8243", "--token-file", "t",
         "--state-dir", str(tmp_path), *extra]
    )  # fmt: skip


def test_http_client_plain_without_tls_options(tmp_path: Path):
    client = sim_agent.build_http_client(
        sim_agent.parse_args(
            [
                "--apartment",
                "a",
                "--fleet",
                "http://127.0.0.1:1",
                "--token-file",
                "t",
                "--state-dir",
                str(tmp_path),
            ]
        )  # fmt: skip
    )
    assert str(client.base_url) == "http://127.0.0.1:1"
    assert client.trust_env is False


def test_ca_file_without_pin_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    with pytest.raises(ValueError, match="--pinned-fingerprint"):
        sim_agent.build_http_client(_args(tmp_path, "--ca-file", str(tmp_path / "ca.pem")))
    tok = tmp_path / "tok"
    tok.write_text(TOKEN)
    rc = sim_agent.main(
        ["--apartment", "a", "--fleet", "https://host.lima.internal:8243",
         "--token-file", str(tok), "--state-dir", str(tmp_path),
         "--ca-file", str(tmp_path / "ca.pem")]
    )  # fmt: skip
    # host.lima.internal is not allowed unless SIM_ALLOWED_HOSTS names it: refused first.
    assert rc == 2 and "refused" in capsys.readouterr().err


def test_pinned_client_checks_the_fingerprint_and_the_ca(tmp_path: Path, monkeypatch):
    """The VM sim agent uses the real agent's pinned client: right pin works, wrong pin and a
    CA nobody trusts both fail (verification is never relaxed)."""
    import http.server
    import ssl
    import threading

    from tools.sim.env.tls import COMMON_NAME, ensure_tls

    material = ensure_tls(tmp_path)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(material.cert_file), str(material.key_file))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    # The certificate names host.lima.internal; resolve it to loopback for the test only.
    import socket

    real = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **k: real("127.0.0.1" if host == COMMON_NAME else host, *a, **k),
    )
    try:
        fleet = f"https://{COMMON_NAME}:{port}"
        base = ["--apartment", "a", "--fleet", fleet, "--token-file", "t", "--state-dir", "s"]
        good = sim_agent.parse_args(
            [
                *base,
                "--ca-file",
                str(material.ca_file),
                "--pinned-fingerprint",
                material.fingerprint,
            ]
        )
        with sim_agent.build_http_client(good) as client:
            assert client.get("/").status_code == 204
        wrong = sim_agent.parse_args(
            [*base, "--ca-file", str(material.ca_file),
             "--pinned-fingerprint", "sha256:" + "0" * 64]
        )  # fmt: skip
        with sim_agent.build_http_client(wrong) as client, pytest.raises(httpx.TransportError):
            client.get("/")
        no_ca = sim_agent.parse_args([*base, "--pinned-fingerprint", material.fingerprint])
        with sim_agent.build_http_client(no_ca) as client, pytest.raises(httpx.TransportError):
            client.get("/")
    finally:
        server.shutdown()
        server.server_close()


def test_standing_clock_ticks_offset_by_one_ms_each_up_to_the_bound_then_repeat():
    from datetime import timedelta

    from tools.sim.sim_agent import MAX_STANDING_OFFSET_MS

    seen: list[datetime] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(Heartbeat.model_validate_json(req.content).sent_at)
        return httpx.Response(204)

    clock = [NOW]
    agent = SimAgent("sim-a03", _client(handler), TOKEN, healthy_sections, now=lambda: clock[0])
    for _ in range(MAX_STANDING_OFFSET_MS + 4):
        agent.tick()
    ms = [int((t - NOW).total_seconds() * 1000) for t in seen]
    assert ms[:4] == [0, 1, 2, 3]
    assert max(ms) == MAX_STANDING_OFFSET_MS == 50  # bound: at most 51 rows per instant
    assert ms[-3:] == [MAX_STANDING_OFFSET_MS] * 3
    clock[0] = NOW + timedelta(days=1)  # the clock moves: first tick exact again
    agent.tick()
    agent.tick()
    assert seen[-2:] == [clock[0], clock[0] + timedelta(milliseconds=1)]


def test_a_lagging_fleet_clock_and_failed_ticks_still_end_in_a_fresh_row():
    """The fleet stores a row per distinct sent_at, stamped with ITS (lagging) clock; sends
    that fail on the wire (stale keep-alive through the TLS proxy) store nothing."""
    from datetime import timedelta

    rows: list[tuple[datetime, datetime]] = []  # (sent_at, received_at)
    calls = [0]
    fleet_clock = NOW - timedelta(days=1)  # lags until the 6th call

    def handler(req: httpx.Request) -> httpx.Response:
        calls[0] += 1
        sent = Heartbeat.model_validate_json(req.content).sent_at
        if calls[0] == 2:  # the old design's one retry (tick 1) is lost on the wire
            raise httpx.RemoteProtocolError("server disconnected")
        received = NOW if calls[0] >= 6 else fleet_clock
        if sent not in [r[0] for r in rows]:  # unique (apartment, sent_at)
            rows.append((sent, received))
        return httpx.Response(204)

    agent = SimAgent("sim-a01", _client(handler), TOKEN, healthy_sections, now=lambda: NOW)
    for _ in range(8):
        agent.tick()
    assert max(r for _, r in rows) == NOW  # a row with a fresh received_at exists
