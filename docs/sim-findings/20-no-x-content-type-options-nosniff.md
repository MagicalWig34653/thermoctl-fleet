# Finding: No X-Content-Type-Options: nosniff

- **Area:** security
- **Severity:** minor
- **Source:** pentest check S05 (tools/sim/pentest), run against the sim

## Expected

UI pages and API/JSON responses carry X-Content-Type-Options: nosniff.

## Actual

no nosniff on: ['/ui/login (200)', '/ui/nonexistent-pentest (404)', '/healthz (200)', '/v1/commands (401)']

None of `/ui/login`, `/ui/nonexistent-pentest`, `/healthz`, `/v1/commands` sends the header. `fleet/ui_routes.py` `install_security_headers` sets only CSP, X-Frame-Options and Referrer-Policy, and only for paths under `/ui`.

## Evidence

```
GET /ui/login /ui/nonexistent-pentest /healthz /v1/commands anonymously
```

## Suggested direction

Add `X-Content-Type-Options: nosniff` in the same middleware that sets CSP and `X-Frame-Options` for `/ui`, and extend that middleware (or add a second one) so it also covers `/v1` and `/healthz`. One line; no behaviour change for the agent.
