# Simulation harness: environment discovery notes (Task 1)

Date: 2026-10-11. Read-only research; facts cite `file:line` in this worktree
(`thermoctl-fleet`) unless prefixed `thermoctl:` (sibling repo
`../thermoctl`). Anything not determinable is marked UNKNOWN.

## Headline

**`python -m agent run` does not send heartbeats in this scaffold.** Later
tasks must supply their own heartbeat sender. Evidence:

- `collect_heartbeat()` raises `NotImplementedError` (`agent/loop.py:1074-1087`).
- `loop.send_heartbeat()` raises `NotImplementedError` (`agent/loop.py:1089-1112`).
- `agent.heartbeat_sender.send_heartbeat` exists (`agent/heartbeat_sender.py:135`)
  but nothing in `agent/` (apart from its own module) imports or calls it
  (grep over `agent/ fleet/ tools/ protocol/`); `agent/__main__.py:_run_agent`
  (`:71-215`) only calls `loop.run(...)`, which does command channel, backup,
  restore and reconcile threads plus the health report file, no heartbeat.
- `report_now` is answered with an honest failure (`agent/loop.py:1146-1155`,
  `_HANDLER_MESSAGE_P23`). `docs/STATUS.md` lists "P2.3 heartbeat collector
  still deferred" in many places (e.g. lines 2702, 6852).
- `tools/mac-test-vm` header claims "the heartbeat runs unmodified"
  (`tools/mac-test-vm:25`), which is not borne out by the code; only the
  command channel runs.

Consequence for the harness: a sim "agent" container = the real
`python -m agent run` (command channel, results, desired state, backup) plus
a sidecar/driver that builds `protocol.heartbeat.Heartbeat` objects and posts
them with `agent.heartbeat_sender.send_heartbeat(client, hb, apartment=...,
buffer_path=...)` using the token the registration produced, or a plain
`POST /v1/heartbeat` (204, `fleet/app.py:612`; batch `POST /v1/heartbeats`,
`fleet/app.py:660`, max 240 entries).

## (a) How the agent obtains health data; minimum stub for a container agent

- Agent -> thermoctl health: **not implemented.** `_default_health_reader`
  returns `None` ("unavailable", `agent/loop.py:2948-2960`); the reconciler
  rejects fail-closed on `None` (same comment). Real callers are meant to
  inject a reader (`reconcile_desired_state(health_reader=...)`,
  `agent/loop.py:3324`). The agent's docstrings refer to thermoctl
  `/api/v1/health` (`agent/loop.py:1079,783`), but **that endpoint does not
  exist in `../thermoctl`**: only `GET /healthz` returning
  `{"status":"ok","version":...}` (`thermoctl: thermoctl/app.py:1425-1427`);
  no `health` in `thermoctl/api/routes.py`, `docs/api.md`.
- Docker use: the agent talks to the Docker Engine API over
  `/var/run/docker.sock` via httpx (`agent/loop.py:200, 432-850`) only for
  `fetch_logs` (`thermoctl_container` default `"thermoctl"`,
  `agent/loop.py:199,1032`), container state/health, and container swaps.
  Startup does not touch Docker (`agent/__main__.py:71-150`). A missing
  socket/container is reported as an honest failed command result
  (`agent/loop.py:1195-1207`), never a crash. So **no real thermoctl
  container is needed** for registration + command channel + results.
