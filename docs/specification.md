<!--
English translation, authoritative for this repository since 2026-09-24.
The German original stays available as the source document, unchanged, at
`thermoctl/lokal/fleet-spezifikation.md` (outside this repository). Where
the two disagree because of a translation error, that is a bug in this
copy, not a reason to prefer the German prose over the meaning it was
meant to carry -- report it and fix the translation.
-->

# Fleet: an overview across all apartments

Specification, as of September 22, 2026. Reference: thermoctl 0.9.5, one device per
apartment (Raspberry Pi 4 or 5 with SSD as the default), no shared MQTT broker and no MQTT
on the open internet. Home Assistant only as an extension at the tenant's request (section 16).

---

## 1. What this thing is, and what it explicitly is not

**Is:** a small, separate cloud service run by the landlord, which receives a heartbeat with
health data from every apartment, collects faults, alarms on **absence**, and can send a
handful of narrowly scoped maintenance commands to an apartment.

**Is not:**

- **Not a second controller.** Setpoints, schedules, frost protection, and arming stay in the
  apartment. The cloud cannot change them -- not "not planned", but "the command does not
  exist".
- **Not a data collector.** Room temperature histories of every household in one place is an
  occupancy profile. What operation needs is health data, not behavioral data (section 6).
- **Not a replacement for the apartment view.** The tenant never sees the cloud.

Reasoning for this scope: a compromised fleet server that can only watch is a privacy
incident. One that can switch things is twelve cold or overheated apartments. This order
cannot be reversed later, so it is fixed now.

---

## 2. Layout

```
Apartment                                   Cloud (landlord)
┌──────────────────────────────┐            ┌────────────────────────────┐
│ thermoctl  ──REST(local)───┐ │            │  Fleet service             │
│ Zigbee2MQTT                │ │            │  ├ heartbeat receipt       │
│ Mosquitto (local only)     │ │            │  ├ event receipt           │
│                            ▼ │            │  ├ command delivery        │
│                   thermoctl agent│──HTTPS─▶ │  └ web UI + alarms        │
└──────────────────────────────┘  (outbound └────────────────────────────┘
                                    only)
```

**The agent** is a separate, very small program on the base station. It is the only thing
that talks to the cloud. thermoctl itself stays unchanged: the agent reads via its
**existing REST interface** with its own token that carries only read rights
(`zone.read`, `device.read`, `audit.read`).

Why a separate program and not a fleet module in thermoctl:

- thermoctl is deliberately a single-apartment product with thin adapters over shared
  domain logic. Pulling multi-tenant management into it breaks exactly that.
- The agent is the **security boundary**: it decides locally which commands it executes at
  all. This check therefore lives in the apartment, not in the cloud -- it still holds even
  if the cloud has been taken over.
- thermoctl stays fully usable without the cloud. Whoever does not install the agent has a
  working system with no fleet.

---

## 3. Connection: SSE downstream, HTTPS upstream

No MQTT on the internet, no shared broker, no custom-invented framing protocol.

| Direction | Path | Why |
|---|---|---|
| Apartment → cloud | ordinary `POST` calls over HTTPS | Heartbeat, events, command results. Nothing special, reproducible with `curl`. |
| Cloud → apartment | **Server-Sent Events**: the agent holds a `GET /v1/commands` open, the cloud writes commands into it as events | Outbound connection only, no port forwarding in the tenant's router. Reconnection, event numbering, and catch-up delivery are already fixed in the format (`Last-Event-ID`, `retry`) -- none of it needs to be invented. |
| Fallback | If the connection cannot be held open (proxy, mobile network), the agent polls every 60 s via `GET /v1/commands?wait=0` | Works everywhere, costs only latency. |

**Why not WebSockets:** they can do the same, but demand their own decisions for
authentication, reconnection, buffering, and ordering. SSE brings that along and is easier
to debug -- an open data stream you can follow with `curl -N`. It becomes bidirectional
through the `POST` direction, not through the protocol.

**Timing:** heartbeat every **120 s**. Commands reach an apartment in under a second with an
open connection, within 60 s in fallback mode.

---

## 4. Registration and rights

- **A separate secret per apartment.** The agent carries a token
  (`agent_<apartment>_<random>`, at least 32 bytes of entropy), the cloud stores only its
  hash. No shared secret, no derivable values.
- **Initial registration** via a one-time, time-limited setup code generated when the
  apartment is created in the cloud. The agent exchanges it for its token; the code expires
  after first use or after 24 hours.
- **Rotation:** the cloud can issue a new token; the agent adopts it and confirms. The old
  token is invalid immediately afterward.
- **Revocation per apartment**, without affecting the others -- the case "device stolen" or
  "apartment out of service".
- **TLS with certificate checking**, never disabled, not even for tests. The agent knows the
  cloud's expected fingerprint (pinning) as a second barrier.
- The agent's **thermoctl token** carries only read rights. For the commands in section 7 it
  needs no write rights in thermoctl, but rights on the base station's operating system.

---

## 5. The heartbeat

`POST /v1/heartbeat`, every 120 s, content (example):

```json
{
  "apartment": "house7-a03",
  "sent_at": "2026-09-22T14:03:11Z",
  "agent": "0.1.0",
  "thermoctl": { "version": "0.9.5", "reachable": true, "mode": "armed" },
  "control": {
    "last_decision": "2026-09-22T14:02:47Z",
    "zones": 6,
    "zones_with_heat_demand": 2,
    "zones_without_reading": 0
  },
  "devices": {
    "zigbee_bridge": "connected",
    "weakest_battery_percent": 62,
    "worst_signal_quality": 47,
    "silent_devices": 0
  },
  "system": {
    "uptime_s": 962114,
    "memory_free_percent": 41,
    "disk_free_percent": 68,
    "clock_drift_s": 0.4
  },
  "open_faults": [
    { "kind": "sensor_fault", "since": "2026-09-21T06:12:00Z", "zone": "bathroom" }
  ]
}
```

The fault kinds are the six thermoctl already knows today: `sensor_fault`,
`bridge_fault`, `command_failure`, `stuck_sensor`, `window_alarm`, `tenant_report`.

**Catching up:** if the apartment was offline, the agent sends the buffered heartbeats (at
most the last 240, i.e. eight hours) on next contact, in one batch. The cloud detects gaps by
the timestamp and displays them as such, instead of smoothing them over.

---

## 6. What is explicitly not transmitted

| Not transmitted | Why |
|---|---|
| Room temperatures, individually or over time | Presence can be inferred from this. "Zone without a reading: 0" is enough for operation. |
| Setpoints and schedules | Belong to the tenant. Operation does not need them. |
| Absence periods | Directly the question "is someone home". |
| Tenant names or contact details | The apartment is tracked by an id, not by people. |
| The text of tenant problem reports | What is transmitted is **that** one exists, with time and room -- it is read in the apartment's own instance. |

This is not caution for its own sake: exactly this combination would be the difference
between "distributed operational data" and "behavioral profile of twelve households in one
place", with everything in legal consequences that hangs off that.

**"Heat demand yes/no per zone" is the same trap in miniature -- see section 21.5's own
"Decided afterward" paragraph** for why a single such reading is harmless but a series of
them in the cloud's running storage is not, and where the line accordingly runs between
`fetch_logs` and the on-demand `diagnostic_bundle`.

---

## 7. Commands: a short, closed list

The cloud can only do what the agent knows. It rejects everything else and reports the
attempt.

**Stage 1 -- from the start:**

| Command | Effect | Risk |
|---|---|---|
| `report_now` | Send a heartbeat immediately instead of waiting for the interval | none |
| `fetch_logs` | The last *n* lines of the service log, masked, to the cloud | content, hence masked and capped at 500 lines |
| `backup_now` | Trigger a database backup and report the result | none |
| `agent_restart` | Restart only the agent | none |

**Stage 2 -- only after operational experience:**

| Command | Effect | Condition |
|---|---|---|
| `service_restart` | Restart the thermoctl container | Only if control has not made a decision for over ten minutes. Otherwise rejected. |
| `apply_update` | Update to a version named by the cloud | Only with a prior backup, only one apartment at a time, fallback to the old version if the heartbeat fails to arrive after 15 minutes |
| `revoke_kiosk_token` | Invalidate wall-panel access | Tenant change, panel lost |

**Never, at any stage:** change a setpoint, change a schedule, change frost protection, arm,
set an override, create a user, change rights. These commands do not exist in the agent.
Whoever needs them goes through the apartment's own instance.

**Execution, rules:**

- Every command carries an **id** and is executed at most once (the agent remembers the
  last 200 ids).
- Every command has an **expiry** (default 15 minutes). If an apartment comes back after
  three days, an old command is **not** executed any more -- otherwise a device restarts
  because someone pressed a button the week before last.
