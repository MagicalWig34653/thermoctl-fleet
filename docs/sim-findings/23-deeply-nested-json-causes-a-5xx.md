# Finding: Deeply nested JSON causes a 5xx

- **Area:** security
- **Severity:** minor
- **Source:** pentest check G06 (tools/sim/pentest), run against the sim

## Expected

A 5000-deep JSON array in the body is answered 4xx (not 5xx) and the service stays up.

## Actual

status 500; healthz afterwards ok=True

`POST /v1/heartbeat` with a valid token and a body of 5000 nested `[` ... `]` answered `500` (`RecursionError: maximum recursion depth exceeded` in the fleet log, `Exception in ASGI application`). Without a token the same body gets 401.

## Evidence

```
POST /v1/heartbeat as sim-a12 with 5000 nested arrays
```

## Suggested direction

Treat a `RecursionError` while parsing the body as a 400/422, or put a body-size limit (see finding 22) in front, which also bounds the nesting depth. The request needs a valid token (an anonymous caller gets 401 first), so impact is a noisy 500 and a stack trace in the log, not a service outage (healthz stayed 200).