- Minimum for a containerised agent to run (from `agent/__main__.py:78-148`,
  `agent/registration.py:87-96,282-297`, `agent/loop.py:160-171`):
  1. `agent-registration.json` (`protocol.registration.AgentRegistrationFile`:
     fleet_address (https only, `agent/transport.py:427`), certificate
     fingerprint, registration code); default `/boot/firmware/...`, override
     `--registration-file`.
  2. `--data-dir` writable (default `/var/lib/thermoctl-agent`); token stored
     there as `agent_token` (0600), private key alongside.
  3. A fleet reachable over **HTTPS with a pinned fingerprint**; TLS is never
     disabled (`agent/transport.py` docstring). In a container, the fleet's CA
     must be in the image trust store (the e2e/mac-test-vm bind-mount
     `/etc/ssl/certs/ca-certificates.crt` over `/usr/lib/ssl/cert.pem`,
     `tools/mac-test-vm.agent-compose.local.yml:63-73`) or `ca_file` (tests
     only, `agent/transport.py:406-425`).
  4. Writable paths for `--watchdog-state-file`, `--led-status-file`,
     `--health-report-file` (default `/run/thermoctl-agent/health.env`,
     `agent/loop.py:160-171`); the compose file mounts `/run/thermoctl-agent`
     and `/var/lib/thermoctl-watchdog`. No watchdog process is required; the
     agent only writes files.
  5. Optional: `--apartment-id` enables backups (`__main__.py:98-108`);
     thermoctl DB / Zigbee2MQTT dirs and `backup-recipients.txt`
     (`agent/encryption.py:63`) only for backup/restore.
- UNKNOWN: exact container uid/gid requirements for mounted dirs in the sim
  (image runs as user `agent`, `docker/Dockerfile.agent:29`; the e2e README's
  uid/gid dirs were not re-derived here).

## (b) Env vars for a fleet start

Minimum (as `tools/dev_fleet.py:72-78` and `tools/mac_test_vm/fleet_local.py`
`serve` set them):

| Var | Purpose | Source |
|---|---|---|
| `FLEET_DATABASE_URL` | SQLAlchemy URL; dev uses `sqlite:///<file>` | `fleet/storage.py:7650`, `tools/dev_fleet.py:44` |
| `FLEET_TOTP_KEY` | url-safe base64 of 32 random bytes; encrypts TOTP secrets | `fleet/totp_crypto.py:64`, `tools/dev_fleet.py:29-33` |
| `FLEET_BACKUP_STORAGE_DIR` | else retention loop logs RuntimeError each tick | `fleet/backup_storage.py:172`, `fleet_local.py` ~l.322 |
| `FLEET_DIAGNOSTIC_BUNDLE_STORAGE_DIR` | same for bundles | `tools/dev_fleet.py:76` |
| `FLEET_WEBAUTHN_RP_ID`, `FLEET_WEBAUTHN_ORIGIN` | passkeys (dev sets localhost) | `tools/dev_fleet.py:77-78` |
| `FLEET_PUBLIC_URL`, `FLEET_CERT_FINGERPRINT` | used to render the registration file shown in the UI | `fleet/ui_routes.py:1135-1143,1223-1224` |

Timing/tuning vars relevant to a sim (all in `fleet/`, grep of
`"FLEET_*"` literals): `FLEET_ALARM_CHECK_INTERVAL_S`,
`FLEET_COMMANDS_POLL_INTERVAL_S`, `FLEET_COMMANDS_SSE_PING_INTERVAL_S`,
`FLEET_COMMANDS_SSE_RETRY_MS`, `FLEET_RETENTION_HEARTBEAT_DAYS`,
`FLEET_RETENTION_FAULT_DAYS`, `FLEET_RETENTION_INTERVAL_S`,
`FLEET_LOG_RETENTION_DAYS`, `FLEET_UI_SESSION_IDLE_TIMEOUT_S`,
`FLEET_UI_SESSION_ABSOLUTE_LIFETIME_S`, `FLEET_UI_LOCKOUT_*`,
`FLEET_UI_IP_THROTTLE_*`, `FLEET_*_THROTTLE_*` (registration / token),
`FLEET_ALERT_WEBHOOK_URLS`, `FLEET_ALERT_SMTP_*`, `FLEET_ROLLOUT_WORKER_INTERVAL_S`,
`FLEET_RESTORE_KEY_BLOCK_TTL_S` (`fleet/ui_routes.py:2474`),
`FLEET_UI_PASSWORDLESS_NETWORKS`, `FLEET_UI_TRUSTED_PROXIES`.
UNKNOWN: which of these have time-based effects that a virtual clock must
drive versus ones the sim should shorten; to be decided in the clock task.