- Every command and every rejection lands in the apartment's **local** log, not only in the
  cloud. Whoever wants to reconstruct what happened to a system does not have to trust the
  cloud for that.
- **Result** via `POST /v1/commands/{id}/result`, with duration and error text.

---

## 8. Alarm rules

The cloud's value lies in **absence**, not in receiving.

| Alarm | Trigger | Urgency |
|---|---|---|
| Apartment not reporting | three heartbeats missing (6 min) | high, during the heating season |
| thermoctl not responding | agent cannot reach it locally | high |
| Control stalled | last decision older than three control cycles | high |
| Fault open | one of the six kinds, longer than 2 h | medium |
| Battery low | weakest cell under 20% | low, collected until the battery round |
| Signal quality dropping | worst value under 30, over several days | low |
| Version gap | apartment running more than two versions behind | low |
| Disk full | under 10% free | medium |
| Clock drift | more than 60 s | medium (schedules run wrong) |

**Against alarm fatigue:** every alarm has an all-clear and a snooze ("until tomorrow
morning"). Repeated alarms of the same kind for the same apartment are bundled. An alarm
without an all-clear is a design flaw, not a feature.

---

## 9. UI

Three views, no more:

1. **The house.** One tile per apartment: name, last contact, mode, open faults, version.
   Sorted by trouble, not by number. Whoever has nothing to do sees a quiet surface -- that
   is the point.
2. **One apartment.** Heartbeat history of the last few days (reachability, not
   temperature), open and past faults, battery and signal values per device, version, the
   four to seven allowed commands as buttons with confirmation.
3. **Tasks.** What is due: battery rounds, updates, unconfirmed faults. This is the list
   people actually work from.

No chart of room temperatures. No tenant relation. No configuration dialog that writes into
the apartment -- the temptation to turn this into remote control is the main reason for
section 7.

---

## 10. What needs to change in thermoctl for this

Little, and nothing in control itself:

1. **Extend `/healthz`** or add a second endpoint `/api/v1/health`: today it only returns
   `{"status": "ok", "version": ...}`. Additionally needed: mode, time of the last control
   decision, state of the Zigbee bridge, and the number of zones without a reading. All
   values that already exist internally.
2. **A `health.read` right** for a token that may read exactly that and nothing else --
   so the agent does not run with `zone.read` across every zone.
3. Nothing further. Battery, signal quality, and the switching log are read by the agent via
   the existing endpoints.

The rest is the agent and the cloud service -- both separate repositories.

---

## 11. Order

| Step | Result | Effort |
|---|---|---|
| 1 | Webhook receiver in the cloud: the six fault reports come in, with the apartment in the text | hours, **no change to thermoctl** |
| 2 | Agent with heartbeat, cloud alarms on absence | the actual gap is closed |
| 3 | UI "The house" and "One apartment" | |
| 4 | SSE channel and stage-1 commands | |
| 5 | After a heating season of experience: consider stage 2 | deliberately late |

Step 1 pays off independently of whether the rest is ever built: it shows within a week
whether the existing fault reports are useful day to day -- before anyone writes a line of
protocol code.

---

## 12. Open points

- **Where does the cloud run?** Server in the EU, otherwise the privacy question from
  section 6 reopens. If a provider operates it, a data processing agreement is needed.
- **What happens on a tenant change?** The apartment's id stays, the token is rotated,
  history data from the previous tenancy is deleted or anonymized -- to be decided.
- **Retention in the cloud:** proposal 90 days for heartbeats, 365 for faults. To be
  decided, not left implicit.
- **The agent needs an update procedure** -- otherwise the problem just moves one level
  down. Proposal: the same path as thermoctl, with a fallback to the old version.
- **Two-person rule for stage 2?** Triggering an update that affects twelve apartments with
  one click is tempting and dangerous. Proposal: stage-2 commands always for one apartment
  only, never "for all".

**Decided afterward (project owner, 2026-10-01):**

- **Retention:** heartbeats 90 days; faults, alarms and events 365 days. Command log
  excerpts and diagnostic bundles keep their own shorter retention; backups keep section
  15.2's rhythm; the audit log is not deleted. Deletion runs as a background job, periods
  configurable.
- **Tenant change:** an explicit UI action (confirmation, mandatory reason, audited)
  rotates the apartment's device token -- the agent obtains the new one through a signed
  challenge with its existing device key, no private key ever leaves the device -- and
  deletes the apartment's heartbeats, events, faults, alarms and command log excerpts.
  Encrypted backups stay, under their own retention.
- **UI login:** passkeys (WebAuthn) are added as a second factor next to TOTP; TOTP secrets
  are stored encrypted with a key from the environment, never in the database.
- **Faults can be acknowledged** in the UI; an acknowledgement applies to the current
  occurrence only -- if the same fault recurs, it shows again.
- **Retention reading (2026-10-02):** "faults 365 days" refers to the stored event
  entries. Open faults are part of each heartbeat; their heartbeat-by-heartbeat history
  older than 90 days goes with the heartbeats, while the current state (open since ...)
  always stays visible through the latest heartbeat.
- **Battery and signal per device** may be transmitted: a list of device id, battery
  percent and signal quality only -- no device names (they may contain room names), no
  measured values. Section 6 stays intact.

**Decided afterward (project owner, 2026-10-02):**

- **Retention, refined:** the 365-day limit for faults, alarms and events applies only to
  *cleared/closed* ones -- anything still open is kept with its real start time regardless
  of age (an alarm open for more than a year is a sign something needs attention, not a
  row to silently discard).
- **Tenant change, refined:** also deletes the apartment's diagnostic bundles (both the
  database row and the stored blob file), scoped to that apartment only. Encrypted
  backups still stay, under their own retention, unchanged by this addition.

---

## 13. Docker in the apartment: desired state instead of remote control

Four containers run on the base station: `thermoctl`, `zigbee2mqtt`, `mosquitto`, and the
`agent` itself. The fleet service **says which state is desired** -- applying it is done by
the agent, with local safeguards. It never executes what it is told, it reconciles toward a
desired state.

**The desired state** is a small file the cloud keeps per apartment:

```json
{
  "revision": 42,
  "services": {
    "thermoctl":   { "image": "ghcr.io/magicalwig34653/thermoctl", "version": "0.9.5",
                     "digest": "sha256:9f2c…" },
    "zigbee2mqtt": { "image": "koenkk/zigbee2mqtt", "version": "2.6.1", "digest": "sha256:1a7b…" },
    "mosquitto":   { "image": "eclipse-mosquitto", "version": "2.0.22", "digest": "sha256:44de…" },
    "agent":       { "image": "ghcr.io/…/thermoctl-agent", "version": "0.1.3", "digest": "sha256:c03a…" }
  },
  "window": { "from": "09:00", "until": "16:00", "not_below_outdoor_temp_c": -2 }
}
```

**What the agent does with it, and does not:**

| allowed | not allowed |
|---|---|
| Fetch, check, start, stop images of the **four known services** | Start arbitrary images. The agent knows exactly these four names; everything else is rejected and reported. |
| Only images from the **hard-coded sources** (a prefix list in the agent, not in the cloud) | Take over a registry or prefix from the cloud |
| Only images whose **digest** matches the desired state | "latest" or a tag without a digest |
| Restart containers, read logs, check disk space | Shell commands, `docker exec`, arbitrary compose files |
| Backup before every change | Change without a backup |

This is the decisive lock: **a compromised fleet server cannot start foreign code in twelve
apartments.** It can only choose between versions that come from the hard-coded sources --
and even that only within the rules below.

### Sequence of an update

1. **Pre-check** (local, without the cloud): is there free space (> 20%)? Has the time
   window arrived? Is the outdoor temperature above the limit? Is the system currently
   controlling normally? If a check fails, the agent rejects and reports the reason -- the
   cloud cannot override this.
2. **Backup** of the database and the configuration, result is reported.
3. **Fetch the image**, check the digest. If it does not match: abort, the old state stays.
4. **Swap**, start the service, wait for health (thermoctl: `/api/v1/health` responds,
   control makes a decision within two cycles).
5. **Confirmation**: if the heartbeat fails to arrive for 15 minutes, or the service does not
   report healthy, **the agent falls back to the previous digest on its own** and reports it.
   No one has to intervene at night.
6. **Agent update last and separately** -- something that swaps itself needs a two-step path
   (start the new version, remove the old one only after a successful heartbeat).

### Rules for the rollout

- **One apartment at a time.** The fleet service knows no "for all". A queue is worked
  through, with a stop at the first apartment that does not come back healthy.
