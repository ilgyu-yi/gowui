"""Shared helpers for the session tests (SPEC §3, §4, §6.3, §7.7, §8.1).

The session is observed only through what it broadcasts (a :class:`Recorder` stands in for the
WebSocket hub, #7) and through its public surface: ``handle``, ``attach_frames``, ``snapshot``,
``restore``, ``resume`` and ``aclose``. Engine traffic is observed at the fake engine.

The API these tests pin (the implementer builds to it)::

    from gowui.session import EngineRequestError, EngineTarget, GameSession

    EngineTarget(protocol=, host=, port=, request_echo=, console=)   # what a resolver returns
    EngineRequestError(message)                                       # a resolver's refusal
    GameSession(resolve_engine, *, expose_address=True, broadcast=callable, preferences=None)
        preferences: dict | None    # None: the storage policy keeps none (SPEC §6.4, §8.5)
        resolve_engine(request: dict) -> EngineTarget   (sync; raises EngineRequestError)
        broadcast(frame: dict) -> None                  (sync, non-blocking; may raise)
        async handle(message)       -> None             (returns quickly; never raises)
        attach_frames()             -> list[dict]       (state, thumbnails, log_history, [analysis])
        snapshot()                  -> dict             (SPEC §8.1, version 1)
        restore(data: dict)         -> None             (raises ValueError when version != 1)
        async resume()              -> None             (replays the stored engine request)
        async aclose()              -> None             (idempotent)
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import random
import re
import socket
from typing import Any, Callable

import gowui
from gowui import coords
from gowui.game import Game
from gowui.session import EngineRequestError, EngineTarget, GameSession

#: Every "does not hang" check waits at most this long (seconds).
HANG = 5.0
LOOPBACK = "127.0.0.1"
#: A host name that must never reach a browser when addresses are hidden (§7.7).
SENTINEL_HOST = "gowui-secret.invalid"
_PACKAGE_DIR = os.path.dirname(os.path.abspath(gowui.__file__)) + os.sep


# -- the broadcast side --------------------------------------------------------------------
class Recorder:
    """The session's ``broadcast``: a sync callable that remembers every frame.

    Each frame is copied through JSON (``allow_nan=False``) at the moment it is broadcast, so a
    later mutation inside the session cannot rewrite history, and a frame that is not valid JSON
    is remembered in :attr:`bad`. With :attr:`fail` set, every call raises instead (the frame is
    lost), to check that a broken broadcast is contained.
    """

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.bad: list[str] = []
        self.fail = False
        self.calls = 0

    def __call__(self, frame: dict) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("the broadcast failed")
        try:
            copy = json.loads(json.dumps(frame, allow_nan=False))
        except (TypeError, ValueError):
            self.bad.append(repr(frame)[:300])
            copy = frame
        self.frames.append(copy)

    # -- reading ---------------------------------------------------------------------------
    def mark(self) -> int:
        """An index; ``of(kind, start=mark)`` sees only what was broadcast after it."""
        return len(self.frames)

    def of(self, kind: str, start: int = 0) -> list[dict]:
        return [f for f in self.frames[start:] if isinstance(f, dict) and f.get("type") == kind]

    def states(self, start: int = 0) -> list[dict]:
        return self.of("state", start)

    def state(self) -> dict:
        states = self.states()
        assert states, "no state frame was broadcast"
        return states[-1]

    def game(self) -> dict:
        return self.state()["game"]

    def errors(self, start: int = 0) -> list[str]:
        return [f.get("message", "") for f in self.of("error", start)]

    def log_texts(self, start: int = 0) -> list[str]:
        return [f["line"]["text"] for f in self.of("log", start)]

    # -- waiting ---------------------------------------------------------------------------
    async def wait(self, kind: str, predicate: Callable[[dict], bool] = lambda f: True,
                   start: int = 0, timeout: float = HANG) -> dict | None:
        """The first ``kind`` frame after ``start`` that satisfies ``predicate``, or None."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for frame in self.of(kind, start):
                if predicate(frame):
                    return frame
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(0.02)

    async def wait_state(self, predicate: Callable[[dict], bool], start: int = 0,
                         timeout: float = HANG) -> dict | None:
        return await self.wait("state", predicate, start, timeout)

    async def wait_error(self, start: int = 0, timeout: float = HANG) -> str | None:
        frame = await self.wait("error", start=start, timeout=timeout)
        return None if frame is None else frame.get("message", "")