- Migrations: `fleet.storage.upgrade(url)` (`fleet/storage.py:7629`); dev
  fleet seeds via `tools.docs_screenshots.seed` (`tools/docs_screenshots.py:140`).
- First UI user: **`python -m fleet.admin create-user <name>`**, password
  read from stdin twice, prints a TOTP provisioning URI
  (`tools/docs_screenshots.py:327-345`, `tools/dev_fleet.py:48-52`).
- Run: `uvicorn fleet.app:app` (`docker/Dockerfile.fleet:19`, base
  `python:3.13-slim`, user `fleet`, port 8000, healthcheck `/healthz`).
  Dev-server flags: `python -m tools.dev_fleet [--reset --yes] [--port 8000]
  [--no-reload]`, state in `.dev/`, http on 127.0.0.1 only.
- **TLS is mandatory for any agent**: the agent refuses non-https. The
  mac-test-vm path starts uvicorn with `--ssl-certfile/--ssl-keyfile` and a
  throwaway RSA CA (`tools/mac_test_vm/fleet_local.py:63-190,
  ~326-340`); `tools/e2e/gen_test_ca.py` does the same for e2e.

## (c) Registration: creation and confirmation

Reusable helpers in `tools/mac_test_vm/fleet_local.py`:

1. `ensure_database(path)` runs real Alembic (`:191`).
2. `ensure_test_apartment_and_device(database_url)` (`:206-278`): via
   `Storage` creates property/apartment, `register_device` (`:245`) and
   `prepare_device` (`fleet/storage.py:5063`, returns the one-time raw
   registration code). IDs are fixed constants (`:46-48`: `mac-test-vm-apt1`,
   `mac-test-vm-device1`), so the sim needs to parametrise this per
   apartment (call the same `Storage` methods in a loop).
3. `write_registration_file(...)` (`:280`) writes the
   `AgentRegistrationFile` JSON with the code, fleet address and TLS
   fingerprint (`agent.transport.fingerprint_for_certificate`).
4. Agent: `python -m agent register --registration-file ... --data-dir ...`
   submits, prints a verification code, then **blocks polling** until a human
   confirms (`agent/__main__.py:30-68`, `tools/mac-test-vm:119-235`).
5. Confirmation: in the UI at `/ui/inventory` ("Bestaetigen"), or
   programmatically `Storage.confirm_device` (`fleet/storage.py:5303`; same
   call the UI makes, `fleet/ui_inventory.py`). Not verified here which args
   it needs: UNKNOWN (read signature in the clock/fleet-setup task).
6. After confirmation the agent receives its token (signed challenge,
   `agent/registration.py:26-29,78-83`), stores `agent_token`; `register` is
   idempotent and short-circuits if a token exists (`:31-35,337-339`).
   Then `python -m agent run --registration-file ... --data-dir ...`.

## (d) Starting the tenant UI of `../thermoctl` headless

- Image/entrypoint: `thermoctl: docker/entrypoint.sh:60-67` chowns
  `$THERMOCTL_DATA_DIR` (default `/data`), drops to user `thermoctl`, runs
  `alembic upgrade head` then `exec thermoctl`. `thermoctl.cli:main` runs
  `uvicorn thermoctl.app:create_app` (factory) on `bind_host`/`bind_port`
  (defaults `0.0.0.0:8000`, `thermoctl/config.py:22-23`). Healthcheck
  `GET /healthz` (`thermoctl/app.py:1425`).
