"""The handol-mux human-policy client (SPEC §2.5).

The surface answers "where would a human of this profile play here?" as a move distribution: one
request line in, one answer line out, at most one query in flight per connection, the position
keys a closed set (no ``initialPlayer``: KataGo infers who is to move). A plain query (no
``human`` block) on a second connection to the same port brings winrates, scores and ownership,
reported from White's view.

Each connection is a :class:`_Channel`: under a lock it writes one line and reads one line with a
timeout. The surface closes idle connections silently, so a connection found closed, or closing
before it answers, is reopened and the request resent once (queries are reads).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import random
from dataclasses import dataclass
from typing import Any

from .. import coords
from ..board import color_from_name
from ..game import Game
from ..rules import DEFAULT_RULES, RULE_SETS
from .base import TEXT_LIMIT, AnalysisCallback, Engine, LogCallback, clip, color_letter, deliver
from .errors import ConnectionClosed, EngineError
from .human import MOVE_STYLES, check_policies, wire_tuple
from .transport import LineConnection
from .types import (Analysis, MoveInfo, Position, RootInfo, board_vertex, board_vertices,
                    clamp_turn, finite, finite_int, flip_rate, flip_score, point_values)

DEFAULT_PROFILE = "preaz_1d"
DEFAULT_MAX_VISITS = 100
DEFAULT_EVAL_VISITS = 200
#: Max visits and eval visits may be at most this (§7.6).
MAX_VISITS_LIMIT = 1_000_000
#: Candidates listed; the full distribution still fills ``policy``.
TOP_CANDIDATES = 20
_UNSET: Any = object()

__all__ = ["HandolEngine", "engine_to_play"]


def engine_to_play(position: Position) -> str:
    """Who KataGo puts on move when no ``initialPlayer`` is given: after moves, the other side of
    the last move; with none, White for a setup of two or more black stones and no white ones
    (a handicap), otherwise Black."""
    if position.moves:
        return "W" if str(position.moves[-1][0]).upper().startswith("B") else "B"
    colours = [str(stone[0]).upper()[:1] for stone in position.initial_stones]
    if colours.count("B") >= 2 and "W" not in colours:
        return "W"
    return "B"


def _visits(value: Any, name: str, low: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= MAX_VISITS_LIMIT:
        raise EngineError(f"{name} must be a whole number from {low} to {MAX_VISITS_LIMIT:,}")
    return value


def _encode(payload: dict) -> str:
    """The request as one JSON line; a non-finite number is never sent."""
    try:
        return json.dumps(payload, allow_nan=False)
    except (ValueError, TypeError):
        raise EngineError("refused a request with a non-finite or unencodable value") from None


class _Channel:
    """One request/reply connection: under a lock, write one line and read one line."""

    def __init__(self, engine: "HandolEngine", name: str, primary: bool) -> None:
        self.engine = engine
        self.name = name
        #: Only the primary (human) connection's loss is reported through ``on_disconnect``.
        self.primary = primary
        self.lock = asyncio.Lock()
        self.conn: LineConnection | None = None
        self.opened = False

    async def open(self) -> None:
        engine = self.engine
        conn = LineConnection(engine.host, engine.port)
        conn.send_timeout = engine.send_timeout
        await conn.connect(timeout=engine.connect_timeout)
        if self.opened:
            engine.note(f"# reopened the {self.name} connection "
                        "(the surface closes idle connections)")
        self.conn = conn
        self.opened = True

    async def close(self) -> None:
        conn, self.conn = self.conn, None
        if conn is not None:
            await conn.close()

    def abort(self) -> None:
        """Drop the connection at once (a request was cancelled mid-flight)."""
        conn, self.conn = self.conn, None
        if conn is not None:
            conn._abort()  # noqa: SLF001 - no public way to drop without awaiting
            conn._writer = conn._reader = None  # noqa: SLF001

    def _lost(self, error: EngineError) -> EngineError:
        """A second loss or a failed reopen: an engine error, reported once when primary."""
        engine = self.engine
        if self.primary:
            engine._lost(error)  # noqa: SLF001
            failure = engine._failure  # noqa: SLF001
            return failure.copy() if failure is not None else error
        engine.note(f"# the {self.name} connection was lost: {error.message}")
        return ConnectionClosed(error.message, address=engine.address)

    async def request(self, payload: dict, text: str) -> dict:
        """Send ``payload`` (encoded as ``text``) and return its answer."""
        async with self.lock:
            self.engine._check()  # noqa: SLF001
            losses = 0
            while True:
                if self.conn is not None and not self.conn.connected:
                    await self.close()  # found closed: that is one loss
                    losses += 1
                    if losses > 1:
                        raise self._lost(ConnectionClosed("the engine closed the connection"))
                if self.conn is None:
                    try:
                        await self.open()
                    except EngineError as exc:
                        if self.opened:
                            raise self._lost(exc) from None
                        raise
                try:
                    return await self._exchange(payload, text)
                except ConnectionClosed as exc:
                    await self.close()
                    losses += 1
                    if losses > 1:
                        raise self._lost(exc) from None
                except EngineError:
                    await self.close()  # an error of this request only; the next reopens
                    raise
                except BaseException:
                    self.abort()  # cancelled mid-flight: the stream position is unknown
                    raise

    async def _exchange(self, payload: dict, text: str) -> dict:
        engine = self.engine
        conn = self.conn
        assert conn is not None
        await conn.write_line(text, timeout=engine._send_limit())  # noqa: SLF001
        engine.log("send", clip(text))
        loop = asyncio.get_running_loop()
        timeout = engine.read_timeout
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise engine.error(f"the engine did not answer within {timeout:g}s")
            try:
                line = await conn.read_line(timeout=remaining)
            except ConnectionClosed:
                raise
            except EngineError as exc:
                if conn.broken:
                    raise
                raise engine.error(f"the engine did not answer within {timeout:g}s") from exc
            if not line.strip():
                continue
            engine.log("recv", clip(line))
            try:
                message = json.loads(line)
            except Exception:  # noqa: BLE001 - untrusted input (ValueError, RecursionError)
                message = None
            if not isinstance(message, dict):
                raise engine.error("the engine answered with something that is not a JSON object")
            answer_id = message.get("id")
            if answer_id is not None and answer_id != payload.get("id"):
                raise engine.error("the engine answered another request (mismatched id)")
            return message


@dataclass
class _Job:
    """One analysis request: the human query and, when eval visits are on, its winrate query."""

    position: Position
    human: dict
    human_text: str
    plain: dict | None
    plain_text: str
    callback: AnalysisCallback
    token: int
    tuples: int


class HandolEngine(Engine):
    """Speaks handol-mux's human-policy surface."""

    protocol = "handol"
    supports_genmove = True
    supports_final_score = False
    supports_raw = False

    def __init__(self, host: str, port: int, log: LogCallback | None = None) -> None:
        super().__init__(host, port, log)
        #: How long a request waits for its answer (longer than a real max-visits search).
        self.read_timeout = 600.0
        #: Draws human-style engine moves; replace it for reproducible games.
        self.rng = random.Random()
        self.profile: Any = DEFAULT_PROFILE
        self.policy: Any = {}
        self.compare: Any = None
        self.eval_visits: Any = DEFAULT_EVAL_VISITS
        self.max_visits: Any = DEFAULT_MAX_VISITS
        self.move_style: dict[str, Any] = {"B": "human", "W": "human"}
        self.name = "handol-mux human analysis"
        self._human = _Channel(self, "human", primary=True)
        self._eval = _Channel(self, "winrate", primary=False)
        self._ids = itertools.count(1)
        self._pending: _Job | None = None
        self._live: int | None = None
        self._drain_task: asyncio.Task | None = None

    def configure(self, profile: Any = _UNSET, policy: Any = _UNSET, compare: Any = _UNSET,
                  eval_visits: Any = _UNSET, max_visits: Any = _UNSET,
                  move_style: Any = _UNSET) -> None:
        """Store the settings; they are checked when a query is built, never here."""
        if profile is not _UNSET:
            self.profile = profile
        if policy is not _UNSET:
            self.policy = dict(policy) if isinstance(policy, dict) else policy
        if compare is not _UNSET:
            self.compare = dict(compare) if isinstance(compare, dict) else compare
        if eval_visits is not _UNSET:
            self.eval_visits = eval_visits
        if max_visits is not _UNSET:
            self.max_visits = max_visits
        if isinstance(move_style, dict):
            self.move_style = {**self.move_style, **move_style}

    # -- lifecycle ---------------------------------------------------------------------------
    async def connect(self) -> None:
        if self._human.conn is not None or self._eval.conn is not None:
            self._closing = True
            await self._teardown()
        self._human = _Channel(self, "human", primary=True)
        self._eval = _Channel(self, "winrate", primary=False)
        self._arm()
        # The surface has no handshake: an accepted TCP connection is all there is.
        await self._human.open()

    async def close(self) -> None:
        self._closing = True
        await self.stop_analysis()
        await self._teardown()

    async def _teardown(self) -> None:
        task, self._drain_task = self._drain_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._human.close()
        await self._eval.close()
        self._set_failure(ConnectionClosed("the engine connection is closed",
                                           address=self.address))

    def _on_failure(self, error: EngineError) -> None:
        self._pending = None
        self._live = None

    # -- queries -----------------------------------------------------------------------------
    def _position_fields(self, position: Position) -> dict[str, Any]:
        if engine_to_play(position) != position.to_play:
            inferred = "White" if engine_to_play(position) == "W" else "Black"
            raise EngineError(f"handol-mux infers who is to move and would put {inferred} on "
                              "move in this setup; play a move first")
        return {
            "boardXSize": position.size,
            "boardYSize": position.size,
            "komi": position.komi,
            "rules": position.rules,
            "initialStones": [list(s) for s in position.initial_stones],
            "moves": [list(m) for m in position.moves],
        }

    def _human_query(self, position: Position, visits: Any, compare: bool) -> dict:
        """The human request, validated with the visits in effect (nothing is sent on refusal)."""
        visits = _visits(visits, "max visits", 1)
        if not isinstance(self.profile, str) or not self.profile.strip():
            raise EngineError("the profile must be a non-empty name")
        policies = [self.policy]
        if compare and self.compare is not None:
            policies.append(self.compare)
        check_policies(policies, visits)
        human: dict[str, Any] = {"profile": self.profile,
                                 "policies": [wire_tuple(t) for t in policies]}
        if visits > 1:
            human["search"] = {"visits": visits}
        return {"id": f"h-{next(self._ids)}", **self._position_fields(position), "human": human}

    def _plain_query(self, position: Position, visits: int, include_ownership: bool) -> dict:
        query = {"id": f"e-{next(self._ids)}", **self._position_fields(position),
                 "maxVisits": visits}
        if include_ownership:
            query["includeOwnership"] = True
        return query

    async def _ask(self, channel: _Channel, payload: dict, text: str) -> dict:
        answer = await channel.request(payload, text)
        if "error" in answer:
            raise self.error(f"the engine refused the query: "
                             f"{clip(str(answer['error']), TEXT_LIMIT)}")
        return answer

    # -- analysis ------------------------------------------------------------------------------
    async def start_analysis(self, position: Position, callback: AnalysisCallback, *,
                             max_visits: int | None = None, interval: float = 0.4,
                             include_ownership: bool = False) -> None:
        # A new request leaves the old position, even when the new one is refused.
        self._live = None
        self._pending = None
        self._check()
        visits = self.max_visits if max_visits is None else max_visits
        human = self._human_query(position, visits, compare=True)
        eval_visits = _visits(self.eval_visits, "eval visits", 0)
        plain = (self._plain_query(position, eval_visits, include_ownership)
                 if eval_visits > 0 else None)
        token = next(self._ids)
        self._pending = _Job(position, human, _encode(human), plain,
                             _encode(plain) if plain is not None else "", callback, token,
                             len(human["human"]["policies"]))
        self._live = token
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain())

    async def stop_analysis(self) -> None:
        # Nothing to cancel on the wire: an answer in flight is dropped when it arrives.
        self._live = None
        self._pending = None

    async def _drain(self) -> None:
        """Send the newest pending request; requests that were replaced meanwhile never go."""
        while self._pending is not None:
            job, self._pending = self._pending, None
            try:
                analysis = await self._analyse(job)
            except EngineError as exc:
                self.note(f"# analysis failed: {clip(exc.message, TEXT_LIMIT)}")
                continue
            if self._live != job.token:
                continue  # the position was left while this was answered
            try:
                await deliver(job.callback, analysis)
            except Exception as exc:  # noqa: BLE001 - a consumer must not wedge the queue
                self.note(f"# dropped an analysis report: {type(exc).__name__}")

    async def _winrate(self, job: _Job) -> dict | None:
        """The plain answer for ``job``, or ``None`` (with a note) when it failed."""
        if job.plain is None:
            return None
        try:
            return await self._ask(self._eval, job.plain, job.plain_text)
        except EngineError as exc:
            self.note(f"# winrate query failed: {clip(exc.message, TEXT_LIMIT)}")
            return None

    async def _analyse(self, job: _Job) -> Analysis:
        answer, scored = await asyncio.gather(
            self._ask(self._human, job.human, job.human_text), self._winrate(job),
            return_exceptions=True)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(scored, BaseException):
            raise scored
        analysis, _ = parse_answer(answer, job.position, job.tuples)
        if scored is not None:
            merge_evaluation(analysis, scored, job.position.size)
        return analysis

    # -- engine moves --------------------------------------------------------------------------
    async def genmove(self, position: Position, color: str) -> str:
        """A move for ``color`` (the side to move) in that colour's move style."""
        letter = color_letter(color)
        if letter != position.to_play:
            raise EngineError(f"the engine can only move for the side to move "
                              f"({'black' if position.to_play == 'B' else 'white'})")
        await self.stop_analysis()
        self._check()
        style = self.move_style.get(letter)
        if style not in MOVE_STYLES:
            raise EngineError(f"unknown move style {str(style)[:40]!r}; expected human or katago")
        if style == "katago":
            visits = _visits(self.max_visits, "max visits", 1)
            query = self._plain_query(position, visits, include_ownership=False)
            answer = await self._ask(self._human, query, _encode(query))
            return _katago_choice(answer, position)
        # An engine move carries only the primary tuple.
        query = self._human_query(position, self.max_visits, compare=False)
        answer = await self._ask(self._human, query, _encode(query))
        _, entries = parse_answer(answer, position, 1)
        return self._sample(entries, position)

    def _sample(self, entries: list[tuple[str, float]], position: Position) -> str:
        """Draw a move weighted by p among the moves legal in the game."""
        game = _replay(position)
        colour = color_from_name(position.to_play)
        moves, weights = [], []
        for move, p in entries:
            if p > 0 and _legal(game, colour, move):
                moves.append(move)
                weights.append(p)
        if not moves:
            raise EngineError("the engine gave no legal move with p > 0")
        return self.rng.choices(moves, weights=weights, k=1)[0]


