"""Confined browser tool for persona sessions (Playwright, headless Chromium).

Usage: python -m tools.sim.browse shot --url URL --out PATH [--width 390] [--dark]
                                       [--role ROLE]
       python -m tools.sim.browse text --url URL [--role ROLE]
       python -m tools.sim.browse act --url URL [--role ROLE] --steps JSON [--shot PATH]

`act` performs at most 20 steps (fill / click / select / check / wait_text) on the
page and then prints the final URL, title, last navigation status, visible text and
link targets. Values typed into password fields are never echoed.

Same allow-list as `tools.sim.http` (127.0.0.1/localhost or env
SIM_ALLOWED_HOSTS); the browser additionally aborts every sub-request and closes every
WebSocket to a host outside it. `--out` must lie inside the working directory. With `--role`, the
Playwright storage state `<SIM_STORAGE_STATE_DIR>/<role>.json` is loaded when it
exists (missing file = anonymous). HTTPS errors are never ignored, service
workers and downloads are off.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

from tools.sim.http import allowed_hosts, check_url, confine_to_cwd

MAX_TEXT = 20000
DEFAULT_WIDTH = 390
VIEWPORT_HEIGHT = 844
NAV_TIMEOUT_MS = 30000
MAX_LINKS = 60
MAX_STEPS = 20
MAX_SELECTOR = 200
MAX_VALUE = 500
STEP_TIMEOUT_MS = 10000
_ECHO_MAX = 80
_ROLE = re.compile(r"[a-z0-9_-]+")
_STEP_KINDS = ("fill", "click", "select", "check", "wait_text")
_WITH_VALUE = ("fill", "select")
_PASSWORD = re.compile(r"password", re.IGNORECASE)


@dataclass(frozen=True)
class Step:
    kind: str  # one of _STEP_KINDS
    selector: str  # CSS selector, `text=Label`, or the text for wait_text
    value: str | None = None


@dataclass(frozen=True)
class BrowseArgs:
    command: str  # "shot" | "text" | "act"
    url: str
    out: Path | None = None
    width: int = DEFAULT_WIDTH
    dark: bool = False
    role: str | None = None
    steps: tuple[Step, ...] = ()


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def validate_role(role: str) -> str:
    if not _ROLE.fullmatch(role):
        raise ValueError(f"role {role!r} must match [a-z0-9_-]+")
    return role


def parse_steps(raw: str) -> list[Step]:
    """Parse and validate the `--steps` JSON; raises ValueError on anything off-list."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"--steps is not valid JSON: {exc}") from None
    if not isinstance(data, list) or not data:
        raise ValueError("--steps must be a non-empty JSON list")
    if len(data) > MAX_STEPS:
        raise ValueError(f"--steps allows at most {MAX_STEPS} steps")
    steps: list[Step] = []
    for number, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"step {number}: must be an object")
        kinds = [k for k in item if k in _STEP_KINDS]
        if len(kinds) != 1:
            raise ValueError(f"step {number}: exactly one of {', '.join(_STEP_KINDS)}")
        kind = kinds[0]
        allowed_keys = {kind, "value"} if kind in _WITH_VALUE else {kind}
        if set(item) - allowed_keys:
            raise ValueError(f"step {number}: unexpected keys {sorted(set(item) - allowed_keys)}")
        selector = item[kind]
        if not isinstance(selector, str) or not selector:
            raise ValueError(f"step {number}: selector must be a non-empty string")
        if len(selector) > MAX_SELECTOR:
            raise ValueError(f"step {number}: selector longer than {MAX_SELECTOR} characters")
        value: str | None = None
        if kind in _WITH_VALUE:
            value = item.get("value")
            if not isinstance(value, str):
                raise ValueError(f"step {number}: {kind} needs a string value")
            if len(value) > MAX_VALUE:
                raise ValueError(f"step {number}: value longer than {MAX_VALUE} characters")
        steps.append(Step(kind, selector, value))
    return steps


def display_value(selector: str, value: str) -> str:
    """What may be echoed back for a typed value: nothing for password fields."""
    if _PASSWORD.search(selector):
        return "***"
    return value if len(value) <= _ECHO_MAX else value[:_ECHO_MAX] + "..."


def describe_step(number: int, step: Step) -> str:
    text = f"{number}. {step.kind} {step.selector}"
    if step.value is not None:
        text += f" = {display_value(step.selector, step.value)}"
    return text


