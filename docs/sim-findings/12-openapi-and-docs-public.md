# Finding 12: `/openapi.json` and `/docs` are served without a login

- **Area:** security
- **Severity:** minor
- **Source:** reported by persona `admin` (week 0), re-checked by hand

## Expected

A service that sits on the public internet and is only meant for its own agents
and a login-protected UI does not publish its full interface description, with
its internal docstrings, to anonymous callers.

## Actual

`GET /openapi.json` and `GET /docs` answer `200` with no cookie and no token.
The schema carries the route docstrings, which describe login throttling,
the WebAuthn begin-throttle budget and other internal reasoning.

## Evidence

```
$ curl -s -o /dev/null -w '%{http_code}' 127.0.0.1:8200/openapi.json   # 200
$ curl -s -o /dev/null -w '%{http_code}' 127.0.0.1:8200/docs           # 200
```

`fleet/app.py:589`: `FastAPI(title="thermoctl-fleet", version=..., lifespan=...)`
with no `docs_url=None` / `openapi_url=None` / `redoc_url=None`.

## Suggested direction

Disable the three URLs in production (or put them behind the UI session). If
they are wanted for development, make it an explicit setting that defaults to
off.
