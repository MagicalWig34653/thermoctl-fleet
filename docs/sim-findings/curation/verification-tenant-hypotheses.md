# Verification: tenant hypotheses H1 (T04), H2 (T11), H3 (S07)

Method: code reading in the sibling `thermoctl` repo and in this repo; no POSTs, no harness, no product code changed. A live reproduction of H1 was not done because it needs a state-changing POST on a sim instance (out of scope).

## H1 (T04): do tenant POSTs redirect before the commit, like login (finding 50)?

Verdict: **PRODUCT_DEFECT (by code identity with finding 50; not reproduced live for these actions)**. Severity suggestion: **minor** (self-healing, lower impact than login). Not the main cause of the T04 rows (those are mostly T01/T06 harness failures).

Evidence
- Commit mechanism: `thermoctl/auth/dependencies.py:29-45` `get_session` is a yield dependency; `commit()` at :39 runs in the teardown after the handler returned. No view commits itself: `grep "\.commit()" thermoctl` finds commits only in `auth/dependencies.py:39`, `db/engine.py:117` (`session_scope`, not used by these views), `integrations/actuators.py` and `services/publishing.py` (not request paths). `create_override` (`thermoctl/domain/schedule.py:714`) only adds/flushes.
- Same dependency, same teardown timing as login, which finding 50 reproduced (30 immediate follow-ups: 14 bad; with 0.5 s pause 0 bad; FastAPI 0.141/Starlette in the venv).
- Per action, redirect to `/` with a 303 and no explicit commit:
  - override create: `thermoctl/web/daily_views.py:483` (also error branches :464, :482); override cancel `:486-495`; thermostat +/-: `:554`; jump-next `:557`. Affected: the follow-up `GET /` can lack the "Vorübergehend X °C bis HH:MM" banner or show the old SOLL.
  - absence set/end: tenant views (redirect `:606` region of the same router family); schedule save `POST /schedule/day` (`thermoctl/web/tenant_views.py:399`) and copy-day (`:551`): the follow-up page can show the old plan (this is the "saved but still old" residual in T09).
  - report: `thermoctl/web/report_views.py:55-135`: only the audit row is delayed; the notice travels in the query string (`_back`, :130-135), so the visible confirmation does not depend on the DB. No visible effect.
- Mitigation that bounds the impact: the tenant page polls itself (`tenant_start.html:29`, `hx-get ... every {{ poll_interval_seconds }}s`), so a stale render corrects itself at the next poll. The mutation is never lost (the commit still happens); only the first render after the POST can be stale. Unlike login there is no dead end.
- Also true: success has no toast by design (`tenant_start.html:210-214` banner only); "dialog stays open" cannot happen on success (T04 text in cluster-tenants.md).

Repro recipe (not run): on a sim tenant, POST `/zones/<id>/override` with CSRF + cookie, then immediate `GET /` x30 without delay, count pages lacking the banner; compare with 0.5 s pause.

Suggested direction (product, not done): call `session.commit()` before returning the RedirectResponse in mutating tenant views, or make `get_session` commit before the response is sent; one fix with finding 50. Propose to extend finding 50 rather than a new finding.

## H2 (T11): ~1000-day-old readings shown with OK status

Verdict: **HARNESS_ARTEFACT** for the observed rows, with a **minor hardening residual (PRODUCT_UX/robustness)**. Severity suggestion: nit/minor.