def format_act_report(
    *,
    log: Sequence[str],
    url: str,
    title: str,
    status: int | None,
    body: str,
    links: str,
) -> str:
    shown = "unknown" if status is None else str(status)
    head = "\n".join(["steps:", *log])
    return f"{head}\n\nurl: {url}\nstatus: {shown}\ntitle: {title}\n\n{body[:MAX_TEXT]}\n\n{links}"


def request_allowed(url: str, allowed: frozenset[str] | set[str]) -> bool:
    """Host-block predicate for browser sub-requests (data: URLs carry no network)."""
    if urlsplit(url).scheme == "data":
        return True
    try:
        check_url(url, allowed)
    except ValueError:
        return False
    return True


def websocket_allowed(url: str, allowed: frozenset[str] | set[str]) -> bool:
    """Host-block predicate for WebSockets: ws/wss get the http/https host checks."""
    parts = urlsplit(url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme)
    if scheme is None:
        return False
    return request_allowed(parts._replace(scheme=scheme).geturl(), allowed)


def anonymous_note(role: str | None, state: Path | None) -> str | None:
    """Stderr hint when a role was asked for but no stored login applies."""
    if role is None or state is not None:
        return None
    return f"note: running anonymous (no stored login for role {role})"


def storage_state_path(role: str | None, env: Mapping[str, str]) -> Path | None:
    """Storage-state file of `role`, or None (no role, no dir configured, no file)."""
    directory = env.get("SIM_STORAGE_STATE_DIR")
    if role is None or not directory:
        return None
    candidate = Path(directory) / f"{validate_role(role)}.json"
    return candidate if candidate.is_file() else None


def build_cli_args(
    argv: Sequence[str], allowed: frozenset[str] | set[str] | None = None
) -> BrowseArgs:
    """Parse and validate; raises ValueError on anything the tool refuses."""
    ap = _Parser(prog="python -m tools.sim.browse")
    sub = ap.add_subparsers(dest="command", required=True, parser_class=_Parser)
    shot = sub.add_parser("shot")
    shot.add_argument("--url", required=True)
    shot.add_argument("--out", type=Path, required=True)
    shot.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    shot.add_argument("--dark", action="store_true")
    shot.add_argument("--role")
    text = sub.add_parser("text")
    text.add_argument("--url", required=True)
    text.add_argument("--role")
    act = sub.add_parser("act")
    act.add_argument("--url", required=True)
    act.add_argument("--role")
    act.add_argument("--steps", required=True)
    act.add_argument("--shot", type=Path)
    ns = ap.parse_args(list(argv))

    check_url(ns.url, allowed if allowed is not None else allowed_hosts())
    role = validate_role(ns.role) if ns.role is not None else None
    if ns.command == "text":
        return BrowseArgs("text", ns.url, role=role)
    if ns.command == "act":
        return BrowseArgs(
            "act",
            ns.url,
            out=confine_to_cwd(ns.shot) if ns.shot is not None else None,
            role=role,
            steps=tuple(parse_steps(ns.steps)),
        )
    if not 200 <= ns.width <= 3000:
        raise ValueError("--width must be between 200 and 3000")
    return BrowseArgs(
        "shot", ns.url, out=confine_to_cwd(ns.out), width=ns.width, dark=ns.dark, role=role
    )


def format_links(
    raw: Sequence[tuple[str, str]], page_url: str, allowed: frozenset[str] | set[str]
) -> str:
    """Link targets of a page as a person would find them by hovering: text -> path.

    Same-host links are shown as paths, other hosts as full URLs marked external;
    anchors, javascript: and duplicates are dropped. Without this a text-only
    view has no way to discover where a button leads.
    """
    here = urlsplit(page_url)
    seen: set[str] = set()
    lines: list[str] = []
    for text, href in raw:
        parts = urlsplit(href)
        if parts.scheme not in ("http", "https"):
            continue
        if parts.netloc == here.netloc and parts.path == here.path and not parts.query:
            continue  # same page (anchor / skip link)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        if parts.netloc != here.netloc:
            target = f"{href} (external)"
        label = " ".join(text.split()) or "(no text)"
        entry = f"- {label[:60]} -> {target}"
        if entry in seen:
            continue
        seen.add(entry)
        lines.append(entry)
        if len(lines) >= MAX_LINKS:
            break
    return "\n".join(["links:", *lines]) if lines else "links: none"


