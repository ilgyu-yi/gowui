"""Shared helpers for the web-app tests (SPEC §3.1, §4, §5, §6, §7.4–§7.6, §8, §9).

The app is exercised the way a browser meets it: one real uvicorn server on port 0, built through
the CLI's own config builder, an ``httpx.AsyncClient`` that sets the ``Host`` header explicitly,
and a ``websockets`` client that dials 127.0.0.1 while sending whatever ``Host`` the test names
(``connect("ws://evil.example:PORT/ws", host="127.0.0.1", port=PORT)``). Header shapes a client
library will not send (two ``Host`` headers, none at all) are tested by calling the guard with a
hand-built ASGI scope, plus one raw-socket HTTP/1.0 request against the real server.

The API these tests pin (the implementer builds to it)::

    from gowui.policies import Identity, Policies
    Identity(key, name="", source="none", logout_kind="", logout_url="")        # frozen
    Policies(identity, engines, storage, idle_release_seconds=None, allowed_hosts=None,
             allow_ip_literals=False, trusted_proxies=(), sign_in_url=None)    # frozen dataclass
        identity policy: identify(conn) -> Identity | None;
                         async login_page(request) / login(request) / logout(request) -> Response
        engines policy:  resolve(request) -> EngineTarget (raises EngineRequestError);
                         describe() -> dict; expose_address: bool
        storage policy:  load(key) -> dict | None; save(key, snapshot); set_aside(key, reason)

    from gowui.local_mode import (local_policies, LocalIdentity, TypedAddresses, JsonFileStorage,
                                  MemoryStorage, default_state_path, STATE_READ_CAP)
    local_policies(*, host="127.0.0.1", storage, engine_defaults=None) -> Policies
    TypedAddresses(defaults: dict)             # {"protocol", "host", "port"}
    JsonFileStorage(path, *, max_bytes=STATE_READ_CAP)
    MemoryStorage()
    default_state_path(platform: str, env: Mapping[str, str], home: Path) -> Path

    from gowui.spaces import SpaceRegistry, DEFAULT_QUEUE_SIZE, SAVE_INTERVAL
    SpaceRegistry(policies, *, save_interval=SAVE_INTERVAL, queue_size=DEFAULT_QUEUE_SIZE,
                  clock=time.monotonic)
        async get(identity) -> Space            # Space: .key, .session, .hub (hub.broadcast)
        async attach(identity, send, close) -> Tab   # send(text) / close(code) are async
        async detach(tab)
        async save_changed()                    # one autosave pass
        async release_idle()                    # one idle-release sweep against ``clock``
        async start()                           # the periodic autosave / idle task
        async aclose()                          # shutdown: save every space, await aclose()
        live -> Mapping[str, Space]             # the published spaces

    from gowui.guard import Guard               # Guard(asgi_app, policies): pure ASGI middleware
    from gowui.app import create_app            # create_app(policies, *, startup=()) -> FastAPI
        app.state.policies, app.state.registry; startup hooks are ``async hook(app)``
    from gowui.cli import main, parse, build_app, build_config, uvicorn_config, url_line
    parse(argv) -> argparse.Namespace           # .command == "local", .host, .port, ...
    build_app(args) -> FastAPI                  # local_policies from the flags + startup hook
    build_config(args) -> uvicorn.Config
    uvicorn_config(app, *, host, port, log_level="info") -> uvicorn.Config
    url_line(host, port) -> str                 # "gowui: http://<host>:<port>"
    main(argv=None) -> int
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from helpers import HANG, wait_for
from session_helpers import sgf_of

LOOPBACK = "127.0.0.1"
MIB = 1024 * 1024
CSP = "default-src 'self'; frame-ancestors 'none'"


# -- policies for the tests -----------------------------------------------------------------------
def local_bundle(storage=None, *, host: str = LOOPBACK, engine_defaults: dict | None = None):
    """The local policy bundle, in memory unless a storage is given."""
    from gowui.local_mode import MemoryStorage, local_policies

    return local_policies(host=host, storage=storage if storage is not None else MemoryStorage(),
                          engine_defaults=engine_defaults)


class NoIdentity:
    """An identity policy that identifies nobody (§6.2): every non-public route needs sign-in."""

    def identify(self, conn) -> None:
        return None

    async def login_page(self, request):
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("sign in")

    async def login(self, request):
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("signed in")

    async def logout(self, request):
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("signed out")


def without_identity(policies, **changes):
    """The same bundle with an identity policy that returns no identity."""
    return dataclasses.replace(policies, identity=NoIdentity(), **changes)


# -- snapshots -------------------------------------------------------------------------------------
def snapshot_with(*vertices: str, name: str = "study", size: int = 9, cursor: int | None = None,
                  connected: bool = False, request: dict | None = None) -> dict:
    """A version 1 snapshot (§8.1) with one board holding ``vertices``."""
    return {
        "version": 1,
        "activeBoard": 1,
        "boards": [{"id": 1, "name": name, "sgf": sgf_of(size, *vertices),
                    "cursor": len(vertices) if cursor is None else cursor,
                    "humanProfile": "preaz_1d", "humanPolicy": {}, "humanCompare": None}],
        "engine": {"connected": connected, "request": request, "maxVisits": 500,
                   "reportInterval": 0.4, "includeOwnership": False, "evalVisits": 0},
        "play": {"blackIsEngine": False, "whiteIsEngine": False, "blackStyle": "human",
                 "whiteStyle": "human", "analysisEnabled": False},
    }


# -- a running server ------------------------------------------------------------------------------
@dataclass
class Running:
    """A uvicorn server serving ``app`` on 127.0.0.1 and a free port."""

    app: Any
    server: Any
    task: asyncio.Task
    port: int

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    @property
    def host(self) -> str:
        """The ``Host`` header a same-origin browser sends."""
        return f"{LOOPBACK}:{self.port}"

    @property
    def origin(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    def client(self, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.url, timeout=HANG, trust_env=False, **kwargs)

    async def stop(self) -> None:
        if self.task.done():
            return
        self.server.should_exit = True
        await asyncio.wait_for(asyncio.shield(self.task), 20)


async def start_server(app, host: str = LOOPBACK) -> Running:
    """Serve ``app`` through the CLI's config builder on port 0 and wait until it listens."""
    import uvicorn

    from gowui.cli import uvicorn_config

    config = uvicorn_config(app, host=host, port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await wait_for(lambda: server.started or task.done(), 15)
    if task.done():
        task.result()
        raise AssertionError("the server exited during startup")
    assert server.started, "the server did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    return Running(app, server, task, port)


# -- WebSocket tabs --------------------------------------------------------------------------------
class Tab:
    """One browser tab's WebSocket: every frame it receives is remembered, in order."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.frames: list[dict] = []
        self.texts: list[str] = []
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        from websockets.exceptions import ConnectionClosed

        with contextlib.suppress(ConnectionClosed):
            async for text in self.ws:
                self.texts.append(text)
                try:
                    self.frames.append(json.loads(text))
                except ValueError:
                    self.frames.append({"type": "<not json>", "text": text[:200]})

    async def send(self, message: Any) -> None:
        await self.ws.send(message if isinstance(message, (str, bytes)) else json.dumps(message))

    def mark(self) -> int:
        return len(self.frames)

    def of(self, kind: str, start: int = 0) -> list[dict]:
        return [f for f in self.frames[start:] if f.get("type") == kind]

    def state(self) -> dict:
        states = self.of("state")
        assert states, "no state frame received"
        return states[-1]

    async def wait(self, kind: str, predicate: Callable[[dict], bool] = lambda f: True,
                   start: int = 0, timeout: float = HANG) -> dict | None:
        found: list[dict] = []

        def ready() -> bool:
            found[:] = [f for f in self.of(kind, start) if predicate(f)][:1]
            return bool(found)

        await wait_for(ready, timeout)
        return found[0] if found else None

    async def wait_state(self, predicate: Callable[[dict], bool] = lambda f: True,
                         start: int = 0, timeout: float = HANG) -> dict | None:
        return await self.wait("state", predicate, start, timeout)

    @property
    def open(self) -> bool:
        return not self._reader.done()

    async def close_code(self, timeout: float = HANG) -> int | None:
        """Wait until the server closes the socket; the close code it sent."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._reader), timeout)
        return self.ws.close_code

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.ws.close(), HANG)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._reader, HANG)