- **The pilot apartment first**, then the rest no earlier than 48 hours later.
- **Time window and heating season:** no update in the evening, none below the set outdoor
  temperature threshold. A restart costs a few minutes of control -- nothing on a mild
  morning, a phone call on a cold evening.
- **Zigbee2MQTT is the bigger risk than thermoctl**, because a version jump there can change
  device pairings. Its own release, never together with a thermoctl update.

**Decided afterward (project owner, 2026-09-28):** desired-state reconciliation (P5.4) is
built now, but stays **inactive** until two conditions hold, so that it cannot arm itself
before the heating season of operational experience section 7 asks for `apply_update`:

1. The pre-check is **fail-closed**: if thermoctl's health or the outdoor temperature cannot
   be read, the update is rejected -- "unknown" never counts as "fine". Treating an unknown
   outdoor temperature as permission would remove exactly the protection against updating
   during a cold spell.
2. Additionally, the target apartment must carry `pilot_mode` (the existing inventory flag,
   section 21.4) -- otherwise the agent rejects. This keeps P5.4 from going live
   automatically the day thermoctl ships its health API.

No new `CommandType`; `apply_update` stays stage 2. Both rejection paths (health missing,
`pilot_mode` missing) are tested, since they are the only code path that is actually live.

**Decided afterward (project owner, 2026-10-02), rollout queue:** a rollout is created once
(one service, one version, one list of apartments) and then works through the list on its
own, one apartment at a time, stopping at the first problem. An apartment counts as "back
healthy" when the agent reports the revision as successfully reconciled and a heartbeat with
thermoctl reachable arrives afterwards (both fleet-side timestamps). The test apartment is
the one marked as such in the list, or else simply the first apartment of the list; the rest
follow no earlier than 48 hours after it came back healthy. This rollout test apartment is
independent of the device-side `pilot_mode` gate above. A new apartment is never set up
through a rollout -- it gets its desired state directly on its own page; a rollout only
updates apartments that already have one.

### Operating system and firmware

