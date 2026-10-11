# Finding 26: the landlord UI accepts a desired-state revision for an apartment that is not in pilot mode

- **Area:** ux
- **Severity:** minor
- **Source:** Task 13-prep, static review of the desired-state flow (no live run)

## Expected

When the landlord prepares a new image revision for an apartment whose `pilot_mode` is off,
the UI says so before or after saving: this apartment will not apply it.

## Actual

`POST /ui/apartments/{id}/desired-state/confirm` (`fleet/ui_routes.py:2039`) validates the
form and creates the revision (`Storage.create_desired_state_revision`,
`fleet/ui_routes.py:2100-2101`) without looking at the apartment's `pilot_mode`, then
redirects to the apartment page (`:2111`). Neither the edit page nor the confirm page mentions
`pilot_mode`; the flag only appears on the inventory list and edit forms
(`fleet/templates/ui/inventory.html:37`, `inventory_apartment_edit.html:42`). The agent then
holds the revision but does nothing with it:
`reconcile_desired_state` returns "pilot_mode is not set for this apartment -- desired-state
reconciliation stays inactive" (`agent/loop.py:3175-3180`) and the reconcile loop returns
without a Docker call (`agent/loop.py:2310-2314`). The landlord sees a saved revision that
silently never converges.

This is correct from a security view: CLAUDE.md principle 5 and specification section 13
(`docs/specification.md:415`) put the check in the agent, not the UI, so a UI refusal would
add nothing and must not be the protection. The gap is purely informational.

## Evidence

- `fleet/ui_routes.py:2039-2113` (the route; no `pilot_mode` use between the form and the
  storage call), `fleet/ui_routes.py:2165` (the flag is only read for the rollout form's
  apartment list, `fleet/templates/ui/rollout_new.html:66`).
- `agent/loop.py:3175` and `agent/loop.py:2310`.

## Suggested direction

UX only, no gate: on the desired-state edit/confirm pages and on the apartment page show a
note when `apartment.pilot_mode` is false ("This apartment is not in pilot mode and will
refuse the update."), and still allow saving. Do not turn it into a server-side refusal.