# -- answers --------------------------------------------------------------------------------------
def _replay(position: Position) -> Game:
    """The position as a game, for legality checks."""
    rules = position.rules if position.rules in RULE_SETS else DEFAULT_RULES
    try:
        game = Game(position.size, komi=position.komi, rules=rules)
        stones = []
        for colour, vertex in position.initial_stones:
            point = coords.from_gtp(vertex, position.size)
            if point is not None:
                stones.append((color_from_name(colour), point[0], point[1]))
        first = color_from_name(position.first_player)
        game._setup(position.size, game.rules, game.komi, 0, stones, first)  # noqa: SLF001
        for colour, vertex in position.moves:
            game.play(color_from_name(colour), coords.from_gtp(vertex, position.size))
    except Exception:  # noqa: BLE001 - a position the rules core cannot replay
        raise EngineError("the position cannot be replayed to check the engine's move") from None
    return game


def _legal(game: Game, colour: int, move: str) -> bool:
    try:
        return game.legal_error(colour, coords.from_gtp(move, game.size)) is None
    except ValueError:
        return False


def _katago_choice(answer: dict, position: Position) -> str:
    """KataGo's own pick from a plain answer: the move of ``order`` 0."""
    infos = answer.get("moveInfos")
    best = None
    for raw in infos if isinstance(infos, list) else []:
        if not isinstance(raw, dict):
            continue
        move = board_vertex(raw.get("move"), position.size)
        if move is not None and finite_int(raw.get("order")) == 0:
            best = move
            break
    if best is None:
        raise EngineError("the engine answered without a move")
    if not _legal(_replay(position), color_from_name(position.to_play), best):
        raise EngineError(f"the engine chose an illegal move ({best})")
    return best


