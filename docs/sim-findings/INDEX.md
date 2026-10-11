# Simulation findings

No findings yet.

## Hand-written findings

- [Smoke run (Task 10): first live run of the usage simulation](01-smoke-run.md)
- [VM base stations: live run (Task 9d)](02-vm-run.md)
- [Finding 10: the agent never sends a heartbeat](10-agent-heartbeat-gap.md)
- [Finding 11: every fleet UI user has the same powers; landlord, caretaker and admin are the same role](11-ui-has-no-roles.md)
- [Finding 12: `/openapi.json` and `/docs` are served without a login](12-openapi-and-docs-public.md)
- [Finding 13: unknown `/ui/...` paths answer with bare JSON, not an app page](13-ui-404-raw-json.md)
- [Finding 14: alarm and detail views use technical wording a landlord cannot act on](14-ui-technical-wording-for-laypeople.md)
- [Finding: No X-Content-Type-Options: nosniff](20-no-x-content-type-options-nosniff.md)
- [Finding: API responses lack hardening headers](21-api-responses-lack-hardening-headers.md)
- [Finding: Heartbeat body size is unbounded](22-heartbeat-body-size-is-unbounded.md)
- [Finding: Deeply nested JSON causes a 5xx](23-deeply-nested-json-causes-a-5xx.md)
- [Finding: Heartbeat with NaN or out-of-range numbers breaks the UI](24-heartbeat-with-nan-or-out-of-range-numbers-breaks-the-ui.md)
- [Finding 25: device tokens never expire](25-device-tokens-never-expire.md)
- [Finding 26: the landlord UI accepts a desired-state revision for an apartment that is not in pilot mode](26-ui-accepts-image-revision-without-pilot-mode.md)
- [Findings 30: first look at the tenant UI (thermoctl, headless sim instances)](30-tenant-ui-first-look.md)
- [Finding 40: the watchdog's service user is never created, so the watchdog never runs on an image built by the recipe](40-watchdog-user-never-created.md)
- [Finding 41: the pre-set watchdog state file is rejected by the watchdog](41-watchdog-preset-state-file-is-rejected.md)
- [Finding 42: the agent cuts every idle command stream after 10 s, the fleet pings every 15 s](42-agent-sse-read-timeout-shorter-than-ping.md)
