# Finding 25: device tokens never expire

- **Area:** security
- **Severity:** minor
- **Source:** Task 13-prep, static review of the token handling (no live run)

## Expected

A long-lived credential on a device that sits in a tenant's apartment for years has some
bound: a maximum age, or at least a visible reminder when a token has been in use for a long
time, so a leaked token does not stay valid indefinitely.

## Actual

The agent token (`agent_<apartment>_<random>`) has no expiry. Only the hash is stored
(`ApartmentRecord.token_hash`, `fleet/storage.py:152`) and every authenticated call looks it up
by hash (`Storage.get_apartment_id_by_token_hash`, `fleet/storage.py:1606`; `fleet/auth.py:170`),
without any age check. The only `expires_at` / `*_expires_at` fields next to tokens belong to
other things: the one-time setup code (24 h, `fleet/storage.py:638`), the token-issue nonce
(`token_nonce_expires_at`, `fleet/storage.py:682`) and the rotation nonce
(`rotation_nonce_expires_at`, `fleet/storage.py:190`). A token stays valid until it is
replaced: by a tenant change (`Storage.rotate_apartment_token_for_tenant_change`,
`fleet/storage.py:6315`), by the agent-driven rotation (`issue_token_rotation_challenge`,
`fleet/storage.py:6448`, `complete_token_rotation`, `fleet/storage.py:6480`), or by revocation.
Nothing starts a rotation on a schedule, and the UI shows no token age.

This matches the specification: section 4 lists initial registration (code with 24 h limit),
rotation on demand and revocation, and names no token lifetime. So this is a design choice,
not a deviation; the owner decides whether it is acceptable.

## Evidence

- `fleet/storage.py:152` (`token_hash`), `:1606` (lookup by hash), `:6315`, `:6448`, `:6480`
  (the only ways a token changes).
- `fleet/auth.py:170` (the lookup is the whole check).
- Search for any age bound on the apartment token: no column on `ApartmentRecord` records
  when a token was issued (`token_issued_at` exists only on `DeviceRegistrationRecord`,
  `fleet/storage.py:667`, as the single-issue guard).
- `docs/specification.md` section 4 ("Rotation", "Revocation").

## Suggested direction

If the owner wants a bound: keep the current behaviour as default, add an optional maximum
age (record the issue time of the current token) and surface "token older than N days" as a
rotation reminder in the landlord UI first; enforcement (rejecting an old token) only after
the agent can rotate on its own schedule, so an aged-out device cannot lock itself out.
