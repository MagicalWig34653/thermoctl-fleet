# Finding 11: every fleet UI user has the same powers; landlord, caretaker and admin are the same role

- **Area:** security / logic
- **Severity:** major
- **Source:** Task 9a (code reading), confirmed live in the Task 10 smoke run

## Expected

The simulated personas (landlord, caretaker, admin) and the specification's
split of duties imply different rights: an administrator reviews users, retention
and rollouts; a caretaker runs maintenance; a landlord looks and hands flats over.
At the very least the pilot-mode flag that unlocks `open_access`
(specification section 21.4) should not be editable by the lowest role.

## Actual

`UiUserRecord` (`fleet/storage.py:371`) has `username`, `password_hash`, TOTP and
nothing else: there is no role or permission column. Three accounts created for
the smoke run (`sim-landlord`, `sim-caretaker`, `sim-admin`) therefore see the
identical UI. The "landlord" persona saw, on the apartment edit form, the
checkbox "Pilotbetrieb (schaltet den Agent-Befehl "open_access" frei)", the
rollout page ("Neuer Rollout") and the device confirm flow.

## Evidence

- `fleet/storage.py:371-390`: the `ui_users` table has no role column.
- Task 10 run, week 0, persona `landlord` (raw/findings.jsonl): "Landlord edit
  form exposes the pilot-mode toggle for the open_access command".
- Same navigation and the same pages for all three logins (`browse text --role
  landlord|caretaker|admin`).

## Suggested direction

Decide with the project owner whether the UI should have roles at all. If yes:
a role column, enforced server-side per route (not by hiding buttons), with the
pilot-mode flag and device confirmation restricted to the administrative role.
The agent-side `pilot_mode` check stays the real boundary either way.
