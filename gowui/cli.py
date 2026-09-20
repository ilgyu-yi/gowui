"""The command line (SPEC §9): the only place that chooses a launch mode and builds its policy
bundle. ``gowui`` and ``gowui local`` run local mode; ``gowui serve`` runs server mode, and
``gowui user`` manages its password accounts.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import ipaddress
import logging
import os
import signal
import sqlite3
import sys
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI

from .app import create_app
from .engine import PROTOCOLS
from .local_mode import (JsonFileStorage, LocalIdentity, MemoryStorage, StateFileError,
                         check_state_path, default_state_path, local_policies)
from .server_mode import ConfigError, ServerConfig, server_policies
from .store import Store, UserExists, valid_name, valid_password

__all__ = ["build_app", "build_config", "build_server_config", "main", "parse", "url_line",
           "user_command", "uvicorn_config"]

log = logging.getLogger("gowui")

#: The WebSocket message limit (§7.6).
WS_MAX_SIZE = 1024 * 1024
LOG_LEVELS = ("critical", "error", "warning", "info", "debug", "trace")
#: The subcommands.
COMMANDS = ("local", "serve", "user")
#: The server database when ``GOWUI_DB`` is unset (§10).
DEFAULT_DB = "./data/gowui.db"


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

    serve = commands.add_parser("serve", help="run for several signed-in accounts (GOWUI_*)")
    serve.add_argument("--host", default="0.0.0.0", help="address to bind (default 0.0.0.0)")
    serve.add_argument("--port", type=int, default=8080, help="port to bind; 0 picks a free one")
    serve.add_argument("--log-level", choices=LOG_LEVELS, default="info")

    user = commands.add_parser("user", help="manage the password accounts in GOWUI_DB")
    actions = user.add_subparsers(dest="action", required=True)
    for action in ("add", "passwd"):
        sub = actions.add_parser(action)
        sub.add_argument("name")
        sub.add_argument("--password-stdin", action="store_true",
                         help="read the password as one line from stdin")
    actions.add_parser("remove").add_argument("name")
    actions.add_parser("list")
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
    """Prints the URL line once the socket is bound, and remembers a SIGINT it handled.

    uvicorn re-raises the signal it captured when its run ends, but the handler it restores is
    asyncio's runner, which answers by cancelling the main task instead of raising
    ``KeyboardInterrupt``; the runner raises it back only while its own count still matches. The
    exit status must not ride on that relay (§9 "Stopping"), so the signal is recorded where it
    is handled.
    """

    def __init__(self, config: uvicorn.Config, host: str) -> None:
        super().__init__(config)
        self._host = host
        self.interrupted = False

    def handle_exit(self, sig: int, frame: Any = None) -> None:
        if sig == signal.SIGINT:
            self.interrupted = True
        super().handle_exit(sig, frame)

    async def startup(self, sockets: Any = None) -> None:
        await super().startup(sockets=sockets)
        if self.started and self.servers and self.servers[0].sockets:
            port = self.servers[0].sockets[0].getsockname()[1]
            print(url_line(self._host, port), flush=True)


def _fail(message: str, status: int) -> int:
    print(f"gowui: {message}", file=sys.stderr, flush=True)
    return status


def _read_password(args: argparse.Namespace) -> str:
    """One stdin line without its line ending, or two prompts that must match (§9)."""
    if args.password_stdin:
        line = sys.stdin.readline()
        return line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith("\n") else line
    first = getpass.getpass("Password: ")
    if getpass.getpass("Password again: ") != first:
        raise ValueError("the passwords do not match")
    return first


def user_command(args: argparse.Namespace) -> int:
    """``gowui user add|passwd|remove|list``; reads only ``GOWUI_DB`` (§9)."""
    if args.action != "list" and not valid_name(args.name):
        return _fail("a name has 1 to 64 printable characters without surrounding spaces", 1)
    password = ""
    if args.action in ("add", "passwd"):
        try:
            password = _read_password(args)
        except ValueError as exc:
            return _fail(str(exc), 1)
        if not valid_password(password):
            return _fail("a password has 8 to 256 characters", 1)
    path = Path(os.environ.get("GOWUI_DB") or DEFAULT_DB)
    if args.action == "list" and not path.exists():
        return 0  # a database that is not there is an empty one; listing never creates it (§9)
    try:
        store = Store(path)
    except (OSError, sqlite3.Error) as exc:
        return _fail(f"could not open the database {path}: {exc}", 1)
    try:
        if args.action == "list":
            for name in store.list_users():
                print(name)
            return 0
        if args.action == "add":
            try:
                store.add_user(args.name, password)
            except UserExists:
                return _fail(f"the account {args.name!r} already exists", 1)
        elif args.action == "passwd":
            if not store.set_password(args.name, password):
                return _fail(f"there is no account {args.name!r}", 1)
        elif not store.remove_user(args.name):
            return _fail(f"there is no account {args.name!r}", 1)
        print("ok")
        return 0
    finally:
        store.close()


def build_server_config(args: argparse.Namespace, env: Any = None) -> tuple[uvicorn.Config, Store]:
    """Server mode from ``GOWUI_*`` (§10): raises :class:`ConfigError` before anything is opened
    or bound (§7.9)."""
    config = ServerConfig.from_env(os.environ if env is None else env)
    if not config.engines:
        log.warning("gowui: GOWUI_ENGINES is empty, so there is no engine to connect to")
    store = Store(config.db)
    app = create_app(server_policies(config, store))
    return uvicorn_config(app, host=args.host, port=args.port, log_level=args.log_level), store


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    if args.command == "user":
        return user_command(args)
    store = None
    if args.command == "serve":
        try:
            config, store = build_server_config(args)
        except ConfigError as exc:
            return _fail(str(exc), 2)  # fail closed, before binding (§7.9)
    else:
        try:
            config = build_config(args)
        except StateFileError as exc:
            _parser().error(str(exc))  # a usage error: exits with status 2 (§9)
    server = _Server(config, args.host)
    interrupted = False
    try:
        server.run()
    except KeyboardInterrupt:  # Ctrl-C after a clean shutdown (§9 "Stopping")
        interrupted = True
    except asyncio.CancelledError:  # the same Ctrl-C, relayed as a cancellation
        if not server.interrupted:
            raise
        interrupted = True
    if store is not None:
        store.close()
    if interrupted or server.interrupted:
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
