# Finding 50: the post-login redirect can arrive before the new session is committed

- **Area:** thermoctl tenant UI (sibling `thermoctl`, `POST /login`), found through the simulation's tenant login refresh
- **Severity:** minor (the user is bounced to the login form after a correct login; a second attempt works)
- **Source:** Task 13 (tenant login refresh failure, run 2 week 7), reproduced live against `thermoctl-sim-tenant1`

## Expected

A correct login ends on the start page. When `POST /login` answers `303` with a `Set-Cookie`,
the session behind that cookie is already visible to the very next request.

## Actual

Intermittently the browser follows the `303` to `GET /` with the fresh session cookie and gets
`303 -> /login` (no valid session). Server log: `POST /login 303`, `GET / 303`, `GET /login 200`.
No `login_failed` audit entry (the login itself succeeded), which is why run 1's audit logs showed 0 failed logins.

## Evidence

- `thermoctl/web/auth_views.py` `login()` creates the session with `create_session()` (only `flush()`) and returns the
  `RedirectResponse`. The commit happens in `get_session` (`thermoctl/auth/dependencies.py`), a yield dependency whose
  teardown runs after the response is sent (FastAPI 0.143 / Starlette 1.8 in the container, `fastapi>=0.141` pinned).
  A fast follow-up request can hit `resolve_session()` before that commit.
- Live reproduction (scratch, tenant1): POST /login then immediately GET / with the returned cookie, 30 times: 16 ok, 14 bad.
  Same with a 0.5 s pause between the two: 30 ok, 0 bad. Playwright (click or `form.submit()`): about 3 of 8 logins end on `/login`,
  with `GET /` carrying the cookie (the cookie is stored with a correct `Max-Age=1209600`; cookies are not the problem).
- The `session` table of tenant1 holds an unused-looking valid row for every failed attempt (ids 9, 10, 11 on 2026-02-23, `expires_at` 2026-03-09, not revoked).
- Tenant1 log over the whole run: 25 of 87 `POST /login 303` were followed directly by `GET / 303`.
- Not date dependent: the failing week (2026-02-23) and the good weeks differ only by chance; the same race occurred on 2026-01-05 and again in my probes on 2026-03-02.

## Suggested direction

Commit the session before the response leaves: in `login()` call `session.commit()` explicitly after `create_session()` and the audit entry
(and the same in `passkey_views.py` login and `kiosk_views.py` token exchange, which share the pattern), or make `get_session`
commit before the response is returned. Add a test that issues the follow-up request immediately.