Not via the fleet service. Operating-system security updates run unattended on the base
station (`unattended-upgrades` or the chosen system's equivalent), restarts only within the
time window. Device Zigbee firmware stays manual work via Zigbee2MQTT -- radio devices that
drop out during an update cannot be brought back remotely.

---

## 14. WireGuard: keys stay on the device, the cloud only distributes the assignment

Yes, the fleet service is the right place for this -- but with a clear separation.

**The private key is generated on the base station and never leaves it.** The agent only
reports its **public** key to the cloud. The cloud distributes what it is allowed to
distribute: the server's address, its public key, the assigned IP address, and the allowed
networks. A cloud that issues private keys can read along in every apartment -- and a
memory dump of it would be the master key to the whole building.

**Separation of apartments:** every apartment gets its own address, and the allowed networks
cover **only** the server -- never other apartments. Forwarding between peers is disabled on
the server. Otherwise the tunnel becomes a detour that recreates exactly the building-wide
network we avoided by using separate brokers.

**On demand, not permanent.** My proposal: the tunnel does not stand permanently, but is
opened for a limited time over the command channel (`open_tunnel`, default 60 minutes, after
which the agent closes it on its own). This gives you real access to an apartment's web UI
when you need it -- without a permanent path into every tenant's apartment staying open.
This is also what is easier to write into a usage agreement: "for troubleshooting, time-
limited, logged" instead of "at any time".

Every opening and every closing is recorded in the apartment's **local** log.

### The question this raises: why still have Home Assistant centrally?

Something does not fit together here, and it should be clarified before building:

- For the rooms of an apartment to appear in a **central** Home Assistant, setpoint and
  actual temperatures of every zone would have to flow there. That is exactly what section 6
  deliberately does **not** transmit.
- The fleet service fully covers the operational purpose -- faults, reachability, versions,
  batteries -- without this data.
- What would be left for central Home Assistant: nice charts, automations, linking with
  other home technology. That is not nothing, but does not justify permanently transmitting
  room temperatures of every household.

Three paths, and I would take the first:

1. **No central Home Assistant.** The fleet service is your overview; Home Assistant runs
   where it belongs -- in the apartment, if the tenant wants it. This is the cleanest scope
   and saves the entire tunnel for regular operation.
2. **Central Home Assistant only for your own apartment** or for shared building technology
   (boiler room, outdoor sensor, stairwell lighting). Then no tenant data flows.
3. **Central Home Assistant with all apartments** -- then deliberately decide that room
   temperatures are transmitted, write that into the privacy notice, cap retention, and
   document the purpose limitation in writing. Technically via the tunnel, not over open
   MQTT.

---

## 15. Swapping a device in minutes: backup, id, initial commissioning

Goal: device fails → replacement device off the shelf → plug it in → a few minutes later the
apartment is under control again. The same path serves the initial installation. This holds
up -- with three decisions, without which it falls apart.

### 15.1 Two kinds of backup that must not be mixed

| | **Device configuration** | **Operational data** |
|---|---|---|
| Content | Apartment id, service versions with digests, broker settings, WireGuard peer, timezone, agent settings | thermoctl database: rooms, setpoints, schedules, users, history. Plus Zigbee2MQTT: device table and coordinator backup |
| Contains tenant data | no | **yes** |
| Lives in the cloud | in plain text | **encrypted only, with a key the cloud does not have** |
| Size | kilobytes | a few megabytes |

Operational data is **encrypted on the device** before being uploaded. The cloud stores an
opaque block and cannot read it. This keeps section 6's claim true -- no readable tenant
data lives in the cloud -- and a restore still takes minutes instead of an evening.

**The key** belongs to the landlord, not to the device and not to the cloud: generated once,
stored in a password manager, additionally printed in a folder. **If it is lost, every backup
is worthless.** That is the price of this design and belongs in the operating manual, not in
a footnote.

### 15.2 What must absolutely be in the operational-data backup

- `thermoctl` database including configuration
- **Zigbee2MQTT device table and `coordinator_backup.json`**

The second point decides between minutes and an afternoon: if the radio stick moves along to
the replacement device, the Zigbee network keeps running -- the network key sits in the
stick. If the stick itself is defective, the coordinator backup restores the network key and
address, and the devices find their way back on their own. **Without this backup, every
sensor and relay must be re-paired -- in every room, requiring access to the apartment.**

Rhythm: device configuration on every change, operational data daily and additionally before
every update. Retention: the last 14 daily backups, plus one weekly backup for each of the
last eight weeks.

### 15.3 Initial commissioning and swap: the same flow

The id alone must **not** be enough to get at the configuration -- otherwise anyone who
guesses an id gets an apartment's credentials. Hence:

1. **Write the image.** A small tool (or the Raspberry Pi Imager with a prepared file)
   writes the finished system onto a card or drive and places a file
   `agent-registration.json` on the boot partition: the fleet service's address, its
   certificate fingerprint, and a **one-time registration code** issued by the cloud
   beforehand. None of it is a permanent secret.
2. **Plug it in.** On first boot the agent generates its own key pair, registers with the
   cloud using the registration code and its **public** key, and displays a short
   verification code (on the panel, in a setup UI, or in the log).
3. **Assign.** In the fleet UI you select the apartment, see the same verification code, and
   confirm. Only this confirmation releases the configuration -- bound to the key of exactly
   this device.
4. **Set up.** The agent fetches the device configuration and, on a swap, the encrypted
   operational data. The landlord enters the decryption key once in the fleet UI; it is only
   passed through, never stored.
5. **Done.** The agent starts the four containers in the secured state, reports a heartbeat,
   and the apartment is under control again.

Realistic duration for a swap: **10 to 20 minutes**, most of it waiting for the images to
download. What a person still has to do: drive there, swap the device, move the radio stick
over.

**Decided afterward (project owner, 2026-09-28), how "passed through" works:** the
landlord's decryption key never reaches the fleet service in plain text. The device
generates, next to its Ed25519 key, its own age key pair on the device and registers only
the **public** recipient. In the fleet UI the landlord's browser encrypts the entered key
locally to exactly this device's recipient; the fleet service stores and forwards only that
opaque block, bound to the assigned device, and deletes it once fetched or expired. A
compromised fleet server therefore sees neither the key nor the operational data.

**Decided afterward (project owner, 2026-09-28), limit of the browser-side encryption:** the
script that encrypts the key in the browser is served by the fleet service itself. This
protects against a leaked database, logs, server backups and passive reading along -- it
does **not** protect against an *actively* taken-over fleet server that ships a modified
script and captures the key at the next restore. Accepted deliberately, since restores are
rare and triggered consciously by the landlord; the UI shows the script's sha256 and the
operating manual names the expected value, so the landlord can check it.

**Decided afterward (project owner, 2026-09-28), where the restore is written:** the agent
container never gets write access to the live tenant data. The thermoctl database and the
Zigbee2MQTT directory stay mounted read-only into the agent; the agent writes the decrypted
operational data only into its own staging directory. Moving it into the real data
directories is done by a small, separate Go program on the bare system next to the watchdog
(same module, no dependency, no network, section 18.4's language rule), and only if no
operational data exists there yet.

### 15.4 If the device only has Wi-Fi

A replacement device with a network cable is the simple case -- the registration code sits
in the image, nothing else is needed. Without a cable, the Wi-Fi password has to get in
somehow. Three paths:

| Path | Verdict |
|---|---|
| Provide Wi-Fi credentials when writing the image | **Recommended.** No radio window, no UI, nothing that can stay open. Assumes you know the apartment's Wi-Fi -- always the case with your own connection or mobile data. |
| Device's own setup access point, **with** a password | Acceptable, if the tool generates and prints a random password, the access point disappears after setup, and closes on its own after 30 minutes at the latest. |
| **Open** Wi-Fi for setup | **No.** Anyone within range reads the tenant's Wi-Fi password along and takes over the device before you have assigned it. The most convenient path here is the one that opens the apartment. |

### 15.5 What the fleet service must not do

- Deliver an apartment's configuration to a device that has not been explicitly assigned to
  it. A mix-up would otherwise mean: apartment 3 controls with apartment 7's rooms.
- Store the decryption key. It is entered, passed through, forgotten.
- Silently change an assignment. If a device is assigned to a different apartment, its token
  expires and the old assignment is logged.

---

## 16. Home Assistant as an extension for the tenant

Proposed: whoever wants it gets their own Home Assistant instance in the cloud with its own
MQTT broker; the apartment builds a tunnel there over WireGuard. This is clean -- MQTT is
never on the open internet, and every tenancy has its own broker instead of a shared one.
Four points about this:

1. **It is a service, not a freebie.** Every instance is a service that wants updating,
   backing up, and restoring. With three interested tenants that is three additional systems
   in your responsibility. This deserves its own short agreement text: purpose, data,
   terminability -- and the clear statement that the heating works without it too.
2. **The tenant is the administrator there, not you.** It is their comfort system. You
   operate the shell, they set up whatever they want. Otherwise you end up in the role of
   fixing their lighting automation.
3. **Only their zones.** The apartment's instance publishes exclusively that apartment's
   rooms into this broker. Separate brokers, separate tunnels, separate credentials --
   forwarding between peers stays disabled (section 14).
4. **In privacy terms, this is exactly the case section 6 avoids** -- here, room temperatures
   and setpoints flow into your cloud. The difference: it happens at the tenant's request,
   for their purpose, and they can cancel it. That makes it viable, but only with consent,
   purpose limitation, and a deletion rule on move-out.

**The boundary I would hold to:** the fleet service and the tenant instances have nothing to
do with each other. The fleet service sees health data of all apartments and no
temperatures; a tenant instance sees one apartment's temperatures and no operational data.
That both run on the same hardware is an operational question -- that they use the same
database would be a design error.

---

## 17. How the agent updates itself -- and what rolls back

The agent is the one program that cannot replace itself while it is running. The proposal to
run **two equivalent agents** that check and swap each other solves this problem -- but
introduces three new ones:

- **Who is right?** If both consider each other defective, they swap each other back and
  forth. That needs a leader election, i.e. exactly the kind of distributed logic you do not
  want to debug on a device in someone else's apartment.
- **Double consumption.** Two agents means twice the memory, two heartbeats, two tokens, two
  logs.
- **Both are moving targets.** What is supposed to roll back changes just as often as what is
  rolled back. A bug in the update path then sits in both versions.

### Instead: a small, dumb watchdog

**Principle: what rolls back must not be the same thing as what changes.**

Two things with very different lifecycles run on the device:

| | **Watchdog** | **Agent** |
|---|---|---|
| Scope | a few hundred lines, one systemd service | the actual application |
| Changes | almost never (versions a year apart) | regularly |
| Can | start containers, stop them, reset a digest, measure time | everything in this specification |
| Knows the cloud | no | yes |
| Is updated | by hand, with the operating system | by the watchdog |

The watchdog does **not** talk to the cloud. It only knows two digests -- the running one and
the previous one -- and one question: *has the agent said "I'm healthy" within the deadline?*

### Who loads, and who swaps

The dividing line is not "agent versus watchdog", but **may fail** versus **must not fail**:

| | May fail | Must not fail |
|---|---|---|
| What | Fetch the image, check the digest, check preconditions, create a backup | Start one of the two **already present** revisions and roll back if the health report fails to arrive |
| Who | **Agent** | **Watchdog** |
| Needs network | yes | **no** |
| Needs registry access | yes | no |
| Changes | often | almost never |

That is why the **agent** downloads the new image itself -- it has network and container
access anyway, because it also maintains the other three services. The **watchdog** never
sees a network, knows no registry, and checks no signatures. It knows two locally present
digests and one question.

The gain: every capability the watchdog does not have is code that cannot break, and a path a
compromised fleet service cannot use. A watchdog with registry access would be a second path
by which foreign code reaches the device.

### Sequence of an agent update

1. **The agent** receives the new desired state, checks the preconditions (section 13),
   **fetches the new image and checks the digest** against the hard-coded sources. If
   anything fails here, everything simply stays as it was -- reported, but with no
   consequences.
2. It writes both digests into a state file that the watchdog also reads --
   **line-based, with Unix timestamps**, in the style of a systemd environment file:

   ```
   desired=sha256:9f2c…
   proven=sha256:1a7b…
   since=1790000123
   ```

   No JSON, deliberately: this way the contract is readable in every language with built-in
   tools -- in Go, in Rust without third-party packages, in Python, in three lines of shell
   if need be. The watchdog's choice of language thus stays revisable later, without
   breaking the contract. **Both images are now present locally.**
3. It stops itself. It does nothing more.
4. **The watchdog** starts the revision named in `desired`. No network needed.
5. The new agent must pass its self-test within **10 minutes** and regularly write a health
   report into a local file. If it fails to arrive, or the container restarts three times in
   a row, the watchdog falls back to `proven` and notes the reason.
6. After one hour of fault-free operation, the agent itself advances the new digest to
   `proven`. Only then may the old image be removed.

### Who watches whom

- **The watchdog only watches the agent.** That is its only job.
- **The agent watches the other three services** (thermoctl, Zigbee2MQTT, Mosquitto)
  following the same pattern: remember the previous digest, check health, roll back on
  failure. This needs no second watchdog -- if the agent is alive, it can do this; if it is
  not alive, it is the agent's turn first, and the watchdog brings it back.
- **If both fail**, thermoctl keeps running anyway: it does not depend on the agent, it is
  only observed by it. The apartment keeps heating while no one is watching -- and it is
  exactly this silence that the cloud reports (section 8).

### Two networks, two safeguards

The cloud notices the missing heartbeat anyway (section 8) and can trigger a rollback on its
own. That is the second layer, not the first -- it only works if the apartment is reachable.
The first layer is the watchdog, and it works even when the internet has been gone for two
days.

### The watchdog itself

It ships with the operating system (part of the prepared image from section 15.3) and is
updated via the operating system's package management -- deliberately outside the fleet
service. If its version really has to change, that is a process like an operating system
update: announced, one apartment first, and if in doubt tied to a visit. With one version a
year apart, that is acceptable; with a second agent that moves along weekly, it would not be.

### Fallback without a proven revision

Added afterward, because the scaffold raised this gap while building `watchdog/watch.go`:
`RollBackToProven` assumes a `proven` digest is set -- what happens with a freshly set-up
device whose first version fails before any revision has even run fault-free for an hour?

**Decided: this case does not exist, because it is already closed off when the image is
built.** The digest of the shipped agent version (section 19.3) is written into the state
file at system-image build time -- `desired` and `proven` point at **the same** digest on
delivery, valid from the first boot. A device therefore has a fallback target from the first
second on, in the worst case the shipped version itself. Without this, a device whose first
update fails could only be rescued by an on-site visit -- exactly the kind of trip the
watchdog exists to avoid in the first place.

Affected: the image recipe (section 19.4, the build now also writes the state file), and
`watchdog/state.go` and `watchdog/watch.go` on the reading side -- both now assume that a
properly shipped device never starts with an empty `proven`; an empty `proven` is therefore
no longer a normal initial state, but a sign of a faulty delivery, and is reported as an
error, not silently accepted.

---

## 18. Three decisions that came up while building the scaffold

### 18.1 What thermoctl's webhook actually sends

Checked in `thermoctl/integrations/notification.py` -- the payload is leaner than hoped and
contains **neither apartment, nor kind, nor timestamp**:

```json
{ "schluessel": "zigbee2mqtt:brücke", "schwere": "warnung",
  "titel": "…", "text": "…" }
```

Plus an optional `Authorization: Bearer …` that the operator configures per system. This
follows for the fleet service:

- **A separate receiving address and a separate token per apartment**:
  `POST /v1/events/{apartment}` with that apartment's token. The assignment comes from the
  address and is checked via the token -- not guessed from the text.
- **The timestamp is the receipt time.** A report that arrives late after a network outage
  cannot be recognized as such; the actual time of the event lives in the heartbeat
  (`open_faults[].since`), not in the report.
- **The kind is embedded in the `key`**, not in a field of its own (`zigbee2mqtt:brücke`,
  `tenant-report:<zone>:<category>`). The fleet service maps it via a prefix and treats
  unknown ones as "other report" instead of failing.
- The webhook thus stays what it is: **the detection that something happened**. The reliable
  state comes from the heartbeat.

*(Note: `Event`'s four field names -- `schluessel`, `schwere`, `titel`, `text` -- are kept in
German in `protocol/events.py` on purpose, because they mirror thermoctl's real, unmodified
webhook payload byte for byte; see that module's docstring for why translating them would
silently break interoperability with a system this repository does not control.)*

### 18.2 Compatibility between versions

`PROTOCOL_VERSION` is a number that increases with every change to the models. Rules:

- The agent sends it with every heartbeat.
- **The fleet service accepts an older version**, as long as it understands its fields, and
  displays the apartment as "outdated version". It does not reject it -- an apartment that
  stops reporting because of a version difference is exactly the silence nobody wants.
- **The agent rejects commands of a newer version** it does not know (it knows its own
  command list as closed anyway), reports that as a result, and keeps running.
- A field may only ever be added, never change its meaning. Whoever means something
  different names it differently.

**Decided afterward (project owner, 2026-09-26):** "a number that increases with
every change to the models" is read literally -- any change to a model counts,
including a purely additive, backward-compatible one such as a wholly new model.
Version 2 is exactly the four registration models P4.2b added (`RegistrationAccepted`,
`TokenChallenge`, `TokenRequest`, `TokenIssued`); nothing else in this section changes,
and the compatibility rules above apply unchanged to the 1-to-2 step like to any other.

### 18.3 Where the watchdog lives, and what it is written in

**In the same repository, its own folder `watchdog/`, written in Go, no Docker image.**

*On location:*

- It runs **outside** the container runtime -- it starts and stops containers and must be
  present precisely when they are not running.
- It still belongs in the same repository, because it shares a contract with the agent: the
  state file and the health report. Separate repositories would mean maintaining exactly
  this contract twice.
- Shipped as part of the prepared image (section 19), with a systemd unit; updated via the
  operating system's package management, not via the fleet service.

*On the language -- Go, not Python:*

The watchdog is the one thing that has to work when everything else is broken. A Python
program assumes the interpreter is intact: no half-applied `apt` transaction, no shot
`python3` symlink after a version jump, no corrupted `.pyc` file on a dying card. These are
rare cases -- but exactly the ones it exists for. A statically linked binary does not know
this class of failure.

Go and not Rust, because the standard library decides it: time handling and file work are
included there, cross-compiling for `arm64` and `amd64` needs no extra tooling, and a Python
person reads Go at three in the morning without a run-up. Rust's strength -- safety when
processing untrusted data -- pays off little here: the watchdog reads a file written by its
own sibling process and calls `systemctl`. No network, no untrusted input.

*Conditions:*

- **`go.mod` without a single dependency.** In particular not the Docker SDK -- the
  container runtime is addressed via its command-line tool or via `systemctl`.
- **Statically built** (`CGO_ENABLED=0`), one binary each for `arm64` and `amd64`, with a
  checksum, produced in CI and placed into the image. Nothing is compiled on the device.
- **Under 300 lines of executable code.** Comments and blank lines do not count toward the
  limit -- only counted are the statements that actually make the decisions (assignments,
  calls, conditionals, and the like). **Redefined afterward:** the limit originally counted
  every line in the file. The point of the limit was always "small enough to read in full",
  and that is a statement about logic, not about explanations -- but the old, literal count
  had already once forced trimming comments to stay under 300, and the comments are exactly
  where the reasoning for a decision lives, the reasoning this specification asks to be kept
  rather than discarded (see the introduction to section 22 and the note at the top of
  `docs/STATUS.md`). Counting only statements removes that trade-off: the watchdog can carry
  as much explanation as a decision needs, and the limit still does the one job it was built
  for -- keeping the actual control flow small enough that one person can hold all of it in
  their head at once. If it grows beyond that, the scope is wrong.
- Its own CI track: `go vet`, `go test`, a build for both architectures.

*This is what makes the contract test better:* Python writes the state file, Go reads it.
Two languages that cannot share a model check the same contract harder than two Python
modules that might be wrong together.

### 18.4 The language rule

> **On the bare metal: Go. In the container: Python.**

The watchdog is the exception, not the start of a migration. It runs on the bare system and
must start even when the operating system's interpreter is broken. The agent runs in the
container, brings its own runtime in its own image, and is not affected by this class of
failure at all -- writing it in Go would gain nothing and cost the shared protocol package
with the cloud service: the model would have to exist twice, once as Go structs, once as
Pydantic models, maintained twice or generated from a schema. Exactly the double maintenance
that is the reason the agent and the cloud live in one repository.

The watchdog does not have this problem, because its contract is three lines long.

If a part of the agent later really does have to run on the bare system -- conceivable for
the tunnel, which sets up a network interface -- **that piece** moves to the watchdog binary,
instead of rewriting the agent. A third language in the repository wants justifying; the
second one is, because it rules out a named class of failure.

---

## 19. The prepared images

**No custom operating system.** A "thermoctlOS" modeled on Home Assistant OS would mean a
custom kernel, a custom bootloader, and responsibility for every gap in the substrate. What
is built instead is a **recipe** that turns a stock Linux distribution into a ready-to-use
device. The difference is not just wording: with a custom system, the security holes belong
to you; with a prepared image, they belong to Debian.

### 19.1 Two images, one recipe

| | **Image "pi"** | **Image "x86"** |
|---|---|---|
| Base | Raspberry Pi OS Lite **64-bit** (Debian 13 "Trixie", kernel 6.12 LTS) | Debian 13 "Trixie" **amd64**, minimal |
| For | Raspberry Pi 4 and 5 | Mini PC with N100, thin client, everything else |
| Differences | Raspberry Pi kernel and firmware, boot partition as FAT32 under `/boot/firmware` | Debian kernel, EFI boot |
| Shared | **everything else**: systemd, package names, container runtime, watchdog, units, update rules |

This is the reason for exactly these two and not Alpine: **Raspberry Pi OS is Debian.** One
recipe, two targets, one maintenance path -- the same package names, the same systemd units,
the same watchdog with no second version.

**Why not Alpine:** it uses OpenRC instead of systemd -- the watchdog would need a second
implementation, i.e. exactly the double maintenance we avoid everywhere else. Plus musl
instead of glibc, which occasionally causes friction with Python packages, and a support
duration of roughly two years per branch instead of five. The upside would be a footprint
about a hundred megabytes smaller -- at 2 GB of memory and an SSD, not a currency this pays
off in. As a special case (a strictly read-only root filesystem) it remains conceivable, but
not as a second regular path.

**And why no third image for ARM outside the Raspberry world:** the hardware research from
2026-09-22 (`lokal/recherche/basisstationen-alternativen.md`) turned up a serious candidate,
the **FriendlyELEC NanoPi R5S** -- 4 GB, eMMC, NVMe slot, metal enclosure, roughly 4 W, for
about €99. It is explicitly *not* ruled out, but it hinges on one condition: it will only
**not** need its own image if the Debian for it comes from **Armbian**, which already
maintains it for a large user base and ships a Trixie image with mainline U-Boot. As soon as
a board instead needs a vendor kernel -- and that holds for every RK3588 board and every
router board from the OpenWrt world -- a third maintenance chain is created, and that costs
more in the long run than the two saved watts are ever worth. **The rule is therefore:
mainline or not at all.** Before a device is added to the fleet, it must be bought, measured,
and observed for a heating season.

### 19.2 Support duration

- **Debian 13 "Trixie"**: full support until **August 9, 2028**, then LTS until
  **June 30, 2030**.
- Raspberry Pi OS has followed the same base since October 2025, with kernel **6.12 LTS**.

This carries the first device generation through its economic lifespan. Moving to the next
Debian release is **not** planned as a live update, but as a wave of new cards or drives via
the replacement-device path from section 15.3 -- one apartment after another, the pilot
apartment first.

### 19.3 What is in both images

- Container runtime, time sync, hardware watchdog enabled
- the **watchdog** as a systemd unit, the agent image already preloaded
- `unattended-upgrades` for security updates, restarts only within the time window
- logs in memory instead of on the card (`log2ram` or `tmpfs`) -- the biggest lever against
  card wear
- udev rule for the Zigbee stick, so it always appears under the same name and is not
  `ttyUSB0` once and `ttyUSB1` after a reboot
- WireGuard installed but not configured
- **ModemManager and the firmware packages for LTE add-ons**, disabled and preconfigured --
  so a device with mobile connectivity starts without further installation and one without
  it notices nothing
- an empty `agent-registration.json` on the boot partition
- an already-written state file (section 17) for the watchdog, with `desired` and `proven`
  set to the digest of the shipped agent version -- see "Fallback without a proven revision"
  in section 17
- **no** SSH password access; keys are deposited during preparation or not at all

**64-bit only**, in both cases -- if only because the thermoctl image itself is only built
for `amd64` and `arm64`.

### 19.4 Build and delivery

- Folder `image/` in the same repository as the agent and the watchdog. Same reason as
  there: the image ships a specific watchdog version, and that version shares a contract
  with the agent.
- Built in CI (pi-gen or `mkosi`/`debos`), the result is two files `.img.xz` with checksums,
  published at the release.
- **Rebuilt quarterly**, so a freshly flashed device does not have to catch up on two years
  of updates. Running devices fetch these themselves anyway.
- The image version carries the same number as the watchdog version shipped inside it.

### 19.5 The preparation tool

It needs to do almost nothing. The boot partition is FAT32 and writable on any computer; a
small program or a local page that, after the image is written, writes the apartment id,
registration code, the fleet service's address, and its fingerprint into
`agent-registration.json` is enough -- and, for Wi-Fi devices, the credentials right along
with it (section 15.4). No custom imager, no reinvention: the image itself is written by the
Raspberry Pi Imager or `dd`.

### 19.6 What is given up by doing this

The OS A/B update that Home Assistant OS has. A failed `apt` update is therefore
theoretically an on-site visit. Against that: security updates in Debian are narrowly scoped
and rarely break, the application has its own A/B safeguard via the digests (section 17),
and for the rest a prepared replacement device sits on the shelf -- the case a minutes-long
restore was built for.

---

## 20. Managing apartments and devices

The fleet service keeps the directory: which apartments exist, which devices are in
circulation, and which one currently sits where. Without this directory there is no
assignment, and without an assignment no configuration is released (section 15.5).

### 20.1 What is tracked

**Property** -- name, address, notes. The top level, so multiple buildings don't get mixed
up.

**Apartment** -- a permanent id (`house7-a03`, never changes), a label, location (floor,
orientation), state, number of heating circuits. **No tenant name, no contact details** --
the apartment is tracked by its id, not by people. Whoever needs that link has it in their
own tenant management.

The machine-readable values of the apartment state are **English** (decided afterward, see
section 22.4 -- only the values, not the model and field names):

| Value | Meaning |
|---|---|
| `occupied` | occupied |
| `vacant` | empty, currently without a tenant |
| `renovating` | under renovation |
| `retired` | retired (section 20.3: "an apartment is not deleted, it is retired") |

**Device** -- serial number or hardware id, model (Pi 4, Pi 5, N100 ...), acquisition date,
public-key fingerprint, image and watchdog version, state. Also English values, the same
reasoning:

| Value | Meaning |
|---|---|
| `registered` | added to the directory, not yet physically prepared |
| `prepared` | image written, registration code generated and valid |
| `reported` | has registered with a verification code, waiting for confirmation |
| `in_service` | assigned to an apartment, reporting a heartbeat |
| `in_storage` | prepared but not assigned -- the replacement device |
| `faulty` | failed, waiting for inspection |
| `decommissioned` | permanently out of circulation, token revoked |

**Assignment** -- never a mere field on the device, but its own entry with `from`, `until`,
and a reason. Only this way can one later answer which device ran in apartment 3 in January.

**Zigbee devices per apartment** -- as an inventory list from the heartbeat: room label,
battery level, signal quality, last report. No temperatures (section 6). This list is the
basis for the battery rounds.

**Battery round** -- when it last happened, in which apartment, which cells. Together with
the weakest value from the heartbeat, this makes up the task list people actually work from.

### 20.2 The two flows that matter

**Initial commissioning**

1. Create the apartment (if new), register the device.
2. Press "prepare" → generate the registration code, write the image, put the code on the
   boot partition (section 15.3).
3. Plug in the device. It registers and displays a verification code.
4. In the UI, select the apartment, **confirm the same verification code** → the assignment
   is created, the configuration is released.

**Device swap**

1. In the apartment, select "replace device" and pick the replacement device off the shelf.
2. The service demands an explicit confirmation and **revokes the old device's token** in
   the process. The old one moves to `faulty` or `in_storage`, the old assignment is closed
   with an `until` timestamp.
3. The replacement device gets the configuration and the last encrypted backup; the landlord
   enters the key once (section 15.1).

### 20.3 Rules the service enforces

- **An apartment has at most one active device.** Assigning a second one automatically
  closes the previous assignment -- with a confirmation prompt, never silently.
- **A device belongs to at most one apartment.** For it to move to another, it must have
  been reset beforehand; the service demands an explicit confirmation for this before it
  releases the new apartment's configuration. Otherwise rooms, schedules, and history of one
  tenancy migrate into the next apartment's.
- **No release without a confirmed verification code.** The id alone is never enough.
- **Every change to assignment, state, or token is logged** -- who, when, why. This is the
  same care thermoctl applies to switching decisions, applied to the device inventory.
- **An apartment is not deleted, it is retired.** Deleting would take the history along with
  it, which is exactly what is needed when something is disputed. The retention period from
  section 12 applies to the data.

### 20.4 What the UI shows for this

Alongside the three views from section 9 comes a fourth, quiet one: **Inventory**.
Properties, apartments, devices, with a filter for "in storage" and "faulty". This is the
place for the questions that are not urgent: how many replacement devices are still on the
shelf? Which apartment is running which model? When was the last battery round in
apartment 7?

---

## 21. Remote factory reset, re-provisioning, and looking inside

### 21.1 Two things that are easily confused

| | **Reset the application** | **Re-provision the system** |
|---|---|---|
| What happens | Data stores, containers, keys, and assignment gone, device back in the application's delivery state | The operating system itself is rewritten |
| Remotely | **yes**, in minutes | **only with A/B partitions**, otherwise not at all |
| Needed for | Tenant change, tangled state, device goes into storage | Debian release change, corrupted filesystem |
| Effort | low, doable right away | rebuilding the image recipe |

**The first case covers almost everything that comes up day to day.** The second is the rare
one, and that is what the replacement device is for.

### 21.2 Remote factory reset

Command `factory_reset`, stage 2, with a confirmation prompt in the UI naming the apartment
in the confirmation text (not just "really?"). The agent executes, the watchdog monitors:

1. Stop containers, delete thermoctl's and Zigbee2MQTT's data stores.
2. **Upload one last encrypted backup first** -- even on a tenant change, because the
   retention period decides the deletion, not the button press.
3. Discard its own keys, token, and `agent-registration.json`; regenerate the WireGuard key
   pair.
4. On the fleet side: revoke the token, close the assignment with `until`, set the device to
   `in_storage`.
5. The device then registers again with a **new verification code** and waits for assignment
   -- the same path as initial commissioning (section 15.3).

This makes a tenant change a matter of a few minutes, with no on-site visit, and the device
is guaranteed to carry nothing forward from the previous tenancy.

### 21.3 Re-provisioning: what it really costs

A running system cannot overwrite its own drive. Whoever wants to do this remotely needs
**two system partitions** (A/B) and a bootloader that switches between them -- RAUC, Mender,
or swupdate can do this.

This is doable, but not a small addition, it is a decision with consequences: the image
recipe (section 19) gets a fixed partition scheme, the bootloader gets a contract, every
system update becomes a signed bundle, and the whole thing needs its own test track. In
return you would get: a Debian release change without a visit, and the same fallback safety
for the system that the application already has via its digests.

**My proposal: not for now.** Build section 21.2 first and run it for a heating season. If it
then turns out that on-site visits actually happen because of the system -- and not just
because of defective hardware, where someone has to go anyway -- A/B is the right answer and
can be retrofitted. The decision is reversible as long as the image recipe stays in our own
hands.

There is a cheaper way to buy the same capability, examined and written down but not adopted:
an immutable distribution that brings transactional updates and snapshot rollback with it
(openSUSE Leap Micro). It carries its own costs, in the image recipe and in the container
runtime. See `docs/deferred-options.md` before building A/B partitions -- that is the entry
point for this decision, not a fresh round of research.

**What remains impossible remotely:** a device that no longer boots. That is what the
replacement device on the shelf is for.

### 21.4 SSH for the pilot phase

A legitimate need, and at the same time the function most likely to become a permanent
backdoor. Built with barbs on it, therefore:

- **No permanently listening service.** Access is created on demand: the command
  `open_access` over the command channel, the agent opens an **outbound** back-channel and
  closes it on its own after **60 minutes**. No open port in the apartment, no port
  forwarding.
- **Key only, and the key is short-lived.** The cloud issues an SSH certificate valid for one
  hour; nothing stays on the device after that. No passwords, no permanently deposited
  `authorized_keys`.
- **Only during the pilot phase.** The apartment carries a flag for this (`pilot_mode`). If
  it is not set, **the agent rejects the command** -- the check is local, not in the UI. A
  cloud that has been taken over therefore cannot open a session in production apartments.
- **Visible, not covert.** Every opening, every closing, and the time are recorded in the
  apartment's local log and in the cloud's audit log. The watchdog closes the channel if the
  agent dies.
- **In occupied apartments, this belongs in the privacy notice.** In the pilot apartment --
  your own or a vacant one -- it is unproblematic. After that, every session is access to a
  device in someone else's home.

### 21.5 What SSH mostly replaces

Before anyone opens a session, a command **`diagnostic_bundle`** (stage 1) should be enough:
logs of the four services, versions and digests, container states, memory and disk usage,
Zigbee network state, the last control decisions -- masked, packaged, uploaded. In the vast
majority of cases this answers the question someone wanted to log in for, and leaves behind a
file you can show a second person.

**Decided afterward (project owner, 2026-09-27):** `fetch_logs` (this section, P5.3a) is read
in the cloud and therefore filtered on the device by an **allowlist**, not a denylist -- a
denylist bets that every possible leak has been enumerated; logs change with every library
version and a missed pattern leaks silently and irreversibly. An allowlist fails the other
way instead: something expected is missing, and that is noticed at once. Filtering happens
exclusively in the agent, never in the cloud; every value a placeholder replaces (a
temperature, a setpoint, a name, ...) gets a fixed, dumb placeholder, never a stable hash,
which would let values be correlated across lines even after masking; every dropped line is
counted and the count travels with the excerpt, so nobody debugs a log with an invisible gap;
and the cloud enforces its own retention period on what it stores from this command (14 days
by default, configurable), so `fetch_logs` uploads do not slowly turn the fleet service into
the data store section 6 was written to exclude.

This is also where `diagnostic_bundle` (this section, still open, P5.5b) must stay
categorically different, not just "more of the same, encrypted": a single reading of "heat
demand yes/no per zone" is operationally harmless, exactly like the aggregate zone counts the
heartbeat already carries (section 5). A **series** of such readings over time is a presence
detector -- absence is directly what section 6 excludes. `diagnostic_bundle` may therefore
carry a control-decision snapshot covering a few hours, end-to-end encrypted, delivered once,
like the operational-data backups (section 15.1) -- but it must never become a channel the
cloud's *running* storage accumulates the way `fetch_logs`'s own retention window does. The
distinction is between a bundle and a series, not between "masked" and "encrypted": encrypting
a series would only hide today's presence detector from today's cloud operator, not stop it
from being one.

---

## 22. Addenda from building the scaffold

Four places where the scaffold had to choose a reading. Fixed here, so they do not stay an
assumption.

### 22.1 The keys of fault reports -- evidenced in the source

Checked in `thermoctl/app.py`, `services/publishing.py`, `domain/fault_notice.py`, and
`domain/problem_report.py`:

| Kind | Key | Mapping in the fleet service |
|---|---|---|
| Sensor fault | `sensor:<zone-id>` | prefix `sensor:` |
| Stuck reading | `sensor:<zone-id>` | **the same key as above** |
| Window alarm | `fenster:<zone-id>` | prefix `fenster:` |
| Failed switching command | `schaltbefehl:<device-id>` | prefix `schaltbefehl:` |
| Bridge or broker gone | `zigbee2mqtt:brücke` | fixed value |
| Tenant problem report | `tenant-report:<zone-id>:<category>` | prefix `tenant-report:` |

**The special case matters:** sensor fault and stuck reading deliberately share the same
key -- they can never occur at the same time, and thermoctl maps both onto the same Home
Assistant entity (reasoning in the source of `fault_notice.py`). The fleet service must
therefore **not** infer the kind from this. The kind is in the `titel`/`text`, and the
reliable state comes from the heartbeat anyway (`open_faults[].kind`). An unknown prefix is
treated as "other report", never as an error.

**Added afterward (unified envelope, sections 5 and 21):** all six fault kinds use the same
envelope in the fleet service -- kind, key, timestamp, plain text. The prefixes
`zigbee2mqtt:` and `tenant-report:` (as well as the others from the table above) stay a
**convention within the key**, not their own types -- the kind is a separate, optional field
in the envelope (`None` where the key does not allow an unambiguous mapping, as in the
special case above), not something guessed out of the key's text. The benefit: a new fault
kind costs no protocol change on either side, only a new entry in the prefix table.

**Decided afterward (project owner, 2026-09-24):** the envelope's "plain text" is generated
by the fleet service itself, from `kind` and `key` only -- it is never taken from
thermoctl's `titel`/`text`. Reason, verified in thermoctl's source: a tenant report's `text`
names the tenant ("Reported by: ..."), the last room temperature, the setpoint, the mode,
and the tenant's free-text note; a sensor-fault `text` carries the frost-protection setpoint.
Section 6 forbids all of these in the cloud outright ("the text of tenant problem reports" is
explicitly listed among what is not transmitted). `Event` keeps accepting and validating
`titel`/`text` unchanged (thermoctl's webhook payload is not ours to change), the fleet
service simply never reads either field again once validation has passed.

### 22.2 `since` in the state file

Meaning: **the point in time since which `desired` has applied** -- i.e. when the agent
wrote the new revision. The watchdog computes two things from this: the 10-minute deadline
for the first health report, and the hour until proven status (section 17). Unix seconds, a
whole number, time zone does not matter.

**Decided afterward:** the specification originally showed this field only in one example
(section 17, step 2), without stating its meaning in the text. This reading is not one of
several equally valid possibilities, but the **only** one with which the field can steer the
fallback at all: only if `since` is bound to the *current* desired revision can a deadline be
computed from when it arrived. Any other reading (say, "time of the last change to any
field") would make the 10-minute and the one-hour deadlines from section 17 unusable as soon
as `proven` is advanced without `desired` changing.

### 22.3 The health report

**Line-based like the state file, not a single timestamp** (decided afterward; replaces the
previous assumption of "a single Unix timestamp"). A file under `/run/`, regularly
overwritten by the agent, with three lines:

```
timestamp=1790000123
digest=sha256:9f2c…
version=0.4.0
```

- **`timestamp`**: Unix seconds, as before -- the watchdog checks its age, older than
  **120 seconds** counts as silent.
- **`digest`**: the **currently running** digest, i.e. that of the container revision
  writing this health report right now. This is the actual gain over a plain timestamp: the
  watchdog thereby sees not only that something is alive, but that **the right thing** is
  alive -- a health report left behind from the old revision after a swap (say, because the
  new container has not written yet) does not thereby fake a healthy new version.
- **`version`**: the agent version in plain text, for on-site diagnosis without falling back
  on the digest.

`/run/` stays deliberate: it lives in memory and is empty after a reboot, so an old report
can never cover for a freshly started, not-yet-healthy agent. Unknown future lines are
skipped, as with the state file (section 18.2, applied analogously).

### 22.4 State names

The tables in section 20.1 are prose; the **English** spellings from
`protocol/inventory.py` apply as the machine-readable form, now recorded as their own table
in section 20.1 (decided afterward -- previously German spellings such as `im_einsatz`,
`im_regal` stood there; the deciding factor was that a later UI or an external system is more
likely to expect English identifiers, as was already the case for `FaultKind` in section 5).
Where they disagree, the code governs, not the table -- the table explains, the code decides.
**Only the values are English**, the model and field names (`ApartmentState`,
`DeviceLifecycle`, `state`, ...) stay in the domain language used throughout this codebase.

---

## 23. Status display on the device (Raspberry Pi only)

Two LEDs on the 40-pin header, driven by the **watchdog**. Deliberately there and not in the
agent: the watchdog runs when the containers are down, when the network is gone, and when
the agent is currently being rolled back. That is exactly when someone on site wants to see
where things stand.

### 23.1 Without a single dependency

The LEDs are **not** driven via a GPIO library, but via the kernel driver: in the image
recipe (section 19), `config.txt` carries two entries

```
dtoverlay=gpio-led,gpio=23,label=thermoctl-device
dtoverlay=gpio-led,gpio=24,label=thermoctl-system
```

and the watchdog afterward only writes to files:

```
/sys/class/leds/thermoctl-device/brightness
/sys/class/leds/thermoctl-system/brightness
```

This keeps `go.mod` empty, builds no `ioctl`, and the driving can be reproduced by hand with
`echo` -- worth more when hunting a bug than any library. Blink patterns go through the
kernel `timer` trigger (`delay_on`/`delay_off`), so the watchdog needs no loop of its own for
this.

### 23.2 What the two LEDs say

**LED 1 -- the device** (green):

| Pattern | Meaning |
|---|---|
| off | no power, or the watchdog is not running |
| slow blink | starting, or the agent is not yet healthy |
| steady on | agent healthy, cloud contact established |
| fast blink | waiting for assignment (verification code, section 15.3) |
| two short blinks, pause | no cloud contact -- control still running |

**LED 2 -- the system** (yellow):

| Pattern | Meaning |
|---|---|
| off | thermoctl controlling normally, no open fault |
| slow blink | open fault (sensor, bridge, switching command) |
| steady on | **control stalled** -- no decision for over three cycles |

The baseline is therefore: **green on, yellow off.** One glance is enough, even from a
ladder, and it works without network, without a phone, and without logging in.

### 23.3 Limits and installation location

- **Raspberry Pi only.** A mini PC has no such header. The watchdog checks at startup
  whether the two files exist, and keeps working unchanged without them -- a missing display
  is not an error and must not block anything.
- **In the distribution board, not the living space.** A blinking yellow light in the hallway
  generates phone calls long before anyone would actually notice something -- and faults go
  to you anyway, not to the tenant.
- **Not a substitute for monitoring.** The LED is only seen by whoever stands in front of it.
  It is the display for the person who drives there, not the report to the person deciding
  whether someone should.
- **The header is shared.** If the apartment has an LTE add-on (section 23.4), it occupies
  the same 40-pin header. Both together only works with a stacking header, and the LED pins
  must be routed onto free lines. Check once before buying which lines the chosen add-on
  actually uses -- datasheets are often silent about this.

**Decided afterward (project owner, 2026-09-26):** the two LEDs are driven by a **separate,
small program next to the watchdog** (`watchdog/cmd/thermoctl-leds/`), not by the watchdog
itself as originally planned in the implementation plan. Reason: the watchdog's own line
budget (section 18.3) has almost no headroom left, and a bug in the display code must never
be able to disturb swapping or rolling back the agent -- the one thing the watchdog exists to
do reliably. Nothing else in this section changes: same two sysfs files, same kernel `timer`
trigger, same patterns, Raspberry Pi only.

### 23.4 Mobile connectivity on the Raspberry Pi: an add-on instead of a second device

Where the apartment connects over mobile data, a Raspberry Pi needs **no separate LTE
router**: an add-on with a SIM or eSIM slot sits on the same header, in the same enclosure,
on the same power supply. This saves not just about ten euros, but above all a device that
can fail, be unplugged, or be forgotten. A mini PC or a finished set has no such header and
still needs the router.

Four points that matter here:

- **Antenna routed outside.** Reception is worst in a metal heating cabinet -- exactly where
  the device sits. An antenna with a cable belongs with the add-on, not accessories for
  later.
- **Peak current when transmitting.** A radio module briefly draws significantly more during
  connection setup than a tightly sized power supply delivers. The result is restarts that
  look like a software bug. Choose the power supply generously.
- **Do not look for the eSIM in the add-on, look in the card slot.** The research from
  2026-09-22 found no LTE add-on in this class with a soldered-in eUICC (Waveshare SIM7670G
  and the SIM7600 series, Sixfab Base HAT -- all card slot or mPCIe only). The path to eSIM
  goes through a **eUICC card in nano-SIM format**, such as the sysmoEUICC1-C2G for €23.80.
  This is not a compromise, it is better: a card can be moved over on a device swap, a
  soldered chip cannot -- and carriers usually only issue a profile once. Flow and commands:
  section 24.
- **Ethernet stays free, Wi-Fi stays off.** A quiet side benefit: without Wi-Fi, the radio
  collision with Zigbee on 2.4 GHz disappears entirely.

---

## 24. Managing eSIM profiles remotely

Added on 2026-09-22. Applies only to apartments on the mobile-data variant.

### 24.1 Why this belongs here

Swapping a SIM card in twelve apartments means twelve visits. A carrier switch, an apartment
with bad reception on one network and good on another, a plan that expires -- tweezers and
doorbells every time. With a eUICC card in the card slot, this becomes a command, and
commands are exactly what this service can do.

### 24.2 The parts

| Part | What it does |
|---|---|
| **eUICC card** (e.g. sysmoEUICC1-C2G, €23.80) | Sits in the add-on's nano-SIM slot. Holds several carrier profiles, one of them active. GSMA-certified per SGP.22. |
| **lpac** (`estkme-group/lpac`, open source) | Local Profile Assistant. Talks to the card over the modem's AT channel: list, load, switch, delete. |
| **Agent** | Knows the commands, calls lpac, reports the result. Holds not a single profile itself. |
| **Watchdog** | Has the fallback. If the device fails to report after a profile switch, it restores the previous profile. |

The card's **EID** belongs in the device inventory (section 20), next to the serial number
and id. Without it, some carriers cannot even take a profile order.

### 24.3 Commands

Stage 2, i.e. only after operational experience, and with the same rules as all the others
(id, expiry, local log, result report):

| Command | Effect | Condition |
|---|---|---|
| `esim_profiles_list` | Report the card's profiles with id, name, and state | none, read-only |
| `esim_profile_load` | Download a profile via an activation code (`LPA:1$…`), **without** activating it | Only one apartment at a time. The activation code is deleted from the command record after execution and never appears in the log. |
| `esim_profile_activate` | Switch to an already-loaded profile | **Only with the fallback clock**, see below |
| `esim_profile_delete` | Remove a profile from the card | Never the active profile. Rejected if it is the only loaded one. |

**Never:** activate a profile without a prior download, reset the card, report the
activation code back to the cloud.

### 24.4 The fallback clock -- the actual point

A profile switch cuts exactly the connection the command arrived on. That is the reason this
command must not exist without a fallback:

1. The agent writes the currently active profile into the watchdog's state file and sets a
   **ten-minute** deadline.
2. The agent activates the new profile. The modem re-registers on the network.
3. If a confirmed heartbeat comes through within the deadline, the agent clears the deadline.
   Done.
4. If the deadline runs out, **the watchdog** -- not the agent -- switches back to the
   previous profile and notes this locally. On the next heartbeat, the cloud learns that the
   switch failed.

This is the same split as for updates: *what rolls back must not be the thing that changes.*
The watchdog needs to understand nothing for this -- it calls lpac with a profile id that
sits in the state file.

**Decided afterward, where this lives:** in the watchdog's **existing** state file (section
17, `desired=`/`proven=`/`since=`), as two further lines -- **no second file**:

```
esim_previous_profile=<profile id>
esim_deadline=1790000723
```

This makes the format extensible without having had to "announce" itself as such:
`watchdog/state.go` already skips any unknown line instead of rejecting it (see there,
"unknown keys are ignored"), so it also skips these two, as long as a watchdog does not yet
know them. An older watchdog on a not-yet-updated device therefore keeps reading
`desired`/`proven`/`since` unchanged and ignores the two eSIM lines with no consequence; it
simply cannot yet operate the fallback clock, until its own version catches up (section 17,
"The watchdog itself" -- version changes are rare, announced events). A second, separate file
would have given the same benefit, but created a second place for the watchdog to look for
deadlines -- exactly the kind of duplication the line-based, skippable format from section 17
was meant to avoid from the start.

### 24.5 Limits, named honestly

- **Someone has to go there once.** The card is inserted by someone during setup. Everything
  after that is remote, not the first step.
- **The download needs network.** A new profile loads over the existing connection. If the
  apartment is already offline, the eSIM does not help -- that is not what it is for. This
  can be prepared in the warehouse over LAN.
- **Carriers can be difficult.** Profiles are often issuable only *once* on consumer plans,
  and some plans are tied to device lists. Both are documented for o2. Before ordering for
  every apartment, run through **one** card on **one** device.
- **Not field-tested.** This path is derived from specifications and the available tooling,
  not from a live contract. Until someone has done this once, this section is a plan, not an
  experience report.
