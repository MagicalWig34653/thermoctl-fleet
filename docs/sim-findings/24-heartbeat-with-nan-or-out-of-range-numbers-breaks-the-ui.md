# Finding: Heartbeat with NaN or out-of-range numbers breaks the UI

- **Area:** security
- **Severity:** major
- **Source:** pentest check G07 (tools/sim/pentest), run against the sim

## Expected

Out-of-range numbers (negative, > 100 %) are 422; NaN and 1e999 are refused too, or -- if accepted -- every UI page still renders (no stored value may break the overview).

## Actual

statuses {'uptime_s<0': 422, 'memory>100': 422, 'zones<0': 422, 'battery>100': 422, 'NaN': 204, '1e999': 204}; UI pages that did not answer 200 after NaN/1e999: {'NaN:/ui/': 500, 'NaN:/ui/tasks': 599, 'NaN:/ui/apartments': 500, 'NaN:/ui/apartments/sim-a12': 599, '1e999:/ui/': 500, '1e999:/ui/tasks': 599, '1e999:/ui/apartments': 500, '1e999:/ui/apartments/sim-a12': 599}

Root cause from the fleet log: `POST /v1/heartbeat` accepts `"clock_drift_s": NaN` (and `1e999`, which becomes infinity) because the `float` field allows it; `save_heartbeat` serialises the value as JSON `null`; `Storage.get_latest_heartbeat` then runs `Heartbeat.model_validate_json(row.payload_json)` on the stored row and raises `ValidationError: system.clock_drift_s Input should be a valid number [input_value=None]`. Every page that builds the house overview (`/ui/`, `/ui/tasks`, `/ui/apartments`, the apartment page) answered 500 or dropped the connection until a later heartbeat of that apartment replaced the row. In production the next honest heartbeat heals it; a compromised device that keeps sending the poisoned value keeps the landlord's whole overview down. Needs a valid agent token of any one apartment.

## Evidence

```
POST /v1/heartbeat as sim-a12 with out-of-range numbers, then NaN and 1e999 in system.clock_drift_s; GET /ui/ /ui/tasks /ui/apartments and the apartment page
```

## Suggested direction

Reject non-finite floats at the model: `Field(allow_inf_nan=False)` on `clock_drift_s` (the only free float), or `model_config = ConfigDict(allow_inf_nan=False)` on `Heartbeat`, so the 422 happens at ingest. Independently, make the read side tolerant: if a stored `payload_json` no longer validates, `get_latest_heartbeat` should skip or flag that row instead of raising, so one bad row cannot take down the overview, the task list and the apartment pages for every user.