- Compose example: `docker/compose.beispiel.yml` = image
  `ghcr.io/magicalwig34653/thermoctl:latest`, `env_file: .env`, port
  `127.0.0.1:8000`, volume `/data`. Env: `THERMOCTL_DATABASE_URL`,
  `THERMOCTL_SECRET_KEY` (required, `.env.example`), `THERMOCTL_BIND_*`,
  `THERMOCTL_SECURE_COOKIES` (false default; needed for plain http),
  `THERMOCTL_MQTT_ENABLED` (default false, so no broker needed), optional
  `THERMOCTL_MCP_TOKEN`, `THERMOCTL_PASSKEY_*`.
- No pre-built local image was found (`docker images` lists only
  debian/python bases); build from `thermoctl: docker/Dockerfile` or run
  from source (`thermoctl` console script after `alembic upgrade head`).
  UNKNOWN: whether the GHCR image is pullable without login; not tried.
- First-run setup: when the DB has no user, the app creates a **setup token
  and writes it to the log** (`thermoctl/app.py:996-1003`, `thermoctl/setup.py:
  47-67`, lifetime 1 h); `GET/POST /setup` form takes `username`,
  `display_name`, `password`, `timezone`, `setup_token`
  (`thermoctl/web/setup_views.py:30-60`), CSRF protected
  (`router dependencies=[Depends(csrf_protection)]`, `:18`). Example groups
  incl. a tenant template "Wohnung" (`thermoctl/setup.py:25-45`).
  Headless sim therefore: scrape token from container logs, POST setup via
  a CSRF-aware client (or Playwright).

## (e) Lima VMs

`limactl list` (2026-10-11):

| Name | Status | Type/Arch | CPU/Mem/Disk |
|---|---|---|---|
| `thermoctl-e2e-basestation` | Stopped | vz aarch64 | 2 / 3GiB / 8GiB |
| `thermoctl-test-vm` | Stopped | vz aarch64 | 4 / 4GiB / 20GiB |

- `thermoctl-test-vm` is created from `tools/mac-test-vm.lima.yaml`
  (Debian 13 genericcloud arm64, 4 CPU/4 GiB/20 GiB, repo mounted read-only
  at `/thermoctl-fleet`, provisions `image/common/install.sh`, docker, go).
- Whether it can be cloned: `limactl clone` is the mechanism; NOT executed
  (read-only task, VM stopped). UNKNOWN: provisioning state of the existing
  instance (docker present? image built?) because it is stopped. The VM
  template's mount location is replaced per checkout by `cmd_create`
  (`tools/mac-test-vm:cmd_create`), so a clone mounts the **main
  checkout's** path, not this worktree: re-point the mount if the sim needs
  worktree code. `host.lima.internal` is the host alias the VM uses to reach
  the Mac fleet (`fleet_local.py:63` default CN).
- Docker contexts: `colima` (current) and `default`; unrelated containers
  (postgres/s3mock/mariadb) are running; sim containers must stay prefixed
  and isolated.

## libfaketime check (Step 2)

`docker run --rm debian:trixie-slim` (aarch64, Debian 13), `apt-get install
faketime python3`:

- Library: **`/usr/lib/aarch64-linux-gnu/faketime/libfaketime.so.1`**
  (amd64 would be `/usr/lib/x86_64-linux-gnu/faketime/...`; resolve with
  `find /usr -name libfaketime.so.1`, the multiarch dir differs).
- `echo "2026-01-05 08:00:00" > /tmp/ft; FAKETIME_NO_CACHE=1
  FAKETIME_TIMESTAMP_FILE=/tmp/ft LD_PRELOAD=<lib> python3 -c ...` printed
  `2026-01-05 08:00:00` and `time.time()==1767600000.0` **identically on two
  runs 2 s apart: absolute timestamp without `@` freezes time**, and the
  clock can be advanced by rewriting the file (`FAKETIME_NO_CACHE=1` makes
  it re-read).
- With `@` prefix in the file: starts at that date but **ticks in real time
  and restarts at the given value for each new process** (microseconds
  advancing, 08:00:00.008 then .005): not suitable for a persistent
  virtual clock across processes.