Evidence
- The status is stored but derived from age by the control cycle: `thermoctl/services/ingest.py` `advance_zone_state` (loop at ~:385-440) calls `sensor_state(measurement.measured_at, now, sensor_timeout_seconds)` (`thermoctl/domain/fault.py:47-59`: `OK` if age <= timeout, else `VERALTET`) and writes `ZoneState.sensor_status_id`. Called from the periodic cycle `thermoctl/app.py:687`.
- The tenant page does not re-derive: `thermoctl/web/start_views.py:109+` joins `ZoneState` with `SensorStatus`; `tenant_start.html:165-170` shows "Anzeige kann veraltet sein" only if the stored code is not `ok`, otherwise "gemessen {{ measured_at|age }}" (age is computed at render time against the clock).
- So "gemessen vor ~1000 Tagen" + OK arises only when the stored status was last computed while the reading was fresh and the cycle has not run since, while the page clock moved on. That matches the sim: frozen/jumped virtual clock and sparse readings, cycle not re-run per virtual step (see also the frozen-clock note in verification-stuck-absence-alarm.md). A production sensor reports every few minutes and the cycle runs continuously, so status flips to `veraltet` after `sensor_timeout_seconds`.
- What a real defect would look like: control cycle stalled or crashed for hours/days (scheduler thread dead, DB lock) while the web process keeps serving. Then the tenant sees an old reading as OK with a growing age string, "GERADE" heating chip from a stale decision, and the stored status never flips. Cheap hardening: derive the stale flag at render time from `measured_at` and the zone timeout (same `sensor_state` call) instead of trusting the stored code; the age text already exposes the contradiction, so this is robustness, not a data-correctness bug.
- Not verified live (login needed on 127.0.0.1:8301-8303); the stored-status path is conclusive from code.

## H3 (S07): "room and setpoint are sent automatically" claim

Verdict: **NOT_REPRODUCED as a defect (claim accurate; no principle violation)**. One residual **NEEDS_MORE** point on wording. Severity suggestion: nit (wording), no security issue.

Evidence
- The form text: `thermoctl/web/templates/tenant_start.html:~288-291` "Raum, letzter Messwert, Sollwert und Zeitpunkt werden automatisch ergaenzt. Technische Angaben sind nicht noetig." True: `thermoctl/domain/problem_report.py:107-166` `build_report` builds a `FaultNotice` text with room, problem, reporter, time, last reading, setpoint, mode and note (:144-152).
- Where it goes: `report_views.py:55-127`: audit row (local DB) plus delivery via the operator's own `notify_webhook` (`notification.deliver`), or the honest "aufgenommen, keine automatische Weiterleitung" when no webhook is set (:100-119). The text goes to the landlord's webhook, as the form implies ("die Meldung verlaesst die Anlage ueber den Webhook des Betreibers", template comment ~:280).
- Fleet side: thermoctl contains no fleet client (no `fleet` reference in `thermoctl/*.py`). The fleet only learns of the report if the agent/webhook path forwards the event envelope. `protocol/events.py:36-52` and `FaultEvent` (:94-119) state that the fleet builds its message from `kind`/`key` only and never stores `Event.titel`/`Event.text`; `FaultKind.TENANT_REPORT` (`protocol/heartbeat.py:57`, `protocol/events.py:143` prefix `tenant-report:`) carries only the zone-bound key `tenant-report:<zone_id>:<kind>`. So room temperature, setpoint, tenant name and note do not reach the cloud; section 6 principle holds at the protocol level.
- Residual: the key contains the zone id and problem kind (a tenant-report occurred in zone N), which is operational metadata, not temperature/setpoint/tenant data. Whether a real deployment points `notify_webhook` at the fleet's event endpoint (where `Event.text` is accepted then dropped after validation, `protocol/events.py:79-91`) is a configuration matter outside this repo; the text would still transit the fleet's validation. Worth stating in the form or ops docs that the webhook is the landlord's channel, not the fleet; no code change required.

## Summary

| Hypothesis | Verdict | Severity |
|---|---|---|
| H1 T04 post-redirect race on tenant POSTs | PRODUCT_DEFECT (code; extend finding 50) | minor |
| H2 T11 old reading shown OK | HARNESS_ARTEFACT (+ robustness hardening) | nit/minor |
| H3 S07 report claim | NOT_REPRODUCED (claim accurate, text not in cloud) | nit |