def _distribution(entry: Any, size: int) -> list[tuple[str, float]]:
    """One tuple's distribution: on-board moves (or pass) with p, a bad p as 0."""
    items = entry.get("distribution") if isinstance(entry, dict) else None
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        move = board_vertex(item.get("move"), size)
        if move is None or move in seen:
            continue
        seen.add(move)
        p = finite(item.get("p"))
        out.append((move, p if p is not None and p > 0 else 0.0))
    return out


def _board(entries: list[tuple[str, float]], size: int) -> list[float]:
    """Row-major from the top-left, pass last."""
    board = [0.0] * (size * size + 1)
    for move, p in entries:
        point = coords.from_gtp(move, size)
        board[size * size if point is None else point[1] * size + point[0]] = p
    return board


def _candidates(entries: list[tuple[str, float]]) -> list[MoveInfo]:
    """The moves with p > 0, most likely first, at most :data:`TOP_CANDIDATES`."""
    likely = sorted((e for e in entries if e[1] > 0), key=lambda e: -e[1])[:TOP_CANDIDATES]
    return [MoveInfo(move=move, prior=p, order=order) for order, (move, p) in enumerate(likely)]


def parse_answer(answer: dict, position: Position,
                 tuples: int) -> tuple[Analysis, list[tuple[str, float]]]:
    """The human answer as the shared shape, and the primary distribution in full."""
    policies = answer.get("policies")
    if not isinstance(policies, list) or len(policies) < tuples:
        got = len(policies) if isinstance(policies, list) else 0
        raise EngineError(f"the engine answered {got} policies for {tuples} tuples")
    size = position.size
    entries = _distribution(policies[0], size)
    analysis = Analysis(
        move_infos=_candidates(entries),
        root=RootInfo(visits=None),
        policy=_board(entries, size),
        turn=clamp_turn(len(position.moves)),
        complete=True,
        source="handol",
        current_player=position.to_play,
    )
    if tuples > 1:
        other = _distribution(policies[1], size)
        analysis.compare = {
            "policy": _board(other, size),
            "moveInfos": [m.to_dict() for m in _candidates(other)],
            "probabilities": dict(other),
        }
    return analysis, entries


