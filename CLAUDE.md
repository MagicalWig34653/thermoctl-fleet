# CLAUDE.md

Working instructions for Claude Code in this repository.

## What this is

`thermoctl-fleet` is the landlord's overview across all apartments: a cloud
service (`fleet/`) that receives a heartbeat with health data from every
apartment, collects faults, alarms on **absence** of a heartbeat, and can
send a short, closed list of maintenance commands to an agent (`agent/`) on
the apartment's base station. `protocol/` is the shared contract between
both sides. Plus two further parts, neither with its own Docker image:
`watchdog/` (in Go, swaps the agent container, runs outside the container
runtime, own CI track) and `image/` (the recipe for the base station's
prepared system images).

**[`docs/specification.md`](docs/specification.md) is authoritative.** It is
an unchanged copy of a local, unpublished document and the only binding
source for field names, flows, and reasoning. Whenever in doubt: read it
there, don't guess. The current status is in
[`docs/STATUS.md`](docs/STATUS.md) -- check the scaffold's open points there
first, before reporting a gap as a bug.

## What the service explicitly is not (specification, section 1)

- **Not a second controller.** Setpoints, schedules, frost protection, and
  arming stay in the apartment. The cloud cannot change them -- not "not
  planned", but "the command does not exist". There is no way to retrofit
  this via an extension of the command list without first explicitly
  clearing it with the project owner.
- **Not a data collector.** Room temperatures, setpoints, schedules, absence
  periods, tenant names and contact details are **not** transmitted
  (section 6). A field that carries one of these categories does not belong
  in `protocol/` -- not even "just for a chart" or "just optional".
- **Not a replacement for the apartment view.** The tenant never sees the
  cloud.

## Security principles (do not renegotiate)

1. **The command list is closed.** `protocol.commands.CommandType` contains
   exclusively the stage-1 commands released by the specification. A new
   value there is never a small addition -- it extends what a potentially
   compromised fleet server can command an apartment to do. Stage 2
   (section 7) only after explicit approval by the project owner, after a
   heating season of operational experience, as the specification requires.
2. **Image sources are hard-coded in the agent, not in the cloud.** The
   prefix list of allowed registries lives as a constant in the `agent`
   package. The cloud only names version and digest, never the source
   (section 13). No digest, no start -- "latest" or a tag without a digest
   is rejected, without exception.
3. **No private keys in the cloud.** WireGuard and device key pairs are
   generated on the base station and never leave it; the cloud only ever
   sees public keys (sections 14, 15.3). An endpoint or a model that accepts
   or returns a private key is a design error, not a feature.
4. **No tenant data in plain text in the cloud.** Operational-data backups
   (thermoctl database, Zigbee2MQTT device table) are encrypted on the
   device before being uploaded; the cloud stores only the opaque block and
   never the key (section 15.1). Device configuration with no tenant
   relation may be stored in plain text -- the two kinds of backup must not
   be mixed.
5. **The agent is the security boundary, not the cloud.** Every check on
   whether a command gets executed (id already seen? expiry exceeded?
   precondition for a desired-state change met?) belongs in `agent/` and is
   enforced there, even if the cloud says otherwise. Concrete example in
   `agent.loop.open_access` (section 21.4): if an apartment lacks the
   `pilot_mode` flag, **the agent** rejects the `open_access` command -- not
   the fleet UI. A compromised cloud can therefore not open an SSH session
   in production apartments. Moving this check into the UI instead of the
   agent (e.g. "the button is greyed out anyway") would cancel out exactly
   the protection it was built for.
6. **The watchdog knows no network and no registry, and its `go.mod` stays
   without a single dependency.** Division of labor from section 17/18.3:
   the agent downloads a new image and checks its digest against the
   hard-coded sources (principle 2 above) -- the watchdog afterward only
   swaps between two already locally present, already checked digests. It
   is written in Go, statically built, under 300 lines -- exactly because it
   has to be the one thing that still works when everything else is broken
   (section 18.3). A `go.mod` entry for a third-party library (even a
   Docker SDK "just to address the runtime") or watchdog code that opens a
   network connection or addresses a registry violates this principle
   regardless of how small the change looks.

Changes to any of these six points are security-relevant in the sense of
principle 7 from thermoctl's `CLAUDE.md` (adopted below) and are read back
in the main session, not only in cross-review.

## Working method

Adopted from [thermoctl's `CLAUDE.md`](../thermoctl/CLAUDE.md), the "Working
method" section -- only the core points here, the original there governs in
case of doubt:

- **Tasks go to agents, not to the main session.** Only these stay in the
  main session: auth and security logic (see the six points above), merging
  branches and collection files, reading back security-relevant work,
  breaking work down into tasks.
- **Review crosswise.** Whoever implemented does not review. Every review
  runs the test suite itself (ruff, mypy, pytest) and reports the result
  verbatim -- the implementer's own report alone does not count.
- **One worktree per task**, its own branch, merge after the review passes.
- **Every completed change gets committed**, together with the updated
  `docs/STATUS.md`. No batched commits across multiple tasks.
- **Opus only with the user's explicit approval** -- ask beforehand.
- **Every endpoint and every function gets a test.** A test that only
  confirms what the code does anyway does not count. Where a line would
  only be reachable through an artificial construction, `# pragma: no
  cover` with a reason is the more honest answer.
- **Nothing hard-coded except the security principles above.** No apartment
  ids, addresses, or credentials in the source code.
- **No secrets in the repo**, not even as a real-looking example value (see
  the reasoning in `protocol/registration.py`).

## Technical framework

| | |
|---|---|
| Backend | Python, FastAPI (`fleet/`), plain Python client (`agent/`) |
| Shared contract | Pydantic models in `protocol/`, imported by both sides |
| Connection | HTTPS upstream (`POST`), SSE downstream (`GET /v1/commands`) -- no MQTT over the internet, no custom framing protocol (section 3) |
| Operation (cloud/device) | Two separate Docker images from one repository (`docker/Dockerfile.fleet`, `docker/Dockerfile.agent`) |
| Watchdog | `watchdog/`, Go with no dependencies, statically built, systemd unit, no Docker image |
| Base station | `image/`, a prepared Debian 13 image (Raspberry Pi OS or amd64), no custom operating system, no Docker image |

One repository for both Docker images plus the watchdog plus the system
image recipe, because all four are tightly coupled via the same contract --
reasoning in `README.md`.

**The language rule (section 18.4): Go on the bare metal, Python in the
container.** The watchdog is the exception, not the start of a migration --
it runs on the bare system and must start even when the operating system's
interpreter is broken. The agent brings its own runtime in its own image
and is not affected by this class of failure at all; writing it in Go would
only have cost the shared protocol package with the cloud service
(`protocol/`) -- maintained twice instead of once. Should a part of the
agent ever need to run on the bare system in the future (conceivable for
the WireGuard tunnel from section 14, which sets up a network interface),
**that piece moves to the watchdog binary**, instead of rewriting the agent
or introducing a third language.