- `FAKETIME="@2026-01-05 08:00:00 x10"` runs at 10x speed (observed 2 s
  became 20 s). Alternative for acceleration.
- Not tested: effect on a running long-lived uvicorn/Python process picking
  up file changes (expected with NO_CACHE), `time.monotonic()` (libfaketime
  does not fake monotonic by default; asyncio/httpx timeouts use it),
  `python:3.13-slim` (Debian-based, same package expected). UNKNOWN until the
  clock task verifies in the actual images.

## Python and tooling in the project environment

- `pyproject.toml`: `requires-python >= 3.13` (`:4`); base dependency
  `pydantic`; extras `fleet`, `agent`, `flash`, `docs`, `dev`.
- The worktree has no `.venv`; `python3`, `ruff`, `mypy`, `pytest` on PATH
  resolve to the **main checkout's** `.venv`
  (`.../thermoctl-fleet/.venv/bin/*`), Python **3.14.6**. Container images
  use `python:3.13-slim` (`docker/Dockerfile.fleet:3,10`,
  `docker/Dockerfile.agent:5,16`): the sim code run inside containers must
  be 3.13-compatible.
- Installed in that venv: pytest 9.1.1, ruff 0.16.8, mypy 2.3.1,
  httpx 0.28.1, httpx2 2.13.0 (used for TestClient, `pyproject.toml` dev
  extra), httpx-sse 0.4.3, playwright 1.63.0 (+ pillow 12.3.0; Chromium
  builds present in `~/Library/Caches/ms-playwright`: chromium-1228..1248),
  fastapi 0.141.1, uvicorn 0.53.0, pyrage 1.4.0, webauthn 3.0.1, textual
  8.2.8, aiosmtpd 1.4.6. `playwright` comes from the `docs` extra
  (`pyproject.toml`: `docs = ["playwright>=1.63,<2", "pillow..."]`), `httpx`
  from `fleet`/`agent`.
- Other host tools: `limactl` and `docker` in `/opt/homebrew/bin`; no
  GNU `timeout` on macOS.

## `claude` CLI flags for headless runs (claude 2.1.296)

From `claude --help` (the user shell wraps `claude` in a retry function;
use `command claude` or the binary directly in scripts):

- `-p, --print`: non-interactive; skips workspace trust dialog.
- `--model <alias|full-name>`: aliases e.g. `sonnet`, `opus`, `fable`
  (help text also names `haiku` in the Agent tool; `--model haiku` not
  verified by a call here: UNKNOWN until a smoke run).
- `--effort <low|medium|high|xhigh|max>`.
- `--output-format text|json|stream-json` (print only); `--input-format
  text|stream-json`; `--json-schema <schema>` for structured output;
  `--include-partial-messages`, `--verbose`.
- Tools/permissions: `--allowedTools`/`--allowed-tools <list>` (e.g.
  `"Bash(git *) Edit"`), `--disallowedTools`, `--tools <list>` (`""`
  disables all, `default` all), `--permission-mode
  acceptEdits|auto|bypassPermissions|manual|dontAsk|plan`,
  `--permission-prompts host|none` (with `none`, anything that would prompt
  is auto-denied), `--dangerously-skip-permissions`.
- Isolation/cost: `--max-budget-usd <n>` (print only), `--no-session-persistence`
  (print only), `--bare` (skip hooks, CLAUDE.md discovery, plugins, keychain;
  auth strictly `ANTHROPIC_API_KEY`), `--setting-sources user,project,local`,
  `--settings`, `--strict-mcp-config` + `--mcp-config`, `--system-prompt`,
  `--append-system-prompt`, `--disable-slash-commands`, `--add-dir`,
  `--session-id <uuid>`, `--fallback-model`.
- Not found in `--help`: a `--max-turns` flag (grep over the help output
  returned nothing), so turn limits must be enforced by budget or prompt.
