# Finding 40: the watchdog's service user is never created, so the watchdog never runs on an image built by the recipe

- **Area:** logic / image
- **Severity:** blocker
- **Source:** Task 9d, live in the Lima VMs (sim-a01, sim-a02), `image/common/install.sh` applied unmodified

## Expected

After `image/common/install.sh` the base station runs `thermoctl-watchdog.service`
(enabled by the script), the one program that must still work when everything else is
broken (specification section 18.3). Its unit names the account it runs as, so the
recipe has to create that account and put it into the `docker` group.

## Actual

`watchdog/thermoctl-watchdog.service` has `User=thermoctl-watchdog` / `Group=docker`,
but nothing in the recipe creates the user (no `useradd`, `sysusers.d` entry or
equivalent under `image/`; the unit's own comment says only that the account "depends on
the prepared system image"). In both VMs the unit is enabled and fails on every
(re)start, every 5 seconds, without ever executing the binary:

```
thermoctl-watchdog.service: Failed at step USER spawning /usr/local/bin/thermoctl-watchdog: No such process
thermoctl-watchdog.service: Main process exited, code=exited, status=217/USER
```

`id thermoctl-watchdog` -> `no such user`. On a real device the watchdog (swap, rollback
to the proven digest, three-restart rule) would therefore never run; the agent's A/B
safety net (specification section 17) does not exist. Nothing else notices: the agent
runs fine without it.

## Evidence

- `watchdog/thermoctl-watchdog.service:53-54`; `grep -rn useradd image/` finds only a
  comment about the agent's uid 10002.
- VM run (docs/sim-findings/02-vm-run.md): `systemctl status thermoctl-watchdog` ->
  `Active: activating (auto-restart) (Result: exit-code)`, `status=217/USER`, after both
  the first provisioning and every reboot (power-loss cycle).
- The Go binary itself builds and installs fine (`/usr/local/bin/thermoctl-watchdog`).

## Suggested direction

Create the account in `install.sh` (a `sysusers.d` file is the idempotent form: system
user `thermoctl-watchdog`, member of `docker`), and add a test to the image checks that
every `User=`/`Group=` named in an installed unit exists after the recipe ran. Better
still, a boot check in the image build that starts the unit once and fails if it exits.
