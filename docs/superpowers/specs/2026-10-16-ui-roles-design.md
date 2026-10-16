# Fleet UI: roles and per-permission access (sim finding F11)

Status: design approved by the project owner on 2026-10-16. Source: sim finding 11
(`docs/sim-findings/11-ui-has-no-roles.md`), consolidated report items F11 / A09 / S01.

## Goal

Every fleet UI user currently has the same powers. Introduce freely definable roles built from
a fixed catalogue of individual permissions, enforced **server-side per route**, plus a web page
where administrators manage roles and users.

## Owner decisions

- Roles are created freely by administrators out of single permissions (not a fixed
  admin/caretaker/landlord trio).
- Existing UI users become **Administrator** (all permissions) on migration: nobody loses access.
- Accounts are created **in the web** by invitation. This knowingly overrides the earlier owner
  decision of 2026-09-24 ("the first account is created via a CLI command, never via the web");
  the CLI `create-user` stays as the emergency path and for the first account.
- New accounts get their credentials through a one-time invitation link; the inviting
  administrator never learns the password or the TOTP secret.

## Non-goals

- The agent stays the security boundary (`CLAUDE.md` principle 5). `pilot_mode.set` protects the
  UI only; `agent/loop.py` still refuses `open_access` without `pilot_mode`. Nothing here changes
  `agent/`, `protocol/` or `CommandType`.
- No per-apartment or per-property scoping of permissions.
- No new tenant data; no audit-log feature beyond the existing server log.

## Permission catalogue

Fixed in code (`fleet/permissions.py`), unknown names stored in the database are ignored on read
(fail closed). Only roles are free, not permissions.

| Permission | Covers |
|---|---|
| `view` | overview, apartments, tasks, alarms (granted to every role, cannot be removed) |
| `faults.acknowledge` | acknowledging faults |
| `commands.send` | confirming commands, diagnostic bundle download |
| `backups.manage` | backup download and restore |
| `tenant_change` | starting a tenant change |
| `desired_state.edit` | editing/confirming the desired state revision |
| `pilot_mode.set` | toggling `pilot_mode` on an apartment |
| `inventory.manage` | properties, apartments, devices: create/edit/prepare/confirm/state/replace |
| `rollouts.manage` | creating, cancelling, resuming rollouts |
| `users.manage` | roles, invitations, role assignment, locking/deleting accounts |

Own account pages (WebAuthn, logout) need only a valid session.

## Data model and migration

- `ui_roles(id, name unique, created_at)`; `ui_role_permissions(role_id, permission)` with a
  unique pair and `ON DELETE CASCADE`.
- `ui_users.role_id` NOT NULL, foreign key to `ui_roles` (no cascade: a role in use cannot be
  deleted).
- `ui_invitations(id, username, role_id, token_hash, created_by, created_at, expires_at,
  used_at)`; only a SHA-256 hash of the token is stored.
- Alembic migration `0020`: create tables, create role "Administrator" with every permission,
  assign it to all existing users. Downgrade drops the new objects.

## Enforcement

- `require_permission(name)` is a FastAPI dependency built on `require_ui_user`; a missing
  permission answers 403 with a plain-language page (HTML for `/ui/`).
- Every state-changing UI route and every route showing sensitive content declares a permission.
  A test enumerates all routes of the UI router and fails if a POST route has no permission
  dependency or an unknown one, so a new route cannot slip through unprotected.
- Navigation and buttons hide what the user may not do. This is convenience only; the route
  always checks itself.
- Permissions are read per request from the database (no caching in the session), so removing a
  permission or a role assignment takes effect immediately.

## Users and invitations (web)

- Page "Nutzer und Rollen" (requires `users.manage`): create/edit/delete roles; invite users;
  change a user's role; lock/unlock or delete accounts.
- Invitation: administrator enters username and role and receives the link **once**. The token
  is `secrets.token_urlsafe(32)`, valid 24 hours, single use. The invitee opens it, chooses a
  password (same policy as the CLI) and enrols TOTP (secret encrypted with the existing
  `totp_crypto`, same storage as CLI-created accounts); then the account exists and the invite
  is marked used.
- The invitation endpoints are unauthenticated by nature and therefore get: IP throttle like the
  login, constant-time hash comparison, identical responses for unknown/expired/used tokens,
  CSRF protection, and `Cache-Control: no-store`.
- Lock-out protection: the last account holding `users.manage` can be neither deleted, locked
  nor demoted; a user cannot change their own role; a role in use cannot be deleted; the
  `view` permission cannot be removed from a role.
- CLI: `fleet.admin create-user --role NAME` (default: Administrator, created if missing).

## Testing

- Role x route matrix: allowed and 403 for every permission.
- Route-coverage test (above).
- Invitation: valid, expired, used twice, wrong token, throttle, username collision.
- Lock-out protection cases; migration on a database that already has users.
- Immediate effect of permission removal within the same session.

## Work split

- Main session (auth logic): `fleet/permissions.py`, `require_permission`, invitation token
  handling, lock-out protection, migration, route-coverage test.
- Agent with cross-review: the "Nutzer und Rollen" pages and templates, navigation hiding,
  applying the declarations to the routes.
- Every step cross-reviewed per `CLAUDE.md`; full test suite run by the reviewer.