async def open_tab(running: Running, *, host: str | None = None, origin: str | None = None,
                   headers: list[tuple[str, str]] | None = None):
    """Open ``/ws`` sending ``Host: host`` (default: the same-origin host); the connection goes
    to 127.0.0.1 whatever the ``Host``. Returns a :class:`Tab`, or the HTTP status when the
    handshake was refused before it was accepted."""
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    uri = f"ws://{host or running.host}/ws"
    try:
        ws = await asyncio.wait_for(
            connect(uri, host=LOOPBACK, port=running.port, origin=origin,
                    additional_headers=headers, proxy=None, max_size=None,
                    open_timeout=HANG, close_timeout=1),
            HANG)
    except InvalidStatus as exc:
        return exc.response.status_code
    return Tab(ws)


async def refusal_code(running: Running, **kwargs) -> Any:
    """The close code of a handshake the server refuses (§4.3), or ``"http <status>"`` when it was
    refused before being accepted, or ``"open"`` when the tab stayed open and got a state."""
    tab = await open_tab(running, **kwargs)
    if isinstance(tab, int):
        return f"http {tab}"
    try:
        code = await tab.close_code(timeout=HANG)
        if code is None and tab.of("state"):
            return "open"
        return code
    finally:
        await tab.close()


# -- raw HTTP --------------------------------------------------------------------------------------
async def raw_http(port: int, data: bytes | list[bytes],
                   timeout: float = HANG) -> tuple[int, dict, bytes]:
    """Send ``data`` as-is (a list is sent piece by piece) and read the answer to EOF:
    (status, lower-cased headers, body). Reading runs alongside writing, so an answer the server
    sends before the whole request is written (a ``413``) is kept even if the server then drops
    the connection."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(LOOPBACK, port), timeout)
    received = bytearray()

    async def read_all() -> None:
        with contextlib.suppress(ConnectionError):
            while chunk := await reader.read(65536):
                received.extend(chunk)

    reading = asyncio.create_task(read_all())
    try:
        for piece in (data if isinstance(data, list) else [data]):
            if reading.done():
                break
            try:
                writer.write(piece)
                await writer.drain()
            except ConnectionError:
                break
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(reading), timeout)
        raw = bytes(received)
    finally:
        reading.cancel()
        with contextlib.suppress(BaseException):
            await reading
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1]) if lines and len(lines[0].split()) > 1 else 0
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers, body


# -- the guard, called directly --------------------------------------------------------------------
class Inner:
    """The app behind the guard in direct tests: answers 200 and remembers being reached."""

    def __init__(self) -> None:
        self.reached = 0

    async def __call__(self, scope, receive, send) -> None:
        self.reached += 1
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"inner"})
        elif scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 1000})


def scope_for(kind: str = "http", *, headers: list[tuple[str, str]], path: str = "/api/health",
              method: str = "GET", raw_path: bytes | None = None, client: str = LOOPBACK) -> dict:
    """A hand-built ASGI scope (HTTP/1.1 unless the test says otherwise)."""
    scope = {
        "type": kind,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http" if kind == "http" else "ws",
        "path": path,
        "raw_path": raw_path if raw_path is not None else path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode("latin-1"), v.encode("utf-8")) for k, v in headers],
        "client": (client, 50000),
        "server": (LOOPBACK, 8080),
    }
    if kind == "http":
        scope["method"] = method
    else:
        scope["subprotocols"] = []
    return scope


async def call_guard(policies, scope: dict, body: bytes = b"") -> tuple[list[dict], Inner]:
    """Run one request or handshake through ``Guard``; every ASGI message it sent, and the inner
    app (whose ``reached`` says whether the request got past the guard)."""
    from gowui.guard import Guard

    inner = Inner()
    guard = Guard(inner, policies)
    sent: list[dict] = []
    if scope["type"] == "http":
        incoming = [{"type": "http.request", "body": body, "more_body": False}]
    else:
        incoming = [{"type": "websocket.connect"}]

    async def receive():
        if incoming:
            return incoming.pop(0)
        await asyncio.sleep(HANG)
        return {"type": "http.disconnect"} if scope["type"] == "http" else \
            {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message)

    await asyncio.wait_for(guard(scope, receive, send), HANG)
    return sent, inner


def http_status(sent: list[dict]) -> int | None:
    starts = [m for m in sent if m["type"] == "http.response.start"]
    return starts[0]["status"] if starts else None


def ws_outcome(sent: list[dict]) -> Any:
    """``("accept", code)`` for a handshake accepted then closed, else what was sent."""
    kinds = [m["type"] for m in sent]
    close = next((m for m in sent if m["type"] == "websocket.close"), None)
    if kinds[:1] == ["websocket.accept"] and close is not None:
        return ("accept", close.get("code"))
    return tuple(kinds)
