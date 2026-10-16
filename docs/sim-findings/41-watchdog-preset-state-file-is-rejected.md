# Finding 41: the pre-set watchdog state file is rejected by the watchdog

- **Area:** logic / image
- **Severity:** major
- **Source:** Task 9d, live in a VM (check mode of the installed binary)

## Expected

`image/common/install.sh` writes `/var/lib/thermoctl-watchdog/state.env` "present from
the very first boot ... so the watchdog always has *something* to read"
(specification section 22.5, "Fallback without a proven revision").

## Actual

The file contains `DIGEST=` and `PROVEN=false`. The watchdog (`watchdog/state.go:39`)
reads the keys `desired` and `proven` (lower case, the same keys the agent writes,
`agent/loop.py:3660` `desired=...`) and refuses the file when `desired` is empty:

```
$ /usr/local/bin/thermoctl-watchdog -check-mode -file /var/lib/thermoctl-watchdog/state.env
thermoctl-watchdog: state file: 'desired' is missing or empty        (exit 2)
```

So the build-time fallback is not readable by the program it exists for, even if the
service user (finding 40) existed: until the agent has written a real revision, every
loop iteration logs this error. And with no `desired` digest there is nothing to start:
`runtime.go` refuses anything that is not `sha256:<64 hex>`.

## Evidence

- `image/common/install.sh:352-356` (`DIGEST=` / `PROVEN=false`, comment "Pre-set ... section 22.5").
- `watchdog/state.go:39-41`, `agent/loop.py:3660`.
- Check-mode run above, in sim-a01 and sim-a02 after provisioning.

## Suggested direction

Decide what the first-boot fallback means when no image digest has ever been proven (the
recipe could name the digest of the preloaded image) and write it with the keys the
watchdog reads. Add a contract check (`watchdog/check_contract.sh` style) that feeds the
file `install.sh` writes through `-check-mode`.
