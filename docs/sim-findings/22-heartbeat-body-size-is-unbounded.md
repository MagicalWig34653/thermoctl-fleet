# Finding: Heartbeat body size is unbounded

- **Area:** security
- **Severity:** minor
- **Source:** pentest check G04 (tools/sim/pentest), run against the sim

## Expected

A 1.5 MB and an 8 MB heartbeat body are refused with a 4xx (413 expected); a heartbeat has a few hundred bytes of content, so storing megabytes per request is unbounded growth.

## Actual

statuses {'1.5MB': 204, '8MB': 204} (204 = accepted and stored); healthz afterwards ok=True

A 1.5 MB and an 8 MB heartbeat (the `agent` string padded) were both answered 204 and stored in `heartbeats.payload_json`. Requires a valid agent token, so this is storage growth by a compromised or buggy device, not an anonymous attack. The sim cleaned up with a normal heartbeat afterwards; the large rows remain in the sim database.

## Evidence

```
POST /v1/heartbeat as sim-a12 with the `agent` string padded to 1.5 MB / 8 MB
```

## Suggested direction

Cap the request body of `POST /v1/heartbeat` and `/v1/heartbeats` the way the backup and bundle upload routes already do (`Content-Length` check plus streamed cut-off, 413). A heartbeat is a few hundred bytes; 64 KiB per entry is generous. Also bound the free-text fields (`agent`, `thermoctl.version`, `zone`) with `max_length` in `protocol/heartbeat.py`.