def merge_evaluation(analysis: Analysis, answer: dict, size: int) -> None:
    """Fold the plain answer (White's view) into the root and the candidates of both
    distributions, flipped to Black's view; moves match regardless of case."""
    root = answer.get("rootInfo")
    root = root if isinstance(root, dict) else {}
    analysis.root = RootInfo(
        visits=finite_int(root.get("visits")),
        winrate=flip_rate(root.get("winrate"), False),
        score_lead=flip_score(root.get("scoreLead"), False),
        score_mean=flip_score(root.get("scoreMean"), False),
    )
    by_move: dict[str, dict] = {}
    infos = answer.get("moveInfos")
    for raw in infos if isinstance(infos, list) else []:
        if isinstance(raw, dict):
            move = board_vertex(raw.get("move"), size)
            if move is not None:
                by_move.setdefault(move, raw)
    for info in analysis.move_infos:
        found = by_move.get(info.move)
        if found is not None:
            for name, value in _evaluation(found, size).items():
                setattr(info, name, value)
    for entry in analysis.compare["moveInfos"] if analysis.compare is not None else []:
        found = by_move.get(entry.get("move"))
        if found is not None:
            entry.update(MoveInfo(entry["move"], **_evaluation(found, size),
                                  prior=entry["prior"], order=entry["order"]).to_dict())
    analysis.ownership = point_values(answer.get("ownership"), flip=True, length=size * size)


def _evaluation(found: dict, size: int) -> dict[str, Any]:
    """One plain-answer move's numbers, flipped from White's view to Black's."""
    pv = found.get("pv")
    return {
        "visits": finite_int(found.get("visits")),
        "winrate": flip_rate(found.get("winrate"), False),
        "score_lead": flip_score(found.get("scoreLead"), False),
        "score_mean": flip_score(found.get("scoreMean"), False),
        "score_stdev": finite(found.get("scoreStdev")),
        "lcb": flip_rate(found.get("lcb"), False),
        "utility": flip_score(found.get("utility"), False),
        "utility_lcb": flip_score(found.get("utilityLcb"), False),
        "pv": board_vertices(pv if isinstance(pv, list) else [], size),
    }
