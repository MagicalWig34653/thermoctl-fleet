"""Confined HTTP client for persona sessions (replaces raw curl).

Usage: python -m tools.sim.http GET|POST URL [--json JSON] [--form k=v ...]
                                [--cookie-jar FILE]

Only http/https URLs to 127.0.0.1/localhost (or the hosts in env
SIM_ALLOWED_HOSTS, comma-separated) are allowed. No userinfo, no redirect
following: a Location header is printed instead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

import httpx

DEFAULT_HOSTS = frozenset({"127.0.0.1", "localhost"})
MAX_BODY = 20000


def allowed_hosts() -> frozenset[str]:
    raw = os.environ.get("SIM_ALLOWED_HOSTS", "")
    hosts = frozenset(h.strip() for h in raw.split(",") if h.strip())
    return hosts or DEFAULT_HOSTS


def check_url(url: str, allowed: frozenset[str] | set[str]) -> None:
    """Raise ValueError unless the URL is a plain http(s) URL to an allowed host."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"scheme {parts.scheme!r} not allowed")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise ValueError("userinfo in URL not allowed")
    host = parts.hostname
    if not host or host not in allowed:
        raise ValueError(f"host {host!r} not allowed")


def confine_to_cwd(path: Path) -> Path:
    """Resolve `path` (symlinks included) and require it to lie inside the cwd.

    Persona sessions run in a scratch dir; every file a tool writes must stay there.
    Raises ValueError otherwise.
    """
    try:
        resolved = path.resolve()
        resolved.relative_to(Path.cwd().resolve())
    except (OSError, ValueError) as exc:
        raise ValueError("path must be inside the working directory") from exc
    return resolved


def _load_jar(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def main(argv: Sequence[str] | None = None, *, client: httpx.Client | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.sim.http")
    ap.add_argument("method", choices=["GET", "POST"])
    ap.add_argument("url")
    ap.add_argument("--json", dest="json_body")
    ap.add_argument("--form", action="append", default=[])
    ap.add_argument("--cookie-jar", type=Path)
    args = ap.parse_args(argv)

    if args.cookie_jar is not None:
        # Confine the jar to the persona scratch dir (= cwd): no writes elsewhere.
        try:
            args.cookie_jar = confine_to_cwd(args.cookie_jar)
        except ValueError:
            print("refused: --cookie-jar must be inside the working directory", file=sys.stderr)
            return 2

    try:
        check_url(args.url, allowed_hosts())
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    kwargs: dict[str, object] = {}
    headers: dict[str, str] = {}
    if args.json_body is not None:
        try:
            kwargs["json"] = json.loads(args.json_body)
        except ValueError as exc:
            print(f"refused: invalid --json: {exc}", file=sys.stderr)
            return 2
    elif args.form:
        kwargs["data"] = dict(item.split("=", 1) for item in args.form if "=" in item)
    jar = _load_jar(args.cookie_jar) if args.cookie_jar else {}
    if jar:
        headers["cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())

    own = client is None
    http = client or httpx.Client(follow_redirects=False, trust_env=False, timeout=30.0)
    try:
        resp = http.request(args.method, args.url, headers=headers, **kwargs)  # type: ignore[arg-type]
    except httpx.InvalidURL as exc:
        print(f"error: invalid URL: {exc}", file=sys.stderr)
        return 2
    except (httpx.HTTPError, httpx.StreamError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if own:
            http.close()

    print(f"HTTP {resp.status_code} {resp.reason_phrase}")
    for name in ("content-type", "location"):
        if name in resp.headers:
            print(f"{name}: {resp.headers[name]}")
    for raw in resp.headers.get_list("set-cookie"):
        print(f"set-cookie: {raw.split('=', 1)[0].strip()}")
    if args.cookie_jar:
        for raw in resp.headers.get_list("set-cookie"):
            pair = raw.split(";", 1)[0]
            if "=" in pair:
                k, v = pair.split("=", 1)
                jar[k.strip()] = v.strip()
        args.cookie_jar.write_text(json.dumps(jar))
    print()
    print(resp.content[:MAX_BODY].decode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
