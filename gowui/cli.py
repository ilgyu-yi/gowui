"""The command line (SPEC §9): the only place that chooses a launch mode and builds its policy
bundle. ``gowui`` and ``gowui local`` run local mode.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI

from .app import create_app
from .engine import PROTOCOLS
from .local_mode import (JsonFileStorage, LocalIdentity, MemoryStorage, StateFileError,
                         check_state_path, default_state_path, local_policies)

__all__ = ["build_app", "build_config", "main", "parse", "url_line", "uvicorn_config"]

log = logging.getLogger("gowui")

#: The WebSocket message limit (§7.6).
WS_MAX_SIZE = 1024 * 1024
LOG_LEVELS = ("critical", "error", "warning", "info", "debug", "trace")
#: The subcommands; #9 adds ``serve`` and ``user``.
COMMANDS = ("local",)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gowui", description="Browser Go GUI for Go engines.")
    commands = parser.add_subparsers(dest="command", required=True)
    local = commands.add_parser("local", help="run for one person on this machine")
    local.add_argument("--host", default="127.0.0.1", help="address to bind (default 127.0.0.1)")
    local.add_argument("--port", type=int, default=8080, help="port to bind; 0 picks a free one")
    local.add_argument("--engine-protocol", choices=list(PROTOCOLS), default="gtp",
                       help="default engine protocol of the connect form")
    local.add_argument("--engine-host", default="127.0.0.1", help="default engine host")
    local.add_argument("--engine-port", type=int, default=6363, help="default engine port")
    local.add_argument("--connect", action="store_true",
                       help="connect to the engine flags at startup")
    storage = local.add_mutually_exclusive_group()
    storage.add_argument("--state", type=Path, default=None, metavar="FILE",
                         help="state file (default: the per-user state file)")
    storage.add_argument("--fresh", action="store_true",
                         help="keep state in memory only; no file is read or written")
    local.add_argument("--log-level", choices=LOG_LEVELS, default="info")
    return parser


def parse(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line; no subcommand, or a first argument starting with ``-``, is
    ``local``."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0].startswith("-") and args[0] not in ("-h", "--help"):
        args = ["local", *args]
    return _parser().parse_args(args)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def build_app(args: argparse.Namespace) -> FastAPI:
    """The local app from the flags: the bundle, and a startup hook that loads the owner's space.

    Raises :class:`StateFileError` when the state path exists and is not a regular file (§8.3).
    """
    if args.fresh:
        storage: Any = MemoryStorage()
    else:
        path = (args.state if args.state is not None
                else default_state_path(sys.platform, os.environ, Path.home()))
        check_state_path(path)
        storage = JsonFileStorage(path)
    engine = {"protocol": args.engine_protocol, "host": args.engine_host,
              "port": args.engine_port}
    policies = local_policies(host=args.host, storage=storage, engine_defaults=engine)

    async def load_owner(app: FastAPI) -> None:
        space = await app.state.registry.get(LocalIdentity.OWNER)
        # --connect goes through the one connect path, unless the restored space reconnects.
        if args.connect and not space.session.snapshot()["engine"]["connected"]:
            await space.session.handle({"type": "connect", **engine})

    return create_app(policies, startup=[load_owner])


def _websocket_protocol() -> Any:
    """uvicorn's WebSocket protocol, with the Origin check left to the guard.

    The websockets library answers a handshake with two ``Origin`` headers with a ``400`` before
    the app sees it; the guard refuses it the way it refuses every Origin, accepted and closed with
    ``4403`` (§4.3, §7.4). Falls back to uvicorn's default when its internals differ.
    """
    try:
        from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
        from websockets.server import ServerProtocol
    except ImportError:  # pragma: no cover - another uvicorn or websockets layout
        return "auto"

    class GuardedOrigin(ServerProtocol):
        def process_origin(self, headers: Any) -> None:
            return None  # the guard applies the Origin rule, duplicates included

    class Protocol(WebSocketsSansIOProtocol):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            if type(self.conn) is ServerProtocol:
                self.conn.__class__ = GuardedOrigin  # adds no state, overrides one check

    return Protocol


def uvicorn_config(app: Any, *, host: str, port: int, log_level: str = "info") -> uvicorn.Config:
    """The server settings of §9: no forwarded-header rewriting, 1 MiB frames, lifespan on, no
    ``Server`` header."""
    return uvicorn.Config(app, host=host, port=port, log_level=log_level, proxy_headers=False,
                          ws=_websocket_protocol(), ws_max_size=WS_MAX_SIZE, lifespan="on",
                          server_header=False)


def build_config(args: argparse.Namespace) -> uvicorn.Config:
    if not _is_loopback(args.host):
        log.warning("gowui: bound to %s, so the app is unauthenticated and reachable from the "
                    "network: anyone who can reach it can drive it and make it connect to any "
                    "engine address. Use `gowui serve` to share it.", args.host)
    return uvicorn_config(build_app(args), host=args.host, port=args.port,
                          log_level=args.log_level)


def url_line(host: str, port: int) -> str:
    shown = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"gowui: http://{shown}:{port}"


class _Server(uvicorn.Server):
    """Prints the URL line once the socket is bound."""

    def __init__(self, config: uvicorn.Config, host: str) -> None:
        super().__init__(config)
        self._host = host

    async def startup(self, sockets: Any = None) -> None:
        await super().startup(sockets=sockets)
        if self.started and self.servers and self.servers[0].sockets:
            port = self.servers[0].sockets[0].getsockname()[1]
            print(url_line(self._host, port), flush=True)


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        config = build_config(args)
    except StateFileError as exc:
        _parser().error(str(exc))  # a usage error: exits with status 2 (§9)
    try:
        _Server(config, args.host).run()
    except KeyboardInterrupt:  # Ctrl-C after a clean shutdown (§9 "Stopping")
        return _die_by_sigint()
    return 0


def _die_by_sigint() -> int:
    """End the way a program killed by Ctrl-C does, so an enclosing shell loop stops (§9)."""
    if sys.platform == "win32":
        return 130
    sys.stdout.flush()
    sys.stderr.flush()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.raise_signal(signal.SIGINT)
    return 130  # not reached: the default handler ends the process
