"""TLS terminator in front of the sim fleet, for the VM base stations.

`python -m tools.sim.env.tls_proxy --state-dir D --listen-host H --listen-port P`

Accepts TLS (certificate from `tls.ensure_tls`), and pipes the decrypted bytes to ONE fixed
upstream (the sim fleet on `127.0.0.1:8200`). It is a plain TCP pipe below TLS, no HTTP is
parsed, so a long-lived `GET /v1/commands` event stream passes through without buffering and
every request header (Authorization, ...) reaches the fleet untouched. Standard library only.

It listens on exactly the address it is given and refuses a wildcard address (0.0.0.0, ::)
unless `--allow-wildcard` is passed on purpose. `vms.py` picks the narrowest address the
Lima VMs can reach.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import os
import signal
import ssl
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from tools.sim.env.config import FLEET_HOST, FLEET_PORT, ROOT, state_dir
from tools.sim.env.tls import TlsMaterial, ensure_tls

PID_FILENAME = "tls-proxy.pid"
LOG_FILENAME = "tls-proxy.log"
CHUNK = 64 * 1024


def check_listen_host(host: str, *, allow_wildcard: bool = False) -> None:
    """Raise ValueError for a wildcard address (everything on the LAN) unless allowed."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost":
            return
        raise ValueError(f"listen host must be an IP address, got {host!r}") from None
    if address.is_unspecified and not allow_wildcard:
        raise ValueError(f"refusing to listen on {host}: pass an explicit interface address")


def server_context(material: TlsMaterial) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(material.cert_file), str(material.key_file))
    return context


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()  # no buffering beyond the socket: backpressure both ways
    except (ConnectionError, ssl.SSLError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream: tuple[str, int],
) -> None:
    try:
        up_reader, up_writer = await asyncio.open_connection(*upstream)
    except OSError:
        client_writer.close()
        return
    await asyncio.gather(
        _pipe(client_reader, up_writer),
        _pipe(up_reader, client_writer),
    )


async def serve(
    host: str,
    port: int,
    material: TlsMaterial,
    upstream: tuple[str, int],
    *,
    allow_wildcard: bool = False,
    ready: asyncio.Event | None = None,
    bound: list[int] | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Run until `stop` is set (tests) or forever. `bound` receives the real port (port 0)."""
    check_listen_host(host, allow_wildcard=allow_wildcard)
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, upstream),
        host,
        port,
        ssl=server_context(material),
    )
    if bound is not None:
        bound.append(server.sockets[0].getsockname()[1])
    if ready is not None:
        ready.set()
    async with server:
        if stop is None:  # pragma: no cover - production: until SIGTERM
            await server.serve_forever()
        else:
            await stop.wait()


def pid_file(directory: Path) -> Path:
    return directory / PID_FILENAME


def is_proxy_process(pid: int) -> bool:
    """True if `pid` is alive and is one of our proxies (never kill an unrelated pid)."""
    out = subprocess.run(  # noqa: S603
        ["ps", "-p", str(pid), "-o", "command="],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return out.returncode == 0 and "tools.sim.env.tls_proxy" in out.stdout


def running_pid(directory: Path) -> int | None:
    try:
        pid = int(pid_file(directory).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return pid if is_proxy_process(pid) else None


def start_background(directory: Path, host: str, port: int) -> int:  # pragma: no cover - spawns
    """Start the proxy detached (log in `<state>/tls-proxy.log`); returns its pid."""
    check_listen_host(host)
    existing = running_pid(directory)
    if existing is not None:
        return existing
    ensure_tls(directory)
    log = open(directory / LOG_FILENAME, "ab")  # noqa: SIM115 - handed to the child
    cmd = [
        sys.executable,
        "-m",
        "tools.sim.env.tls_proxy",
        "--state-dir",
        str(directory),
        "--listen-host",
        host,
        "--listen-port",
        str(port),
    ]
    child = subprocess.Popen(  # noqa: S603
        cmd, cwd=ROOT, stdout=log, stderr=log, start_new_session=True
    )
    log.close()
    fd = os.open(pid_file(directory), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as out:
        out.write(f"{child.pid}\n")
    return child.pid


def stop_background(directory: Path) -> bool:
    """SIGTERM the proxy of this state dir if it is ours; True if one was stopped."""
    pid = running_pid(directory)
    pid_file(directory).unlink(missing_ok=True)
    if pid is None:
        return False
    os.kill(pid, signal.SIGTERM)
    return True


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m tools.sim.env.tls_proxy", description=__doc__)
    ap.add_argument("--state-dir", type=Path, default=None)
    ap.add_argument("--listen-host", required=True)
    ap.add_argument("--listen-port", type=int, required=True)
    ap.add_argument("--upstream-port", type=int, default=FLEET_PORT)
    ap.add_argument("--allow-wildcard", action="store_true")
    return ap.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - process entry
    args = parse_args(argv)
    directory = args.state_dir or state_dir()
    try:
        check_listen_host(args.listen_host, allow_wildcard=args.allow_wildcard)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    material = ensure_tls(directory)
    print(
        f"tls proxy on {args.listen_host}:{args.listen_port} -> {FLEET_HOST}:{args.upstream_port}"
    )
    stop = asyncio.Event()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await serve(
            args.listen_host,
            args.listen_port,
            material,
            (FLEET_HOST, args.upstream_port),
            allow_wildcard=args.allow_wildcard,
            stop=stop,
        )

    asyncio.run(run())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