# -- engine-address policies (in-test resolvers, §6.1 / §6.3) ---------------------------------
def typed_resolver(request: Any) -> EngineTarget:
    """The local policy: ``{protocol, host, port}``; the console is offered."""
    if not isinstance(request, dict):
        raise EngineRequestError("an engine request must be an object")
    fields = {k: v for k, v in request.items() if k != "type"}
    if set(fields) - {"protocol", "host", "port"}:
        raise EngineRequestError("a typed engine request has protocol, host and port only")
    protocol, host, port = fields.get("protocol"), fields.get("host"), fields.get("port")
    if protocol not in ("gtp", "analysis", "handol"):
        raise EngineRequestError("the protocol must be gtp, analysis or handol")
    if not isinstance(host, str) or not host:
        raise EngineRequestError("the host must be a non-empty string")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise EngineRequestError("the port must be a number from 1 to 65535")
    return EngineTarget(protocol=protocol, host=host, port=port,
                        request_echo={"protocol": protocol, "host": host, "port": port},
                        console=True)


class CatalogResolver:
    """The server policy: ``{engineId}`` naming an entry; addresses never reach the browser.

    ``entries`` maps an id to ``(protocol, port, console)``; every entry lives at :attr:`host`.
    Tests fill it in after starting their fake engines.
    """

    def __init__(self, host: str = LOOPBACK) -> None:
        self.host = host
        self.entries: dict[str, tuple[str, int, bool]] = {}

    def add(self, engine_id: str, protocol: str, port: int, console: bool = False) -> None:
        self.entries[engine_id] = (protocol, port, console)

    def __call__(self, request: Any) -> EngineTarget:
        if not isinstance(request, dict):
            raise EngineRequestError("an engine request must be an object")
        if "host" in request or "port" in request:
            raise EngineRequestError("choose an engine from the list")
        engine_id = request.get("engineId")
        if not isinstance(engine_id, str) or engine_id not in self.entries:
            raise EngineRequestError("no such engine in the catalog")
        protocol, port, console = self.entries[engine_id]
        return EngineTarget(protocol=protocol, host=self.host, port=port,
                            request_echo={"engineId": engine_id}, console=console)


# -- a session under test ---------------------------------------------------------------------
class Harness:
    """A :class:`GameSession` with its recorder and a few conveniences."""

    def __init__(self, resolver: Callable[[Any], EngineTarget] | None = None, *,
                 expose_address: bool = True, preferences: dict | None = None) -> None:
        self.recorder = Recorder()
        self.resolver = resolver or typed_resolver
        self.session = GameSession(self.resolver, expose_address=expose_address,
                                   broadcast=self.recorder, preferences=preferences)

    @property
    def rec(self) -> Recorder:
        return self.recorder

    async def send(self, message: Any) -> None:
        """Hand one browser message to the session; it must return (and not raise) quickly."""
        await asyncio.wait_for(self.session.handle(message), HANG)

    async def new_game(self, size: int = 9, komi: float | None = None,
                       rules: str = "japanese", handicap: int = 0) -> dict:
        start = self.rec.mark()
        await self.send({"type": "new_game", "size": size, "komi": komi, "rules": rules,
                         "handicap": handicap})
        frame = await self.rec.wait_state(lambda f: f["game"]["size"] == size
                                          and f["game"]["moveCount"] == 0, start)
        assert frame is not None, f"no state for the new game; errors: {self.rec.errors(start)}"
        return frame

    async def play(self, *vertices: str) -> dict:
        """Play vertices for the side to move, one after the other, as a human."""
        frame: dict | None = None
        for vertex in vertices:
            start = self.rec.mark()
            cursor = self.rec.game()["cursor"] if self.rec.states() else 0
            colour = self.rec.game()["toPlay"] if self.rec.states() else "black"
            await self.send({"type": "play", "color": colour, "vertex": vertex})
            frame = await self.rec.wait_state(
                lambda f, c=cursor: f["game"]["cursor"] == c + 1, start)
            assert frame is not None, f"{vertex} was not played; errors: {self.rec.errors(start)}"
        assert frame is not None
        return frame

    async def connect(self, request: dict) -> dict:
        """Connect and wait until ``state`` shows the engine connected."""
        start = self.rec.mark()
        await self.send({"type": "connect", **request})
        frame = await self.rec.wait_state(lambda f: f["engine"]["connected"], start)
        assert frame is not None, f"the engine did not connect; errors: {self.rec.errors(start)}"
        return frame

    async def connect_to(self, server, protocol: str | None = None) -> dict:
        """Connect through the typed policy to a fake engine."""
        return await self.connect({"protocol": protocol or server.protocol, "host": LOOPBACK,
                                   "port": server.port})

    async def wait_moves(self, count: int, start: int = 0, timeout: float = HANG) -> dict:
        frame = await self.rec.wait_state(lambda f: f["game"]["moveCount"] >= count, start,
                                          timeout)
        assert frame is not None, (f"the game did not reach {count} moves; "
                                   f"errors: {self.rec.errors(start)}")
        return frame

    async def fresh_state(self) -> dict:
        """Ask for a fresh ``state`` (§4.1 ``state``) and return it."""
        start = self.rec.mark()
        await self.send({"type": "state"})
        frame = await self.rec.wait_state(lambda f: True, start)
        assert frame is not None, "no state frame in answer to a state message"
        return frame

    async def aclose(self) -> None:
        await asyncio.wait_for(self.session.aclose(), HANG)


