"""Shared helpers for the engine tests (SPEC §2): positions, request logs, waiting, raw TCP."""

from __future__ import annotations

import asyncio
import json

from gowui import coords
from gowui.board import BLACK, WHITE
from gowui.engine import Position
from gowui.game import Game

#: Every "does not hang" check waits at most this long (seconds).
HANG = 5.0


def color_letter(color: int) -> str:
    return "B" if color == BLACK else "W"


def position_from(game: Game) -> Position:
    """The position at the game's cursor, as an engine client receives it."""
    size = game.size
    return Position(
        size=size,
        komi=game.komi,
        rules=game.rules.katago,
        initial_stones=[[color_letter(c), coords.to_gtp((x, y), size)]
                        for c, x, y in game.setup_stones],
        moves=[[color_letter(m.color), coords.to_gtp(m.point, size)]
               for m in game.moves[: game.cursor]],
        first_player=color_letter(game.first_player),
    )


def play(game: Game, *vertices: str) -> Game:
    """Play vertices alternately, starting with the side to move."""
    for vertex in vertices:
        game.play(game.to_play, coords.from_gtp(vertex, game.size))
    return game


def game_with(size: int = 9, *vertices: str, **kwargs) -> Game:
    return play(Game(size, **kwargs), *vertices)


def color_of(letter: str) -> int:
    return BLACK if letter.upper().startswith("B") else WHITE


# -- request logs -----------------------------------------------------------------
def gtp_commands(server) -> list[str]:
    """Every non-blank GTP command the fake received, without its numeric id."""
    return strip_ids(server.requests)


def strip_ids(lines) -> list[str]:
    """Non-blank GTP command lines without their numeric ids."""
    out = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        head, _, rest = text.partition(" ")
        out.append(rest.strip() if head.isdigit() else text)
    return out


def gtp_names(server) -> list[str]:
    return [c.split()[0] for c in gtp_commands(server)]


def queries(server) -> list[dict]:
    """Every JSON object the fake analysis engine received."""
    out = []
    for line in server.requests:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def position_queries(server) -> list[dict]:
    return [q for q in queries(server) if "action" not in q]


# -- waiting ---------------------------------------------------------------------
async def wait_for(predicate, timeout: float = HANG) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


class Reports:
    """An analysis callback that remembers every report."""

    def __init__(self) -> None:
        self.items: list = []

    def __call__(self, analysis) -> None:
        self.items.append(analysis)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index):
        return self.items[index]

    async def at_least(self, count: int, timeout: float = HANG) -> bool:
        return await wait_for(lambda: len(self.items) >= count, timeout)


class Log:
    """A traffic-log callback: ``log(direction, text)``."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    def __call__(self, direction: str, text: str) -> None:
        self.entries.append((direction, text))

    @property
    def notes(self) -> list[str]:
        return [text for _, text in self.entries if text.startswith("#")]


class Disconnects:
    """An ``on_disconnect`` callback that counts its calls."""

    def __init__(self) -> None:
        self.errors: list = []

    def __call__(self, error) -> None:
        self.errors.append(error)

    async def fired(self, timeout: float = HANG) -> bool:
        return await wait_for(lambda: bool(self.errors), timeout)


# -- raw TCP (tests of the fake engine itself) --------------------------------------
class RawClient:
    """A bare line client, so the fake engine is tested without the gowui clients."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._id = 0

    @classmethod
    async def open(cls, port: int, host: str = "127.0.0.1") -> "RawClient":
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=16 * 1024 * 1024), HANG)
        return cls(reader, writer)

    async def send(self, line: str) -> None:
        self.writer.write((line + "\n").encode())
        await self.writer.drain()

    async def line(self) -> str:
        raw = await asyncio.wait_for(self.reader.readline(), HANG)
        return raw.decode(errors="replace").rstrip("\r\n")

    async def gtp(self, command: str) -> tuple[bool, str]:
        """Send one GTP command; return (accepted, payload) of its reply."""
        self._id += 1
        await self.send(f"{self._id} {command}")
        head = await self.line()
        while head.strip() == "":
            head = await self.line()
        ok = head.startswith("=")
        body = head[1:].lstrip("0123456789").strip()
        lines = [body] if body else []
        while True:
            more = await self.line()
            if more == "":
                break
            lines.append(more)
        return ok, "\n".join(lines)

    async def json(self, payload: dict) -> None:
        await self.send(json.dumps(payload))

    async def json_line(self) -> dict:
        return json.loads(await self.line())

    async def at_eof(self) -> bool:
        """Whether the far end has closed the connection (within HANG seconds)."""
        try:
            while True:
                raw = await asyncio.wait_for(self.reader.readline(), HANG)
                if not raw:
                    return True
        except (ConnectionError, OSError):
            return True
        except asyncio.TimeoutError:
            return False

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass
