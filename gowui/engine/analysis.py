"""The KataGo analysis-engine client (SPEC §2.4).

JSON lines, one query per line, results matched by ``id``; several queries may be in flight, so a
background reader dispatches every result to the query waiting for it. There is no native
genmove: an engine move is a bounded search whose top-ranked move is played.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any, Callable

from .base import AnalysisCallback, Engine, LogCallback, color_letter, deliver
from .base import clip as _clip
from .errors import ConnectionClosed, EngineError
from .transport import LineConnection
from .types import (Analysis, MoveInfo, Position, RootInfo, board_vertex, board_vertices,
                    clamp_turn, finite, finite_int, flip_rate, flip_score, point_values,
                    sort_by_order)

DEFAULT_MAX_VISITS = 500

Handler = Callable[[dict], Any]


class AnalysisEngine(Engine):
    """Speaks KataGo's analysis JSON protocol."""

    protocol = "analysis"
    supports_genmove = True
    supports_final_score = False
    supports_raw = False

    def __init__(self, host: str, port: int, log: LogCallback | None = None) -> None:
        super().__init__(host, port, log)
        self.query_timeout = 600.0
        self.version_timeout = 15.0
        self._conn = LineConnection(host, port)
        self._reader_task: asyncio.Task | None = None
        self._handlers: dict[str, Handler] = {}
        self._waiters: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._live: str | None = None
        self._connecting = False

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids)}"

    # -- lifecycle ---------------------------------------------------------------------------
    async def connect(self) -> None:
        if self._reader_task is not None or self._conn.connected:
            # Connecting again tears the live connection down first (§2.1).
            self._closing = True
            await self._teardown()
        self._conn = LineConnection(self.host, self.port)
        self._arm()
        await self._conn.connect(timeout=self.connect_timeout)
        self._reader_task = asyncio.create_task(self._read_loop())
        self._connecting = True
        try:
            reply = await self._request({"action": "query_version"}, self.version_timeout)
        except BaseException:
            self._closing = True
            await self._teardown()
            raise
        finally:
            self._connecting = False
        version = reply.get("version")
        self.version = str(version) if isinstance(version, (str, int, float)) else ""
        self.name = "KataGo analysis engine"

    async def close(self) -> None:
        self._closing = True
        await self.stop_analysis()
        await self._teardown()

    async def _teardown(self) -> None:
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._conn.close()
        self._set_failure(ConnectionClosed("the engine connection is closed",
                                           address=self.address))

    def _drop_now(self) -> None:
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
        self._conn.abort()

    def _on_failure(self, error: EngineError) -> None:
        """Tell every waiting query why nothing is coming back; drop the streams."""
        waiters, self._waiters = list(self._waiters.values()), {}
        self._handlers = {}
        self._live = None
        for future in waiters:
            if not future.done():
                future.set_exception(error.copy())

    # -- plumbing -------------------------------------------------------------------------------
    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._conn.read_line()
                if not line.strip():
                    continue
                self.log("recv", _clip(line))
                try:
                    message = json.loads(line)
                except Exception:  # noqa: BLE001 - untrusted input (ValueError, RecursionError)
                    if self._connecting:
                        self._set_failure(self.error(
                            "the engine did not answer in JSON: this looks like a GTP engine, "
                            "so choose the gtp protocol"))
                        return
                    self.note("# skipped a line that is not JSON")
                    continue
                if not isinstance(message, dict):
                    self.note("# skipped a line that is not a JSON object")
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as exc:
            self._lost(exc)
        except EngineError as exc:
            self._set_failure(exc.copy())
        except Exception as exc:  # noqa: BLE001 - the reader is the last line of defence
            self._set_failure(self.error(f"the engine reader failed: {type(exc).__name__}"))

    async def _dispatch(self, message: dict) -> None:
        query_id = message.get("id")
        if "warning" in message and "turnNumber" not in message and "error" not in message:
            self.note(f"# engine warning: {_clip(str(message['warning']), 500)}")
            return
        waiter = self._waiters.get(query_id) if isinstance(query_id, str) else None
        if waiter is not None:
            if "error" in message:
                self._waiters.pop(query_id, None)
                if not waiter.done():
                    waiter.set_exception(self.error(
                        f"the engine refused the query: {_clip(str(message['error']), 500)}"))
            elif not message.get("isDuringSearch"):
                self._waiters.pop(query_id, None)
                if not waiter.done():
                    waiter.set_result(message)
            return
        handler = self._handlers.get(query_id) if isinstance(query_id, str) else None
        if handler is None:
            if "error" in message:
                self.note(f"# engine error: {_clip(str(message['error']), 500)}")
            return  # a terminated query, or an acknowledgement
        try:
            await deliver(handler, message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a vanished consumer must not end the read
            self.note(f"# dropped an analysis report: {type(exc).__name__}")

    async def _send(self, payload: dict, bound: float | None = None) -> None:
        """Send one query; a send the engine does not accept in time (at most ``bound``) leaves
        the connection unusable (§2.1)."""
        self._check()
        text = json.dumps(payload)
        try:
            await self._conn.write_line(text, timeout=self._send_limit(bound))
        except ConnectionClosed as exc:
            self._lost(exc)
            raise self._failure.copy() if self._failure else exc from None
        except EngineError as exc:
            if self._conn.broken:
                raise self._unusable(exc.message) from None
            raise
        self.log("send", _clip(text))

    async def _request(self, payload: dict, timeout: float) -> dict:
        """Send a query and wait for its final result (or error)."""
        self._check()
        query_id = str(payload.get("id") or self._next_id("req"))
        payload = {**payload, "id": query_id}
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._waiters[query_id] = future
        deadline = loop.time() + timeout
        try:
            await self._send(payload, timeout)  # sending counts against the query's deadline
            return await asyncio.wait_for(asyncio.shield(future),
                                          timeout=max(0.0, deadline - loop.time()))
        except asyncio.TimeoutError:
            raise self._unusable(f"the engine did not answer within {timeout:g}s") from None
        except asyncio.CancelledError:
            self._unusable("a query was cancelled while waiting for its reply")
            raise
        finally:
            self._waiters.pop(query_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # failed while sending: already raised another way

    # -- analysis ------------------------------------------------------------------------------
    @staticmethod
    def _query(position: Position, *, max_visits: int | None, include_ownership: bool,
               report_every: float | None) -> dict:
        query: dict[str, Any] = {
            "boardXSize": position.size,
            "boardYSize": position.size,
            "komi": position.komi,
            "rules": position.rules,
            "initialStones": [list(s) for s in position.initial_stones],
            "initialPlayer": "W" if str(position.first_player).upper().startswith("W") else "B",
            "moves": [list(m) for m in position.moves],
            "analyzeTurns": [len(position.moves)],
            "includePolicy": True,
            "maxVisits": int(max_visits or DEFAULT_MAX_VISITS),
        }
        if include_ownership:
            query["includeOwnership"] = True
        if report_every:
            query["reportDuringSearchEvery"] = float(report_every)
        return query

    async def start_analysis(self, position: Position, callback: AnalysisCallback, *,
                             max_visits: int | None = None, interval: float = 0.4,
                             include_ownership: bool = False) -> None:
        await self.stop_analysis()
        self._check()
        query_id = self._next_id("analyze")
        query = self._query(position, max_visits=max_visits, include_ownership=include_ownership,
                            report_every=max(0.1, interval))
        query["id"] = query_id
        black = position.black_to_play()
        turn = len(position.moves)

        async def handler(message: dict) -> None:
            if "error" in message:
                self.note(f"# analysis refused: {_clip(str(message['error']), 500)}")
                self._drop(query_id)
                return
            try:
                analysis = parse_result(message, black, position.size, turn)
            except Exception:  # noqa: BLE001 - untrusted input
                self.note("# skipped an analysis report that could not be read")
                return
            if analysis.complete:
                self._drop(query_id)
            await deliver(callback, analysis)

        self._handlers[query_id] = handler
        self._live = query_id
        try:
            await self._send(query)
        except BaseException:
            self._drop(query_id)
            raise

    def _drop(self, query_id: str) -> None:
        self._handlers.pop(query_id, None)
        if self._live == query_id:
            self._live = None

    async def stop_analysis(self) -> None:
        query_id, self._live = self._live, None
        if query_id is None:
            return
        self._handlers.pop(query_id, None)  # no report of it reaches anyone from here on
        if self._failure is not None:
            return
        try:
            await self._send({"id": self._next_id("terminate"), "action": "terminate",
                              "terminateId": query_id})
        except EngineError:
            pass

    async def genmove(self, position: Position, color: str, *,
                      max_visits: int | None = None) -> str:
        """Search the position with ``max_visits`` (500 when not given) and return the engine's
        top-ranked move (side to move only)."""
        letter = color_letter(color)
        if letter != position.to_play:
            raise self.error(f"the analysis engine can only move for the side to move "
                             f"({'black' if position.black_to_play() else 'white'})")
        await self.stop_analysis()
        query = self._query(position, max_visits=max_visits, include_ownership=False,
                            report_every=None)
        result = await self._request(query, self.query_timeout)
        analysis = parse_result(result, position.black_to_play(), position.size,
                                len(position.moves))
        if not analysis.move_infos:
            return "pass"
        return analysis.move_infos[0].move


def parse_result(message: dict, black_to_play: bool, size: int, turn: int) -> Analysis:
    """One analysis-engine result, sanitised (§2.2) and flipped to Black's view.

    The side to move comes from the position the client sent, not from the engine.
    """
    reported_turn = finite_int(message.get("turnNumber"))
    analysis = Analysis(
        turn=clamp_turn(turn if reported_turn is None else reported_turn),
        complete=not bool(message.get("isDuringSearch")),
        source="analysis",
        current_player="B" if black_to_play else "W",
    )
    infos = message.get("moveInfos")
    for raw in infos if isinstance(infos, list) else []:
        if not isinstance(raw, dict):
            continue
        move = board_vertex(raw.get("move"), size)
        if move is None:
            continue
        pv = raw.get("pv")
        analysis.move_infos.append(MoveInfo(
            move=move,
            visits=finite_int(raw.get("visits")),
            winrate=flip_rate(raw.get("winrate"), black_to_play),
            score_lead=flip_score(raw.get("scoreLead"), black_to_play),
            score_mean=flip_score(raw.get("scoreMean"), black_to_play),
            score_stdev=finite(raw.get("scoreStdev")),
            prior=finite(raw.get("prior")),
            lcb=flip_rate(raw.get("lcb"), black_to_play),
            utility=finite(raw.get("utility")),
            utility_lcb=finite(raw.get("utilityLcb")),
            order=finite_int(raw.get("order")),
            pv=board_vertices(pv if isinstance(pv, list) else [], size),
        ))
    sort_by_order(analysis.move_infos)
    root = message.get("rootInfo")
    root = root if isinstance(root, dict) else {}
    analysis.root = RootInfo(
        visits=finite_int(root.get("visits")),
        winrate=flip_rate(root.get("winrate"), black_to_play),
        score_lead=flip_score(root.get("scoreLead"), black_to_play),
        score_mean=flip_score(root.get("scoreMean"), black_to_play),
    )
    # Row-major from the top-left; policy has a trailing pass entry and -1 for illegal points.
    analysis.policy = point_values(message.get("policy"), length=size * size + 1)
    analysis.ownership = point_values(message.get("ownership"), flip=not black_to_play,
                                      length=size * size)
    return analysis
