# Finding 42: the agent cuts every idle command stream after 10 s, the fleet pings every 15 s

- **Area:** logic
- **Severity:** major
- **Source:** Task 9d, live in a VM against the sim fleet over https

## Expected

The SSE command channel (specification section 3) stays open while no command is
pending; the fleet's keep-alive ping keeps it alive, and a new command is delivered
immediately.

## Actual

`agent.transport.build_client` gives every request a 10 s timeout (`timeout: float =
10.0`), which for the stream is the read timeout. The fleet's keep-alive comment is sent
every 15 s (`FLEET_COMMANDS_SSE_PING_INTERVAL_S`, default 15.0 in `fleet/app.py`). An idle
stream therefore always times out first. The agent logs

```
SSE command stream unavailable (The read operation timed out); falling back to polling.
```

and works through the poll fallback (`GET /v1/commands?wait=0`, 60 s interval) instead.
A command sent from the fleet UI path (`create_command_unless_duplicate`) reached the
real agent and its result came back, but only after 30 s (`fetch_logs`) and 48 s
(`report_now`), not instantly. The same stream read with a 60 s timeout through the sim
TLS proxy showed the pings arriving exactly every 15 s, so the transport itself is fine;
only the client timeout is too short.

## Evidence

- `agent/transport.py:411` (`timeout: float = 10.0`), `agent/__main__.py:99,179`
  (`build_client(...)` without a timeout), `agent/commands_channel.py:539-548` (fallback
  and 60 s `DEFAULT_FALLBACK_POLL_INTERVAL_S`), `fleet/app.py:118-119`.
- Agent container log of sim-a01 / sim-a02, probe with a 60 s timeout described in
  docs/sim-findings/02-vm-run.md.

## Suggested direction

Use a stream-specific timeout (read timeout comfortably above the ping interval, e.g. 2x
to 3x, or none for the read with an application-level idle check), keeping the short
timeout for ordinary requests. Add a test with a stream that only sends pings.