@contextmanager
def _open_page(args: BrowseArgs, allowed: frozenset[str]) -> Iterator[Any]:  # pragma: no cover
    """One guarded browser context and page; shared by shot, text and act."""
    from playwright.sync_api import Route, WebSocketRoute, sync_playwright

    state = storage_state_path(args.role, os.environ)
    note = anonymous_note(args.role, state)
    if note:
        print(note, file=sys.stderr)

    def guard_ws(ws: WebSocketRoute) -> None:
        # Context routes do not see WebSockets; without connect_to_server the
        # socket never reaches the server.
        if websocket_allowed(ws.url, allowed):
            ws.connect_to_server()
        else:
            ws.close()

    def guard(route: Route) -> None:
        if request_allowed(route.request.url, allowed):
            route.continue_()
        else:
            route.abort()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            context = browser.new_context(
                viewport={"width": args.width, "height": VIEWPORT_HEIGHT},
                color_scheme="dark" if args.dark else "light",
                storage_state=str(state) if state else None,
                ignore_https_errors=False,
                service_workers="block",
                accept_downloads=False,
            )
            context.route("**/*", guard)
            context.route_web_socket("**/*", guard_ws)
            yield context.new_page()
        finally:
            browser.close()


def _page_text(page: Any, allowed: frozenset[str]) -> tuple[str, str]:  # pragma: no cover
    body = page.inner_text("body")[:MAX_TEXT]
    anchors = page.eval_on_selector_all(
        "a[href]",
        "els => els.map(e => [e.innerText || e.getAttribute('aria-label') || '', e.href])",
    )
    return body, format_links([(str(t), str(h)) for t, h in anchors], page.url, allowed)


def _run_step(page: Any, step: Step) -> None:  # pragma: no cover
    if step.kind == "fill":
        assert step.value is not None
        page.fill(step.selector, step.value, timeout=STEP_TIMEOUT_MS)
    elif step.kind == "click":
        page.click(step.selector, timeout=STEP_TIMEOUT_MS)
        with suppress(Exception):  # no navigation followed
            page.wait_for_load_state("load", timeout=STEP_TIMEOUT_MS)
    elif step.kind == "select":
        assert step.value is not None
        page.select_option(step.selector, step.value, timeout=STEP_TIMEOUT_MS)
    elif step.kind == "check":
        page.check(step.selector, timeout=STEP_TIMEOUT_MS)
    else:
        page.get_by_text(step.selector).first.wait_for(timeout=STEP_TIMEOUT_MS)


def _browse(args: BrowseArgs, allowed: frozenset[str]) -> str:  # pragma: no cover
    # Needs a real Chromium; exercised by the smoke run, not by unit tests.
    with _open_page(args, allowed) as page:
        last_status: list[int | None] = [None]

        def on_response(response: Any) -> None:
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                last_status[0] = response.status

        page.on("response", on_response)
        page.goto(args.url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        if args.command == "shot":
            assert args.out is not None
            page.screenshot(path=str(args.out), full_page=True)
            return f"saved {args.out.name}"
        if args.command == "act":
            log: list[str] = []
            for number, step in enumerate(args.steps, 1):
                try:
                    _run_step(page, step)
                except Exception as exc:
                    reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                    log.append(f"{describe_step(number, step)}  -> FAILED: {reason[:200]}")
                    break
                log.append(f"{describe_step(number, step)}  -> ok")
            if args.out is not None:
                page.screenshot(path=str(args.out), full_page=True)
                log.append(f"screenshot saved {args.out.name}")
            body, links = _page_text(page, allowed)
            return format_act_report(
                log=log,
                url=page.url,
                title=page.title(),
                status=last_status[0],
                body=body,
                links=links,
            )
        body, links = _page_text(page, allowed)
        return f"title: {page.title()}\n\n{body}\n\n{links}"


def main(argv: Sequence[str] | None = None) -> int:
    allowed = allowed_hosts()
    try:
        args = build_cli_args(sys.argv[1:] if argv is None else argv, allowed)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    try:
        print(_browse(args, allowed))
    except Exception as exc:  # pragma: no cover - browser/network failure
        print(
            f"error: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
