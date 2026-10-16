# VM base stations: live run (Task 9d)

Date 2026-10-11, Mac (Apple silicon), Lima 2.2.0 / vz, Colima for Docker, fresh throw-away
`SIM_STATE_DIR` under the home directory, fleet only (`up_fleet`, no container agents).

## What ran

Two Lima VMs `thermoctl-sim-a01` / `-a02` (2 CPU, 3 GiB, 8 GiB, Debian 13 arm64), created from
`tools/mac-test-vm.lima.yaml`, provisioned by the unmodified `image/common/install.sh` (Docker,
the Go watchdog build, systemd units, tmpfiles), the repo mounted read-only at
`/thermoctl-fleet`, `<state>/clock` at `/sim-clock` and `<state>/faults` at `/sim-faults`
(both read-only). `thermoctl-agent:local` was built inside each VM from `docker/Dockerfile.agent`.

| Step | Result |
|---|---|
| `vms --up` from nothing, per VM (create, boot, provision, build image, enrol, stack up) | about 3 min each (a01 ended 15:00:33, a02 15:06:04, started 3 min earlier) |
| `python -m agent register` (real), device report -> code from the fleet DB equals the code the agent printed -> `confirm_device` as `sim-admin` -> signed-challenge token issued to the agent | pass for both; codes `JSC0-P12W`, `TE1K-KQF3` |
| `python -m agent run` in the VM, https to `host.lima.internal:8243`, pinned fingerprint | running; it finds no SSE stream, see finding 42 |
| Sim heartbeat sender in the VM (token read from the agent's data dir, read-only) | `up_fleet --check` shows both apartments with virtual timestamps |
| Idempotent `--up` (second call) | skips both VMs ("already enrolled"), no create/start/register |
| `heartbeat_loss` sim-a01 (`docker pause` of the sender in the VM), clock +10 min | `not_reporting` alarm raised, `raised_at` 08:10 |
| `heartbeat_restore` | alarm cleared once the virtual clock moved past the raise time (+1 min, `cleared_at` 08:11; with a frozen clock the same instant does not clear, an artefact of the frozen sim clock) |
| `power_loss` sim-a01 and sim-a02 (`limactl stop --force`, `limactl start`) | VM booted ~2 s after the stop, the real agent and the sender were started by `thermoctl-sim-stack.service` ~6 s after the stop, first heartbeat ~7 s after the stop (a02: stop 13:06:28Z, boot 13:06:30, containers 13:06:34, heartbeat 13:06:35); `limactl start` itself returned after ~12 s. The Lima clock mount was readable after boot. |
| `wifi_change` sim-a01 (default interface down 8 s, up) | sender logged `ConnectError`/`RemoteProtocolError` during the gap, 204 again after; NetworkManager adds a second default route (metric 100 next to 200) |
| Command from the fleet (`create_command_unless_duplicate`, the call behind the UI form): `fetch_logs` to a01, `report_now` to a02 | delivered to the real agent, result came back (`successful=false` with honest reasons: no thermoctl container; "Herzschlag-Erfassung noch nicht verfügbar (P2.3)"), after 30 s and 48 s (finding 42) |
| SSE through the TLS proxy | a 60 s read from inside the VM got `: ping` every 15.0 s, no buffering (the unit tests also prove chunk-by-chunk passing) |
| `--down` | only the two VMs deleted; proxy stopped |

## What is NOT exercised

- The digest-based swap: the local image has no registry digest (`mac-test-vm` has the same
  limit); the VM runs `thermoctl-agent:local` through `tools/sim/env/vm/compose.vm.yml`
  (restart "no", started after boot by `thermoctl-sim-stack.service`), not through the
  watchdog. The real watchdog binary is built and installed, but its unit never starts
  (finding 40) and its preset state file is unreadable (finding 41).
- thermoctl, Zigbee2MQTT, WireGuard, backups, the hardware watchdog, Wi-Fi/NetworkManager
  first-boot, real hardware. `docker.sock` is the VM's own.
- Heartbeats are the sim sender's (finding 10), not the agent's. `backup`, `token_expiry` are
  still skipped for the VMs; `image_update`/`tenant_change` act on the fleet only.
- Time: only the containers see the virtual clock (libfaketime reading the mounted file).
  The VM's kernel, systemd, journald, Docker, chrony and the TLS handshake of the host side use real
  time. The monotonic clock is not faked (timeouts run in real time). Code that reads the
  real clock outside the two containers is not covered.

## Product problems found

40 (watchdog user missing), 41 (preset state file rejected), 42 (SSE read timeout shorter than ping).
Also confirmed: finding 10 (the real agent answers `report_now` with "not yet available").
