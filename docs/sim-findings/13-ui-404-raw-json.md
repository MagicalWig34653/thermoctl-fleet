# Finding 13: unknown `/ui/...` paths answer with bare JSON, not an app page

- **Area:** ux
- **Severity:** minor
- **Source:** reported by personas `landlord` and `admin` (week 0), re-checked by hand

## Expected

A person who mistypes or follows an old bookmark under `/ui/` gets a page in the
app's look, in German, with the navigation and a way back to the overview.

## Actual

`GET /ui/nonexistent` (with or without a session) returns `404` with
`content-type: application/json` and the body `{"detail":"Not Found"}`: English,
raw, no navigation, empty page title.

## Evidence

```
$ curl -s -w ' | %{http_code} %{content_type}' 127.0.0.1:8200/ui/nonexistent
{"detail":"Not Found"} | 404 application/json
```

No exception handler for 404 on the `ui_router` paths in `fleet/app.py`.

## Suggested direction

A 404 handler that, for paths under `/ui/`, renders a templated page with the
normal chrome ("Diese Seite gibt es nicht", link to the overview).
