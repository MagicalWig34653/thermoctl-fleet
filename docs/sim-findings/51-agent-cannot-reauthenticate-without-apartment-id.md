# Finding 51: after a tenant change the base station's agent exits instead of re-authenticating, the image recipe never passes `--apartment-id`

- **Area:** agent (`agent/__main__.py`) and base station image recipe (`image/common/agent-compose.yml`, `docker/Dockerfile.agent`)
- **Severity:** major (every tenant change locks the apartment's base station out of the fleet until someone intervenes; the apartment goes silent and the fleet raises `not_reporting`)
- **Source:** Task 13 (sim-a01, run 2, week 53), reproduced live on the Lima VM base station

## Expected

Specification section 12 (tenant change, "decided afterward" 2026-10-01): the landlord's tenant change rotates the apartment token server-side. The agent notices through the `401 ... error="reauth_required"` on its next call, runs the signed token-rotation flow with its device key and carries on with the new token, without human intervention.

## Actual

The re-authentication is only attempted if the agent was started with `--apartment-id`. The image recipe starts it without (`CMD ["python", "-m", "agent"]`, no `command:` in the compose file), so on the `reauth_required` 401 the agent re-raises, prints `token re-authentication already attempted once; refusing to retry again. Check --apartment-id ...` and exits with code 1. After that the old token is dead for good (the next fleet call with it answers 403, i.e. `command authorization refused; token revoked or invalid`), so even a restart of the container does not help. The device key is still on the box, but nothing ever uses it.

## Evidence

- `agent/__main__.py:209-210`: `if reauthenticated or not args.apartment_id: ... raise`. `--apartment-id` is defined at `agent/__main__.py:317` with `default=None`; its help text only mentions backups (`:318-324`), nothing says it is also required for token recovery.
- `docker/Dockerfile.agent:31` `CMD ["python", "-m", "agent"]`; `image/common/agent-compose.yml` (service `agent`, from line 31) has no `command:` and no argument carrying the apartment id; `grep -rn apartment-id image/ docs/specification.md` finds nothing.
- Live (VM `thermoctl-sim-a01`): tenant change `tenant_change:sim-a01:0` applied during run 2; `docker ps -a` showed `thermoctl-agent Exited (1)`, last log line `thermoctl-agent: command authorization refused; token revoked or invalid.`; the heartbeat sender got 403 on every call. The apartment id is available on the device (the registration status after enrolment) but the agent does not read it from there.
- The simulation now works around it: `tools/sim/env/vm_reauth.py` runs the product's own `agent.token_rotation.reauthenticate` in a one-shot container with the agent's mounts and starts the agent container afterwards.

## Suggested direction

Take the apartment id from what the device already knows (the registration result stored in the data dir at enrolment) instead of an optional CLI flag, or make the image recipe pass `--apartment-id` and fail the container at start when it is missing. Add a test that a `reauth_required` on a freshly started agent without the flag still recovers (or refuses to start). Independent of that, consider not ending the process on `CommandStreamAuthError` for the one case where the agent could still recover.