def move_list(state: dict) -> list[str]:
    """The vertices of a state's move list, in order."""
    return [m["vertex"] for m in state["game"]["moves"]]


def board_ids(state: dict) -> list[int]:
    return [b["id"] for b in state["boards"]]


def board_entry(state: dict, board_id: int) -> dict:
    return next(b for b in state["boards"] if b["id"] == board_id)


# -- engine-side observation ------------------------------------------------------------------
def gtp_count(server, name: str) -> int:
    """How many GTP commands named ``name`` the fake received."""
    from helpers import gtp_names
    return gtp_names(server).count(name)


def handol_requests(server, kind: str) -> list[dict]:
    """Every handol request the fake received: ``kind`` is ``"human"`` or ``"plain"``."""
    out = []
    for _, text in server.connection_requests:
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict) and (("human" in value) == (kind == "human")):
            out.append(value)
    return out


def gowui_tasks() -> list[asyncio.Task]:
    """Pending tasks running code of the ``gowui`` package (session or engine clients)."""
    found = []
    for task in asyncio.all_tasks():
        if task.done():
            continue
        coro = task.get_coro()
        code = getattr(coro, "cr_code", None) or getattr(coro, "gi_code", None)
        if code is not None and os.path.abspath(code.co_filename).startswith(_PACKAGE_DIR):
            found.append(task)
    return found


async def settle(seconds: float = 0.6) -> None:
    await asyncio.sleep(seconds)


def free_port() -> int:
    """A local port nothing listens on (bound, then released)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


def route_sentinel_host(monkeypatch) -> None:
    """Make :data:`SENTINEL_HOST` resolve to the loopback address (test-only name service)."""
    real = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if isinstance(name, str) and name.lower() == SENTINEL_HOST:
            host = LOOPBACK
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def leaks(frames: list, host: str, port: int) -> list[str]:
    """Every place in ``frames`` that carries ``host`` (any case) or ``port``."""
    port_text = re.compile(rf"(?<!\d){port}(?!\d)")
    found: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, str):
            if host.lower() in value.lower() or port_text.search(value):
                found.append(f"{path}: {value[:200]!r}")
        elif isinstance(value, bool):
            return
        elif isinstance(value, int):
            if value == port:
                found.append(f"{path}: {value}")
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(key, f"{path}.<key>")
                walk(item, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    for index, frame in enumerate(frames):
        walk(frame, f"frame{index}")
    return found


# -- games ---------------------------------------------------------------------------------------
@functools.lru_cache(maxsize=None)
def random_sgf(moves: int, size: int = 9, seed: int = 7) -> str:
    """The SGF of a legal game of exactly ``moves`` moves that is not over and whose last move is
    a stone (so an engine move can follow; §1.3 game over is two passes)."""
    for attempt in range(seed, seed + 50):
        rng = random.Random(attempt)
        game = Game(size)
        ok = True
        while game.move_count < moves:
            colour = game.to_play
            point = None
            for _ in range(40):
                candidate = (rng.randrange(size), rng.randrange(size))
                if game.legal_error(colour, candidate) is None:
                    point = candidate
                    break
            if point is None:
                legal = game.legal_moves(colour)
                point = rng.choice(legal) if legal else None
            last_was_pass = bool(game.moves) and game.moves[-1].point is None
            if point is None and last_was_pass:
                ok = False
                break
            game.play(colour, point)
        if ok and game.moves and game.moves[-1].point is not None and not game.is_game_over():
            return game.to_sgf()
    raise AssertionError("could not build a random game for the test")


def sgf_of(size: int, *vertices: str, result: str = "") -> str:
    """An SGF with the given moves (alternating from Black) and an optional ``RE``."""
    game = Game(size)
    for vertex in vertices:
        game.play(game.to_play, coords.from_gtp(vertex, size))
    game.result = result
    return game.to_sgf()
