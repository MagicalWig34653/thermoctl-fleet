# Finding: API responses lack hardening headers

- **Area:** security
- **Severity:** nit
- **Source:** pentest check S06 (tools/sim/pentest), run against the sim

## Expected

API responses (/v1, /healthz) carry the same frame/referrer/CSP hardening as UI pages (informational depth: JSON endpoints are not framed, so a miss is a nit).

## Actual

missing: ['/healthz:x-frame-options', '/healthz:referrer-policy', '/healthz:content-security-policy', '/v1/commands:x-frame-options', '/v1/commands:referrer-policy', '/v1/commands:content-security-policy']

`/healthz` and `/v1/commands` carry none of the three headers the `/ui` pages carry.

## Evidence

```
GET /healthz /v1/commands
```

## Suggested direction

Optional: send `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `Content-Security-Policy: default-src 'none'` on JSON responses too (the middleware already exists, only its path filter `startswith("/ui")` keeps them out). Low value because the API returns no HTML, so this is a nit.
