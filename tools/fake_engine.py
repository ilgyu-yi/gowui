"""A stand-in engine speaking each registered protocol over real TCP (SPEC §2.6).

It plays legal but weak moves and invents plausible analysis, which is enough to exercise the
clients and the GUI without KataGo, a GPU or a model file. It is both an importable module (tests
start fake engines in-process with :func:`start_fake_engine`, switching engine features off or
injecting faults through :class:`FakeOptions`) and a command-line program::

    python tools/fake_engine.py --protocol gtp --port 6363
    python tools/fake_engine.py --protocol analysis --port 0   # prints "listening on 127.0.0.1:<port>"
    python tools/fake_engine.py --protocol handol --port 0
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gowui import coords  # noqa: E402
from gowui.board import BLACK, WHITE, color_from_name, opponent  # noqa: E402
from gowui.game import Game  # noqa: E402
from gowui.rules import RULE_SETS  # noqa: E402

MIB = 1024 * 1024
GTP_COMMANDS = [
    "protocol_version", "name", "version", "known_command", "list_commands", "quit",
    "boardsize", "clear_board", "komi", "play", "genmove", "undo", "showboard",
    "final_score", "kata-set-rules", "kata-set-param", "kata-analyze",
    "kata-genmove_analyze", "lz-analyze",
]
_KATA_ONLY = {"kata-analyze", "kata-genmove_analyze"}
_STREAMS = {"kata-analyze", "kata-genmove_analyze", "lz-analyze"}
_COLORS = ("b", "w", "black", "white")
#: Placeholder turned into a bare overflowing number in the JSON text.
_OVERFLOW = "__overflow_1e999__"


@dataclass
class FakeOptions:
    """Engine features to switch off and faults to inject."""

    #: False acts like a pre-rootInfo KataGo: ``rootInfo`` in an analyze command is refused.
    root_info: bool = True
    #: False refuses any analyze command carrying the ``ownership`` key (true or false).
    ownership: bool = True
    #: False: no kata-analyze / kata-genmove_analyze (lz-analyze only).
    kata: bool = True
    #: Replaces the ``list_commands`` answer.
    list_commands: list[str] | None = None
    #: Seconds to hold the reply, per GTP command name.
    delay: dict[str, float] = field(default_factory=dict)
    #: Close the connection on receiving this command (``"query"``: any analysis position query).
    hangup_on: str | None = None
    #: Close the connection after this many analysis reports.
    hangup_after_reports: int | None = None
    #: A line sent first in every analysis stream (``%ID%`` becomes the query id).
    emit_line: str | None = None
    #: Answer this command (``"query"`` for analysis) with a line over 1 MiB.
    overlong_line: str | None = None
    #: ``(command, mib)``: answer with a multi-line reply of about ``mib`` MiB.
    big_reply: tuple[str, float] | None = None
    #: Reports carry NaN, infinity and an overflowing ``1e999``.
    nonfinite: bool = False
    #: Keep streaming after an interrupt / terminate.
    ignore_interrupt: bool = False
    #: Payload overrides per GTP command name.
    replies: dict[str, str] = field(default_factory=dict)
    #: GTP command names answered with ``?``.
    reject: list[str] = field(default_factory=list)
    #: Answer this GTP command with another command's id.
    wrong_id_on: str | None = None
    #: Analysis: seconds to hold back each query, ``query_delay(query) -> float``.
    query_delay: Callable[[dict], float] | None = None
    #: Stop reading after the handshake (GTP: ``list_commands``; analysis: ``query_version``),
    #: like a wedged engine that no longer drains its input.
    never_read: bool = False
    #: GTP: answer every command without echoing its id (``=`` / ``?`` alone).
    no_reply_id: bool = False
    #: GTP: answer these commands with this id instead of theirs (``%ID%`` becomes their id).
    reply_id: dict[str, str] = field(default_factory=dict)
    #: GTP ``(every, line)``: while a delayed reply is held, write ``line`` every ``every`` seconds.
    stray: tuple[float, str] | None = None
    #: GTP: the message of a ``?`` answer for a command in ``reject``.
    reject_message: str = "rejected by the fake engine"
    # -- handol (``hangup_on``, ``wrong_id_on`` and ``query_delay`` apply too; a handol fault
    # names the requests it hits: ``"human"``, ``"plain"`` or ``"query"`` for both) --
    #: Handol: close a connection that sent nothing for this many seconds, silently.
    idle_close: float | None = None
    #: Handol: answer these requests with an error reply.
    error_reply: str | None = None
    #: Handol: whether an error reply carries the request id.
    error_reply_id: bool = True
    #: Handol: how many times a fault fires in all (``None``: every time).
    fault_times: int | None = None
    #: Handol: put most of the mass of each distribution on an occupied point.
    illegal_point: bool = False
    #: Handol: plain answers carry no moves.
    empty_katago: bool = False
    #: Handol: every distribution entry has p 0.
    empty_distribution: bool = False
    #: Handol: answer with one policy fewer than the tuples sent.
    short_policies: bool = False
    #: Handol: add entries that are not on the board (and not pass) with the largest p.
    offboard_move: bool = False
    #: Handol: the three likeliest entries get p NaN, negative and overflowing (``1e999``).
    bad_p: bool = False


def _command_name(line: str) -> str:
    """The command name of a GTP line, with or without a numeric id."""
    parts = line.split()
    if parts and parts[0].isdigit():
        parts = parts[1:]
    return parts[0] if parts else ""


# -- invented analysis --------------------------------------------------------------------------
def _candidates(game: Game, color: int, count: int = 8) -> list[tuple[int, int]]:
    """Legal moves nearest the centre, so the fake engine looks vaguely sane."""
    legal = game.legal_moves(color)
    middle = (game.size - 1) / 2
    rng = random.Random(len(game.moves) * 7919 + color)
    scored = sorted(legal, key=lambda p: abs(p[0] - middle) + abs(p[1] - middle) + rng.random() * 3)
    return scored[:count]


def _move_infos(game: Game, color: int, visits: int) -> list[dict]:
    moves = _candidates(game, color)
    total = max(1, visits)
    norm = sum(math.exp(-i * 0.6) for i in range(len(moves))) or 1.0
    infos = []
    for order, point in enumerate(moves):
        share = math.exp(-order * 0.6)
        winrate = min(0.99, max(0.01, 0.5 + 0.06 * math.exp(-order * 0.5) - 0.002 * len(game.moves)))
        infos.append({
            "move": coords.to_gtp(point, game.size),
            "visits": max(1, int(total * share / 2.2)),
            "winrate": round(winrate, 4),
            "scoreLead": round(2.5 * share - 0.5, 2),
            "scoreMean": round(2.5 * share - 0.5, 2),
            "scoreStdev": 18.0,
            "prior": round(share / norm, 4),
            "lcb": round(min(0.99, max(0.01, winrate - 0.02)), 4),
            "utility": round(0.2 * share, 4),
            "utilityLcb": round(0.18 * share, 4),
            "order": order,
            "pv": [coords.to_gtp(p, game.size) for p in moves[order:order + 4]],
        })
    return infos


def _ownership(game: Game, black_view: bool) -> list[float]:
    """A crude influence map, from the side to move's view as KataGo reports it."""
    size = game.size
    board = game.board
    stones = [(x, y, board.get(x, y)) for y in range(size) for x in range(size) if board.get(x, y)]
    values = []
    for y in range(size):
        for x in range(size):
            total = sum((1.0 if c == BLACK else -1.0) / (1.0 + (sx - x) ** 2 + (sy - y) ** 2)
                        for sx, sy, c in stones)
            value = math.tanh(total * 1.5)
            values.append(round(value if black_view else -value, 4))
    return values


def _policy(game: Game, color: int) -> list[float]:
    size = game.size
    policy = [-1.0] * (size * size + 1)
    moves = _candidates(game, color, count=size * size)
    weights = [math.exp(-i * 0.25) for i in range(len(moves))]
    total = sum(weights) or 1.0
    for (x, y), weight in zip(moves, weights):
        policy[y * size + x] = round(weight / total, 6)
    policy[-1] = 0.001
    return policy


def _new_game(size: int, komi: float, rules: str) -> Game:
    game = Game(size, rules=rules if rules in RULE_SETS else "tromp-taylor")
    game.komi = komi
    return game


# -- GTP ----------------------------------------------------------------------------------------
class FakeGTPEngine:
    """One GTP engine process (one per connection)."""

    def __init__(self, options: FakeOptions, requests: list[str]) -> None:
        self.options = options
        self.requests = requests
        self.game = Game(19)
        self.reports = 0
        self._writer: asyncio.StreamWriter | None = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One pump feeds a queue, so the stream and the command loop never both read."""
        self._writer = writer
        lines: asyncio.Queue = asyncio.Queue()

        async def pump() -> None:
            while True:
                raw = await reader.readline()
                if not raw:
                    await lines.put(None)
                    return
                text = raw.decode(errors="replace").rstrip("\r\n")
                self.requests.append(text)
                await lines.put(text)
                if self.options.never_read and _command_name(text) == "list_commands":
                    writer.transport.pause_reading()  # the handshake is done: never read again
                    return

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                line = await lines.get()
                if line is None:
                    break
                line = line.strip()
                if not line:
                    continue
                command_id, _, rest = line.partition(" ")
                if not command_id.isdigit():
                    command_id, rest = "", line
                parts = rest.split()
                if not parts:
                    continue
                name, args = parts[0], parts[1:]
                if name == self.options.hangup_on:
                    break
                delay = self.options.delay.get(name)
                if delay:
                    await self._hold(delay)
                if name in _STREAMS and self._known(name) and name not in self.options.reject:
                    if not await self._stream(lines, command_id, name, args):
                        break
                    continue
                if not await self._answer(command_id, name, args):
                    break
        finally:
            pump_task.cancel()
            writer.close()

    def _known(self, name: str) -> bool:
        return self.options.kata or name not in _KATA_ONLY

    def _reply_id(self, command_id: str, name: str) -> str:
        """The id a reply echoes, after the id faults of :class:`FakeOptions`."""
        options = self.options
        if options.no_reply_id:
            return ""
        if name in options.reply_id:
            return options.reply_id[name].replace("%ID%", command_id)
        if name == options.wrong_id_on:
            return str(int(command_id or "0") + 1000)
        return command_id

    async def _hold(self, delay: float) -> None:
        """Hold a reply for ``delay`` seconds, writing stray lines meanwhile if asked to."""
        if not self.options.stray:
            await asyncio.sleep(delay)
            return
        every, text = self.options.stray
        loop = asyncio.get_running_loop()
        end = loop.time() + delay
        while True:
            left = end - loop.time()
            if left <= 0:
                return
            await asyncio.sleep(min(every, left))
            if loop.time() < end:
                await self._write(text + "\n")

    async def _write(self, text: str) -> None:
        assert self._writer is not None
        self._writer.write(text.encode())
        await self._writer.drain()

    async def _answer(self, command_id: str, name: str, args: list[str]) -> bool:
        """Answer one ordinary command; False when the connection should close."""
        options = self.options
        reply_id = self._reply_id(command_id, name)
        if name == options.overlong_line:
            await self._write(f"={reply_id} " + "x" * (MIB + 64) + "\n\n")
            return True
        if options.big_reply and name == options.big_reply[0]:
            chunk = "y" * (MIB // 2)
            count = max(1, int(round(options.big_reply[1] * 2)))
            await self._write(f"={reply_id} " + "\n".join([chunk] * count) + "\n\n")
            return True
        if name in options.reject:
            await self._write(f"?{reply_id} {options.reject_message}\n\n")
            return True
        if name in options.replies:
            await self._write(f"={reply_id} {options.replies[name]}\n\n")
            return True
        try:
            payload = self._dispatch(name, args)
        except Exception as exc:  # noqa: BLE001 - report as a GTP error
            await self._write(f"?{reply_id} {exc}\n\n")
            return True
        if payload is None:
            await self._write(f"={reply_id}\n\n")
            return False
        await self._write(f"={reply_id} {payload}\n\n")
        return True

    def _dispatch(self, name: str, args: list[str]) -> str | None:
        if not self._known(name) or name in _STREAMS:
            raise ValueError("unknown command")
        if name == "quit":
            return None
        if name == "protocol_version":
            return "2"
        if name == "name":
            return "FakeKataGo"
        if name == "version":
            return "0.0-fake"
        if name == "list_commands":
            listed = self.options.list_commands
            if listed is None:
                listed = [c for c in GTP_COMMANDS if self._known(c)]
            return "\n".join(listed)
        if name == "known_command":
            return "true" if args and args[0] in GTP_COMMANDS and self._known(args[0]) else "false"
        if name == "boardsize":
            self.game = _new_game(int(args[0]), self.game.komi, self.game.rules.name)
            return ""
        if name == "clear_board":
            self.game = _new_game(self.game.size, self.game.komi, self.game.rules.name)
            return ""
        if name == "komi":
            komi = float(args[0])
            if not math.isfinite(komi):
                raise ValueError("bad komi")
            self.game.komi = komi
            return ""
        if name == "kata-set-rules":
            if args and args[0] in RULE_SETS:
                self.game.rules = RULE_SETS[args[0]]
            return ""
        if name == "kata-set-param":
            return ""
        if name == "play":
            self.game.play(color_from_name(args[0]), coords.from_gtp(args[1], self.game.size))
            return ""
        if name == "undo":
            if self.game.undo() is None:
                raise ValueError("cannot undo")
            return ""
        if name == "showboard":
            return ""
        if name == "final_score":
            return "B+0.5"
        if name == "genmove":
            return self._genmove(color_from_name(args[0]))
        raise ValueError("unknown command")

    def _genmove(self, color: int) -> str:
        moves = _candidates(self.game, color, count=3)
        if not moves:
            self.game.play(color, None)
            return "pass"
        choice = random.choice(moves)
        self.game.play(color, choice)
        return coords.to_gtp(choice, self.game.size)

    def _report(self, color: int, visits: int, name: str, ownership: bool, root: bool) -> str:
        infos = _move_infos(self.game, color, visits)
        if not infos:
            return ""
        chunks = []
        if name == "lz-analyze":
            for info in infos:
                chunks.append(f"info move {info['move']} visits {info['visits']} "
                              f"winrate {int(info['winrate'] * 10000)} "
                              f"prior {int(info['prior'] * 10000)} "
                              f"lcb {int(info['lcb'] * 10000)} order {info['order']} "
                              f"pv {' '.join(info['pv'])}")
            return " ".join(chunks)
        if self.options.nonfinite:
            infos[0].update(winrate="nan", scoreLead="inf", scoreMean="1e999")
        for info in infos:
            fields = " ".join(f"{k} {v}" for k, v in info.items() if k != "pv")
            chunks.append(f"info {fields} pv {' '.join(info['pv'])}")
        best = infos[0]
        if root:
            chunks.append(f"rootInfo visits {visits} winrate {best['winrate']} "
                          f"scoreLead {best['scoreLead']} scoreMean {best['scoreMean']}")
        if ownership:
            values = [f"{v:g}" for v in _ownership(self.game, color == BLACK)]
            if self.options.nonfinite:
                values[0] = "nan"
            chunks.append("ownership " + " ".join(values))
        return " ".join(chunks)

    async def _stream(self, lines: asyncio.Queue, command_id: str, name: str,
                      args: list[str]) -> bool:
        """Emit reports until interrupted, the way KataGo does; False when hung up."""
        has_color = bool(args) and args[0].lower() in _COLORS
        color = color_from_name(args[0]) if has_color else self.game.to_play
        rest = args[1:] if has_color else args
        interval_cs = 50
        options: dict[str, str] = {}
        index = 0
        while index < len(rest):
            token = rest[index]
            if token.isdigit():
                interval_cs = int(token)
                index += 1
            elif token == "interval" and index + 1 < len(rest) and rest[index + 1].isdigit():
                interval_cs = int(rest[index + 1])
                index += 2
            elif index + 1 < len(rest):
                options[token] = rest[index + 1].lower()
                index += 2
            else:
                index += 1
        kata = name != "lz-analyze"
        reply_id = self._reply_id(command_id, name)
        if kata and "rootInfo" in options and not self.options.root_info:
            await self._write(f"?{reply_id} unknown analyze option: rootInfo\n\n")
            return True
        if kata and "ownership" in options and not self.options.ownership:
            await self._write(f"?{reply_id} unknown analyze option: ownership\n\n")
            return True
        want_ownership = kata and options.get("ownership") == "true"
        want_root = kata and options.get("rootInfo") == "true"
        await self._write(f"={reply_id}\n")

        async def emit(text: str) -> bool:
            await self._write(text + "\n")
            self.reports += 1
            limit = self.options.hangup_after_reports
            return not (limit is not None and self.reports >= limit)

        if self.options.emit_line is not None:
            if not await emit(self.options.emit_line.replace("%ID%", command_id)):
                return False
        visits = 0
        while True:
            visits += 120
            report = self._report(color, visits, name, want_ownership, want_root)
            if report and not await emit(report):
                return False
            if name == "kata-genmove_analyze" and visits >= 360:
                break
            try:
                interrupt = await asyncio.wait_for(lines.get(), timeout=max(0.02, interval_cs / 100))
            except asyncio.TimeoutError:
                continue
            if interrupt is None:
                await lines.put(None)  # EOF belongs to the command loop
                return True
            if self.options.ignore_interrupt:
                continue
            if interrupt.strip():
                # Any input interrupts an analysis; a real command still has to run.
                requeue = [interrupt]
                while not lines.empty():
                    requeue.append(lines.get_nowait())
                for item in requeue:
                    lines.put_nowait(item)
            break
        if name == "kata-genmove_analyze":
            await self._write(f"play {self._genmove(color)}\n")
        await self._write("\n")
        return True


# -- analysis -------------------------------------------------------------------------------------
class FakeAnalysisEngine:
    """One analysis-engine process (one per connection); every query runs as its own task."""

    def __init__(self, options: FakeOptions, requests: list[str]) -> None:
        self.options = options
        self.requests = requests
        self.reports = 0
        self._writer: asyncio.StreamWriter | None = None
        self._queries: dict[str, asyncio.Task] = {}
        self._write_lock = asyncio.Lock()
        self._closed = False

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        try:
            while not self._closed:
                raw = await reader.readline()
                if not raw:
                    break
                text = raw.decode(errors="replace").rstrip("\r\n")
                self.requests.append(text)
                if not text.strip():
                    continue
                try:
                    query = json.loads(text)
                except ValueError:
                    query = None
                if not isinstance(query, dict):
                    await self._send({"error": "could not parse the query as a JSON object"})
                    continue
                await self._handle_query(query)
                if self.options.never_read and query.get("action") == "query_version":
                    writer.transport.pause_reading()  # the handshake is done: never read again
                    await asyncio.Event().wait()
        finally:
            await self._hangup()

    async def _hangup(self) -> None:
        self._closed = True
        tasks = [t for t in self._queries.values() if t is not asyncio.current_task()]
        self._queries = {}
        for task in tasks:
            task.cancel()
        if self._writer is not None:
            self._writer.close()

    async def _send(self, payload: dict | str) -> None:
        if self._closed or self._writer is None:
            return
        text = payload if isinstance(payload, str) else json.dumps(payload)
        text = text.replace(f'"{_OVERFLOW}"', "1e999")
        async with self._write_lock:
            self._writer.write((text + "\n").encode())
            await self._writer.drain()

    async def _report(self, payload: dict | str) -> bool:
        """Send one report; False once the fake hung up."""
        await self._send(payload)
        self.reports += 1
        limit = self.options.hangup_after_reports
        if limit is not None and self.reports >= limit:
            await self._hangup()
            return False
        return not self._closed

    async def _handle_query(self, query: dict) -> None:
        query_id = str(query.get("id", ""))
        action = query.get("action")
        if action == "query_version":
            await self._send({"id": query_id, "version": "1.16.0", "git_hash": "fake"})
            return
        if action in ("terminate", "terminate_all"):
            target = query.get("terminateId")
            if not self.options.ignore_interrupt:
                victims = list(self._queries) if action == "terminate_all" else [str(target)]
                for victim in victims:
                    task = self._queries.pop(victim, None)
                    if task is not None:
                        task.cancel()
            await self._send({"id": query_id, "action": action, "terminateId": target})
            return
        if action is not None:
            await self._send({"id": query_id, "action": action})
            return
        if self.options.hangup_on == "query":
            await self._hangup()
            return
        task = asyncio.create_task(self._run(query_id, query))
        self._queries[query_id] = task
        task.add_done_callback(lambda t, q=query_id: self._queries.get(q) is t
                               and self._queries.pop(q, None))

    async def _run(self, query_id: str, query: dict) -> None:
        with contextlib.suppress(asyncio.CancelledError, ConnectionError, OSError):
            if self.options.query_delay is not None:
                delay = self.options.query_delay(query)
                if delay:
                    await asyncio.sleep(delay)
            if self.options.overlong_line == "query":
                await self._send(json.dumps({"id": query_id, "pad": "x" * (MIB + 64)}))
                return
            try:
                game, color = self._position(query)
            except Exception as exc:  # noqa: BLE001 - an error for this query only
                await self._send({"id": query_id, "error": f"illegal query: {exc}"})
                return
            if self.options.emit_line is not None:
                if not await self._report(self.options.emit_line.replace("%ID%", query_id)):
                    return
            max_visits = max(1, int(query.get("maxVisits", 500)))
            every = query.get("reportDuringSearchEvery")
            steps = 4 if every else 0
            for step in range(steps):
                visits = max(1, max_visits * (step + 1) // (steps + 1))
                if not await self._report(self._result(query_id, query, game, color, visits, True)):
                    return
                await asyncio.sleep(max(0.02, float(every)))
            if not steps:
                await asyncio.sleep(0.02)
            await self._report(self._result(query_id, query, game, color, max_visits, False))

    @staticmethod
    def _position(query: dict) -> tuple[Game, int]:
        size = int(query.get("boardXSize", 19))
        if size != int(query.get("boardYSize", size)):
            raise ValueError("only square boards")
        game = _new_game(size, float(query.get("komi", 7.5)), str(query.get("rules", "")))
        for color, vertex in query.get("initialStones", []):
            game.play(color_from_name(color), coords.from_gtp(vertex, size))
        initial = str(query.get("initialPlayer", "B")).upper()
        to_play = WHITE if initial.startswith("W") else BLACK
        for color, vertex in query.get("moves", []):
            played = color_from_name(color)
            game.play(played, coords.from_gtp(vertex, size))
            to_play = opponent(played)
        return game, to_play

    def _result(self, query_id: str, query: dict, game: Game, color: int, visits: int,
                during: bool) -> dict:
        infos = _move_infos(game, color, visits)
        best = infos[0] if infos else {"winrate": 0.5, "scoreLead": 0.0}
        result: dict[str, Any] = {
            "id": query_id,
            "turnNumber": len(query.get("moves", [])),
            "isDuringSearch": during,
            "moveInfos": infos,
            "rootInfo": {"visits": visits, "winrate": best["winrate"],
                         "scoreLead": best["scoreLead"], "scoreMean": best["scoreLead"],
                         "currentPlayer": "B" if color == BLACK else "W"},
        }
        if query.get("includePolicy"):
            result["policy"] = _policy(game, color)
        if query.get("includeOwnership"):
            result["ownership"] = _ownership(game, color == BLACK)
        if self.options.nonfinite:
            if infos:
                infos[0].update(winrate=math.nan, scoreLead=math.inf, scoreMean=_OVERFLOW)
            if result.get("ownership"):
                result["ownership"][0] = math.nan
            if result.get("policy"):
                result["policy"][0] = math.nan
        return result


# -- handol-mux ---------------------------------------------------------------------------------
_HANDOL_POSITION = {"boardXSize", "boardYSize", "komi", "rules", "initialStones", "moves"}
_HANDOL_HUMAN_KEYS = _HANDOL_POSITION | {"id", "human"}
_HANDOL_PLAIN_KEYS = _HANDOL_POSITION | {"id", "maxVisits", "includeOwnership"}


def _tuple_number(tuple_: dict, name: str, default: float) -> float:
    value = tuple_.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    return number if math.isfinite(number) else default


def _handol_refusal(request: dict) -> str | None:
    """Why the surface refuses ``request`` (its own small checks), or ``None``."""
    if "action" in request:
        return "action verbs are not supported"
    for name in ("initialPlayer", "analyzeTurns"):
        if name in request:
            return f"{name} is not accepted: the side to move is inferred"
    if "human" in request:
        if "maxVisits" in request:
            return "maxVisits is not accepted next to a human block: use human.search.visits"
        allowed = _HANDOL_HUMAN_KEYS
    else:
        allowed = _HANDOL_PLAIN_KEYS
    unknown = sorted(set(request) - allowed)
    if unknown:
        return f"unknown top-level key {unknown[0]!r}"
    if "human" not in request:
        visits = request.get("maxVisits")
        if isinstance(visits, bool) or not isinstance(visits, int) or visits < 1:
            return "maxVisits must be a positive integer"
        return None
    human = request["human"]
    if not isinstance(human, dict):
        return "human must be an object"
    if not isinstance(human.get("profile"), str) or not human["profile"]:
        return "human.profile must be a non-empty string"
    policies = human.get("policies")
    if not isinstance(policies, list) or not policies or not all(isinstance(p, dict)
                                                                  for p in policies):
        return "human.policies must be a non-empty list of objects"
    search = human.get("search")
    if search is not None:
        visits = search.get("visits") if isinstance(search, dict) else None
        if isinstance(visits, bool) or not isinstance(visits, int) or visits < 1:
            return "human.search.visits must be a positive integer"
    if search is None and any(p.get("lambda_utility") is not None for p in policies):
        return "human.policies declares lambda_utility but human.search is absent"
    return None


def _handol_position(request: dict) -> tuple[Game, int]:
    """The position of a request, and who KataGo puts on move (no ``initialPlayer``)."""
    size = request["boardXSize"]
    if isinstance(size, bool) or not isinstance(size, int) or size != request["boardYSize"]:
        raise ValueError("only square boards")
    game = _new_game(size, float(request["komi"]), str(request["rules"]))
    stones = request["initialStones"]
    for color, vertex in stones:
        game.play(color_from_name(color), coords.from_gtp(vertex, size))
    colours = [str(color).upper()[:1] for color, _ in stones]
    to_play = WHITE if colours.count("B") >= 2 and "W" not in colours else BLACK
    for color, vertex in request["moves"]:
        played = color_from_name(color)
        game.play(played, coords.from_gtp(vertex, size))
        to_play = opponent(played)
    return game, to_play


def _handol_distribution(game: Game, color: int, tuple_: dict,
                         options: FakeOptions) -> list[dict]:
    """A deterministic, tuple-dependent distribution over the legal moves plus pass: listed in
    board order, with a cut tail of ``p: 0`` points."""
    size = game.size
    ranked = _candidates(game, color, count=size * size)
    decay = ((0.35 + 0.4 * _tuple_number(tuple_, "distance_slope", 0.0)
              + 0.1 * _tuple_number(tuple_, "lambda_utility", 0.0))
             / max(0.05, _tuple_number(tuple_, "temperature", 1.0)))
    cut = min(24, max(3, len(ranked) // 2))
    weights = {point: (math.exp(-i * decay) if i < cut else 0.0) for i, point in enumerate(ranked)}
    pass_weight = math.exp(-3 * decay)
    total = sum(weights.values()) + pass_weight
    min_p = _tuple_number(tuple_, "min_p", 0.0)

    def share(weight: float) -> float:
        p = round(weight / total, 6)
        return p if p >= min_p else 0.0

    entries: list[dict[str, Any]] = [
        {"move": coords.to_gtp(point, size), "p": share(weights[point])}
        for point in sorted(weights, key=lambda p: (p[1], p[0]))]
    entries.append({"move": "pass", "p": share(pass_weight)})
    if options.empty_distribution:
        for entry in entries:
            entry["p"] = 0.0
    board = game.board
    occupied = [(x, y) for y in range(size) for x in range(size) if board.get(x, y)]
    if options.illegal_point and occupied:
        for entry in entries:
            entry["p"] = round(entry["p"] * 0.2, 6)
        entries.insert(0, {"move": coords.to_gtp(occupied[0], size), "p": 0.8})
    if options.bad_p:
        likely = sorted((e for e in entries if e["move"] != "pass"), key=lambda e: -e["p"])
        for entry, bad in zip(likely, (math.nan, -0.5, _OVERFLOW)):
            entry["p"] = bad
    if options.offboard_move:
        column = coords.GTP_COLUMNS[size] if size < len(coords.GTP_COLUMNS) else "Z"
        entries[:0] = [{"move": f"{column}1", "p": 0.5}, {"move": f"A{size + 1}", "p": 0.5},
                       {"move": "nonsense", "p": 0.5}, {"move": 7, "p": 0.5}]
    return entries


def _handol_plain(game: Game, color: int, request: dict, options: FakeOptions) -> dict:
    """KataGo's own answer to a plain query, from White's view (as handol-mux reports it); the
    moves are listed out of order and pass is spelled ``PASS``."""
    size = game.size
    board = game.board
    stones = [board.get(x, y) for y in range(size) for x in range(size)]
    black_lead = stones.count(BLACK) - stones.count(WHITE) - game.komi + 7.0
    mover_lead = black_lead if color == BLACK else -black_lead
    visits = request["maxVisits"]
    ranked: list[tuple[int, int] | None] = list(_candidates(game, color))
    ranked.append(None)
    infos = []
    for order, point in enumerate(ranked):
        lead = mover_lead - (0.4 * order if point is not None else 3.0)
        white_lead = -lead if color == BLACK else lead
        winrate = 1.0 / (1.0 + math.exp(-white_lead / 5.0))
        move = "PASS" if point is None else coords.to_gtp(point, size)
        infos.append({
            "move": move,
            "visits": max(1, visits >> (order + 1)),
            "winrate": round(winrate, 4),
            "scoreLead": round(white_lead, 2),
            "scoreMean": round(white_lead, 2),
            "scoreStdev": 12.0,
            "prior": round(math.exp(-order * 0.6) / 2.5, 4),
            "lcb": round(winrate - 0.02, 4),
            "utility": round(2 * winrate - 1, 4),
            "utilityLcb": round(2 * winrate - 1.04, 4),
            "order": order,
            "pv": [move] + [coords.to_gtp(p, size) for p in ranked[order + 1:order + 3]
                            if p is not None],
        })
    best = infos[0]
    result: dict[str, Any] = {
        "id": request.get("id"),
        "turnNumber": len(request["moves"]),
        "moveInfos": [] if options.empty_katago else list(reversed(infos)),
        "rootInfo": {"visits": visits, "winrate": best["winrate"],
                     "scoreLead": best["scoreLead"], "scoreMean": best["scoreMean"],
                     "currentPlayer": "B" if color == BLACK else "W"},
    }
    if request.get("includeOwnership"):
        result["ownership"] = _ownership(game, black_view=False)
    return result


class FakeHandolEngine:
    """One handol-mux surface connection: one answer line per request, strictly serial (the next
    request is taken only after the answer). A pump reads ahead, so pipelined lines are counted
    as in flight."""

    def __init__(self, options: FakeOptions, requests: list[str]) -> None:
        self.options = options
        self.requests = requests
        self.server: "FakeEngineServer | None" = None
        self.index = 0
        self.in_flight = 0

    def attach(self, server: "FakeEngineServer", index: int) -> None:
        self.server = server
        self.index = index

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        lines: asyncio.Queue = asyncio.Queue()
        server = self.server

        async def pump() -> None:
            while True:
                try:
                    raw = await reader.readline()
                except (ValueError, ConnectionError, OSError):
                    raw = b""
                if not raw:
                    await lines.put(None)
                    return
                text = raw.decode(errors="replace").rstrip("\r\n")
                self.requests.append(text)
                if not text.strip():
                    continue
                self.in_flight += 1
                if server is not None:
                    server.connection_requests.append((self.index, text))
                    server.max_in_flight = max(server.max_in_flight, self.in_flight)
                await lines.put(text)

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                try:
                    text = await asyncio.wait_for(lines.get(), self.options.idle_close)
                except asyncio.TimeoutError:
                    if server is not None:
                        server.idle_closed.append(self.index)
                    return  # the surface's silent idle close
                if text is None or not await self._answer(text, writer):
                    return
                self.in_flight -= 1
        finally:
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
            writer.close()

    def _fault(self, name: str, kind: str) -> bool:
        """Whether fault option ``name`` fires for a request of ``kind`` (counting it)."""
        target = getattr(self.options, name)
        if target is None or target not in (kind, "query"):
            return False
        counts = self.server.fault_counts if self.server is not None else {}
        limit = self.options.fault_times
        if limit is not None and counts.get(name, 0) >= limit:
            return False
        counts[name] = counts.get(name, 0) + 1
        return True

    async def _answer(self, text: str, writer: asyncio.StreamWriter) -> bool:
        """Answer one request; False when the connection is to be closed instead."""
        try:
            request = json.loads(text)
        except ValueError:
            request = None
        if not isinstance(request, dict):
            await self._send(writer, {"error": "could not parse the request as a JSON object"})
            return True
        kind = "human" if "human" in request else "plain"
        if self._fault("hangup_on", kind):
            return False
        if self.options.query_delay is not None:
            delay = self.options.query_delay(request)
            if delay:
                await asyncio.sleep(delay)
        request_id = request.get("id")
        if self._fault("error_reply", kind):
            reply: dict[str, Any] = {"error": f"the fake refused this {kind} request"}
            if self.options.error_reply_id:
                reply = {"id": request_id, **reply}
        else:
            reply = self._reply(request)
            if self._fault("wrong_id_on", kind):
                reply["id"] = f"{request_id}-other"
        await self._send(writer, reply)
        return True

    def _reply(self, request: dict) -> dict:
        request_id = request.get("id")
        refusal = _handol_refusal(request)
        if refusal is not None:
            return {"id": request_id, "error": refusal}
        try:
            game, color = _handol_position(request)
        except Exception as exc:  # noqa: BLE001 - an error for this request only
            return {"id": request_id, "error": f"illegal position: {exc}"}
        if "human" not in request:
            return _handol_plain(game, color, request, self.options)
        policies = request["human"]["policies"]
        answers = [{"params": tuple_,
                    "distribution": _handol_distribution(game, color, tuple_, self.options)}
                   for tuple_ in policies]
        if self.options.short_policies:
            answers = answers[:-1]
        return {"id": request_id, "policies": answers}

    async def _send(self, writer: asyncio.StreamWriter, payload: dict) -> None:
        text = json.dumps(payload).replace(f'"{_OVERFLOW}"', "1e999")
        if self.server is not None:
            self.server.replies.append((self.index, text))
        writer.write((text + "\n").encode())
        await writer.drain()


#: The registered fake protocols.
PROTOCOLS: dict[str, type] = {"gtp": FakeGTPEngine, "analysis": FakeAnalysisEngine,
                              "handol": FakeHandolEngine}

class FakeEngineServer:
    """A listening fake engine; every accepted connection gets a fresh engine."""

    def __init__(self, protocol: str, options: FakeOptions) -> None:
        self.protocol = protocol
        self.options = options
        self.host = ""
        self.port = 0
        #: Every line received on any connection, in order (blank lines included).
        self.requests: list[str] = []
        #: Handol: every non-blank line received, with the index of its connection (in accept
        #: order), and every answer line sent, likewise.
        self.connection_requests: list[tuple[int, str]] = []
        self.replies: list[tuple[int, str]] = []
        #: Handol: the connections closed for being idle.
        self.idle_closed: list[int] = []
        #: Handol: the most lines one connection had sent without an answer (pipelining counts).
        self.max_in_flight = 0
        #: Handol: how many times each fault option fired.
        self.fault_counts: dict[str, int] = {}
        self._next_index = 0
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._stopped = False

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._stopped:
            writer.close()
            return
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        engine = PROTOCOLS[self.protocol](self.options, self.requests)
        index, self._next_index = self._next_index, self._next_index + 1
        if hasattr(engine, "attach"):
            engine.attach(self, index)
        try:
            await engine.handle(reader, writer)
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            if task is not None:
                self._tasks.discard(task)

    @property
    def open_connections(self) -> int:
        """Connections accepted and not yet closed by either side."""
        return len(self._writers)

    async def start(self, host: str, port: int) -> None:
        self._server = await asyncio.start_server(self._on_client, host, port, limit=16 * MIB)
        self.host, self.port = self._server.sockets[0].getsockname()[:2]

    async def stop(self) -> None:
        """Stop listening and drop every connection; harmless to call twice."""
        if self._stopped:
            return
        self._stopped = True
        server = self._server
        # Let accepts already under way finish, so their transports get closed below instead of
        # being created on a closed server and left to the garbage collector.
        for _ in range(5):
            await asyncio.sleep(0)
        if server is not None:
            server.close()
            # A connection accepted but not yet handed to _on_client is only reachable here.
            if hasattr(server, "close_clients"):  # Python 3.13+
                server.close_clients()
        await asyncio.sleep(0)  # let just-accepted connections reach _on_client
        for writer in list(self._writers):
            writer.close()
        tasks = [t for t in self._tasks if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(server.wait_closed(), 2.0)


async def start_fake_engine(protocol: str, host: str = "127.0.0.1", port: int = 0,
                            **options: Any) -> FakeEngineServer:
    """Start a fake engine for ``protocol``; an unregistered protocol is a ValueError."""
    if protocol not in PROTOCOLS:
        raise ValueError(f"unknown fake protocol {protocol!r}; expected one of "
                         f"{', '.join(PROTOCOLS)}")
    server = FakeEngineServer(protocol, FakeOptions(**options))
    await server.start(host, port)
    return server


def _delay_arg(text: str) -> tuple[str, float]:
    name, sep, value = text.partition("=")
    try:
        seconds = float(value)
    except ValueError:
        seconds = math.nan
    if not sep or not name or not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError(f"expected COMMAND=SECONDS, got {text!r}")
    return name, seconds


async def _serve(protocol: str, host: str, port: int, **options: Any) -> None:
    server = await start_fake_engine(protocol, host, port, **options)
    print(f"listening on {server.host}:{server.port}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fake engine for gowui development and tests")
    parser.add_argument("--protocol", choices=sorted(PROTOCOLS), default="gtp")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6363)
    parser.add_argument("--delay", action="append", default=[], type=_delay_arg,
                        metavar="COMMAND=SECONDS",
                        help="hold each reply to this GTP command (repeatable)")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_serve(args.protocol, args.host, args.port, delay=dict(args.delay)))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
