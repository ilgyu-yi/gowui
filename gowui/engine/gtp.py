"""The GTP client (SPEC §2.3) for KataGo over TCP.

A command goes out as ``<id> <command>``; the reply is ``=id payload`` (or ``?id error``) closed by
a blank line. The analysis commands answer ``=id`` at once and then stream ``info ...`` reports
until interrupted by an empty line.

One background reader owns the socket and queues every line, so a hang-up is noticed even while
the client is idle (§2.1); commands, the analysis stream and ``genmove_analyze`` consume the queue
one at a time.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .base import TEXT_LIMIT, AnalysisCallback, Engine, LogCallback, clip, color_letter, deliver
from .errors import ConnectionClosed, EngineError
from .transport import LineConnection, check_line
from .types import (Analysis, MoveInfo, Position, RootInfo, board_vertex, board_vertices, finite,
                    finite_int, flip_rate, flip_score, point_values, sort_by_order)

#: A whole reply is capped at this many bytes (§2.1).
MAX_REPLY = 4 * 1024 * 1024
#: Lines the reader may queue ahead of the consumer before it stops reading (backpressure).
#: With lines of at most 1 MiB this bounds the queued input to 16 MiB per connection.
_QUEUE_LINES = 16
_HEAD = re.compile(r"([=?])([0-9]*)(.*)", re.DOTALL)
#: A reply id longer than this is a mismatch (§2.1); it could not name a command we sent.
_MAX_ID_DIGITS = 18
_KATA_OPTIONS = (("ownership", "ownership"), ("rootInfo", "rootInfo"))

#: Single-valued keys in a ``kata-analyze`` / ``lz-analyze`` report.
_SCALARS = {"move", "visits", "edgeVisits", "weight", "edgeWeight", "utility", "winrate",
            "scoreMean", "scoreStdev", "scoreLead", "scoreSelfplay", "prior", "lcb",
            "utilityLcb", "order", "isSymmetryOf"}
#: Keys whose value runs until the next key or section.
_LISTS = {"pv", "pvVisits", "pvEdgeVisits", "ownership", "ownershipStdev", "movesOwnership",
          "movesOwnershipStdev"}
_SECTIONS = {"info", "rootInfo"}
_KEYWORDS = _SCALARS | _LISTS | _SECTIONS


class _Rejected(EngineError):
    """A ``?`` reply: the engine refused the command; the connection is fine."""


class GTPEngine(Engine):
    """Drives a GTP engine and keeps a mirror of its board to sync incrementally."""

    protocol = "gtp"
    supports_genmove = True
    supports_final_score = True
    supports_raw = True

    def __init__(self, host: str, port: int, log: LogCallback | None = None) -> None:
        super().__init__(host, port, log)
        self.command_timeout = 30.0
        self.genmove_timeout = 600.0
        self.stop_timeout = 5.0
        self._conn = LineConnection(host, port)
        self._lines: asyncio.Queue | None = None
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._stream_task: asyncio.Task | None = None
        self._next_id = 1
        self._commands: set[str] = set()
        self._has_root_info = True
        self._has_ownership = True
        # Mirror of the engine's board: (size, komi, rules, setup) and the moves played on it.
        self._state: tuple | None = None
        self._moves: list[list[str]] = []

    # -- lifecycle ---------------------------------------------------------------------------
    async def connect(self) -> None:
        await self._drop_connection()
        # Every connection starts afresh: new socket, empty mirror, no remembered capability.
        self._conn = LineConnection(self.host, self.port)
        self.invalidate_mirror()
        self._commands = set()
        self._has_root_info = True
        self._has_ownership = True
        self._arm()
        self._lines = asyncio.Queue(maxsize=_QUEUE_LINES)
        await self._conn.connect(timeout=self.connect_timeout)
        self._reader_task = asyncio.create_task(self._read_loop())
        try:
            self.name = await self._optional("name") or "GTP engine"
            self.version = await self._optional("version") or ""
            listed = await self._optional("list_commands") or ""
            self._commands = {line.strip() for line in listed.splitlines() if line.strip()}
            if not self._commands:
                raise self.error("the engine answered no command list; is this a GTP engine?")
        except BaseException:
            self._closing = True
            await self._teardown()
            raise

    async def close(self) -> None:
        self._closing = True
        await self.stop_analysis()
        if self._failure is None and self._conn.connected:
            try:
                await asyncio.wait_for(self._command("quit"), 2.0)
            except (EngineError, asyncio.TimeoutError):
                pass
        await self._teardown()

    async def _drop_connection(self) -> None:
        """Tear down a live connection (stream, reader and socket) before connecting again."""
        stream, self._stream_task = self._stream_task, None
        if stream is not None:
            stream.cancel()
            await asyncio.gather(stream, return_exceptions=True)
        if self._reader_task is not None or self._conn.connected:
            self._closing = True
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
        for task in (self._stream_task, self._reader_task):
            if task is not None:
                task.cancel()
        self._stream_task = self._reader_task = None
        self._conn.abort()

    def supports(self, command: str) -> bool:
        return command in self._commands

    # -- the line queue -----------------------------------------------------------------------
    async def _read_loop(self) -> None:
        assert self._lines is not None
        try:
            while True:
                line = await self._conn.read_line()
                await self._lines.put(line)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as exc:
            self._lost(exc)
        except EngineError as exc:
            self._set_failure(exc.copy())
        except Exception as exc:  # noqa: BLE001 - the reader is the last line of defence
            self._set_failure(self.error(f"the engine reader failed: {type(exc).__name__}"))

    async def _next_line(self, timeout: float | None, deadline: float | None = None) -> str:
        """The next received line, waiting at most ``timeout`` -- or, given a ``deadline`` (loop
        time), only until then; a timeout leaves the connection unusable."""
        assert self._lines is not None
        wait = timeout
        if deadline is not None:
            wait = deadline - asyncio.get_running_loop().time()
            if wait <= 0:
                self._check()
                raise self._unusable(f"the engine did not answer within {timeout:g}s")
        if not self._lines.empty():
            return self._lines.get_nowait()
        self._check()
        getter = asyncio.ensure_future(self._lines.get())
        waiting = {getter} if self._failed is None else {getter, self._failed}
        try:
            await asyncio.wait(waiting, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not getter.done():
                getter.cancel()
        if getter.done() and not getter.cancelled():
            return getter.result()
        self._check()
        raise self._unusable(f"the engine did not answer within {timeout:g}s")

    async def _write(self, line: str, bound: float | None = None) -> None:
        """Send one line; a send the engine does not accept in time leaves the connection
        unusable (§2.1)."""
        self._check()
        try:
            await self._conn.write_line(line, timeout=self._send_limit(bound))
        except ConnectionClosed as exc:
            self._lost(exc)
            raise self._failure.copy() if self._failure else exc from None
        except EngineError as exc:
            if self._conn.broken:
                raise self._unusable(exc.message) from None
            raise

    # -- one command -----------------------------------------------------------------------------
    async def _send(self, command: str, bound: float | None = None) -> int:
        """Write one command line with a fresh id; the caller holds the lock."""
        check_line(command)
        self._check()
        command_id = self._next_id
        self._next_id += 1
        line = f"{command_id} {command}"
        await self._write(line, bound)
        self.log("send", line)
        return command_id

    def _deadline(self, timeout: float) -> float:
        """One deadline for a whole reply (§2.1), in loop time."""
        return asyncio.get_running_loop().time() + timeout

    async def _head(self, command_id: int, timeout: float, deadline: float) -> tuple[bool, str]:
        """Skip stray output until the ``=``/``?`` line answering ``command_id``."""
        while True:
            line = await self._next_line(timeout, deadline)
            if line.lstrip().startswith("{"):
                raise self._unusable(
                    "the engine answered with JSON: this looks like the KataGo analysis engine, "
                    "so choose the analysis protocol")
            match = _HEAD.match(line)
            if match:
                ok, digits, rest = match.group(1) == "=", match.group(2), match.group(3)
                # Compare at most 18 digits as a number; anything longer cannot be ours.
                if digits and (len(digits) > _MAX_ID_DIGITS or int(digits) != command_id):
                    shown = digits if len(digits) <= _MAX_ID_DIGITS else (
                        digits[:_MAX_ID_DIGITS] + "...")
                    raise self._unusable(f"a reply for command {shown} arrived while "
                                         f"waiting for command {command_id}")
                return ok, rest.strip()
            if line.strip():
                self.log("recv", clip(line))

    async def _body(self, first: str, timeout: float, deadline: float) -> str:
        """The rest of a reply up to its blank line, capped at :data:`MAX_REPLY` in total."""
        lines = [first] if first else []
        total = len(first.encode()) + 1
        while True:
            line = await self._next_line(timeout, deadline)
            if line == "":
                return "\n".join(lines)
            total += len(line.encode()) + 1
            if total > MAX_REPLY:
                raise self._unusable("the engine's reply is larger than 4 MiB")
            lines.append(line)

    async def _exchange(self, command: str, timeout: float) -> str:
        """Send ``command`` and return its payload; a ``?`` reply raises :class:`_Rejected`."""
        deadline = self._deadline(timeout)
        command_id = await self._send(command, timeout)
        try:
            ok, first = await self._head(command_id, timeout, deadline)
            payload = (await self._body(first, timeout, deadline)).strip()
        except asyncio.CancelledError:
            self._unusable(f"{command.split()[0] if command.split() else 'a command'} was "
                           "cancelled while waiting for its reply")
            raise
        except EngineError:
            raise
        except Exception as exc:  # noqa: BLE001 - untrusted input must not escape as a bug
            raise self._unusable(f"the engine's reply could not be read "
                                 f"({type(exc).__name__})") from None
        self.log("recv", clip(f"{'=' if ok else '?'} {payload}".rstrip()))
        if not ok:
            raise _Rejected(clip(payload, TEXT_LIMIT)
                            or f"the engine rejected {command.split()[0]!r}",
                            address=self.address)
        return payload

    async def _stop_stream_locked(self) -> None:
        """Stop a stream started while we waited for the lock (the lock is held)."""
        if self._stream_task is not None:
            await self.stop_analysis()

    async def _command(self, command: str, timeout: float | None = None) -> str:
        """One ordinary command: stop any analysis stream first, then exchange under the lock."""
        await self.stop_analysis()
        async with self._lock:
            await self._stop_stream_locked()
            return await self._exchange(command, self.command_timeout if timeout is None
                                        else timeout)

    async def _optional(self, command: str) -> str | None:
        try:
            return await self._command(command)
        except _Rejected:
            return None

    async def raw(self, command: str) -> str:
        """A console command, passed through verbatim; its reply, or an error for ``?``.

        Any raw command may move the engine's board, so the mirror is dropped either way.
        """
        check_line(command)
        self.invalidate_mirror()
        try:
            return await self._command(command)
        finally:
            self.invalidate_mirror()

    # -- board synchronisation -----------------------------------------------------------------
    async def sync(self, position: Position) -> None:
        """Bring the engine's board to ``position`` with as few commands as possible."""
        setup = tuple((str(c), str(v)) for c, v in position.initial_stones)
        key = (position.size, float(position.komi), position.rules, setup)
        try:
            if self._state != key:
                await self._reset(position, key)
            else:
                shared = _common_prefix(self._moves, position.moves)
                extra = len(self._moves) - shared
                if extra and extra <= 3 and self.supports("undo"):
                    for _ in range(extra):
                        await self._command("undo")
                        self._moves.pop()
                elif extra:
                    await self._reset(position, key)
            for color, vertex in position.moves[len(self._moves):]:
                await self._command(f"play {color} {vertex}")
                self._moves.append([color, vertex])
        except BaseException:
            # A half-applied sync must not leave a prefix the next call would trust.
            self.invalidate_mirror()
            raise

    async def _reset(self, position: Position, key: tuple) -> None:
        self.invalidate_mirror()
        await self._command(f"boardsize {position.size}")
        await self._command("clear_board")
        await self._command(f"komi {float(position.komi):g}")
        if self.supports("kata-set-rules"):
            try:
                await self._command(f"kata-set-rules {position.rules}")
            except _Rejected:
                self.note(f"# the engine kept its own rules ({position.rules} not accepted)")
        for color, vertex in position.initial_stones:
            await self._command(f"play {color} {vertex}")
        self._state = key
        self._moves = []

    def invalidate_mirror(self) -> None:
        self._state = None
        self._moves = []

    async def set_max_visits(self, max_visits: int | None) -> None:
        if not max_visits or not self.supports("kata-set-param"):
            return
        try:
            await self._command(f"kata-set-param maxVisits {int(max_visits)}")
        except _Rejected:
            pass

    # -- moves ------------------------------------------------------------------------------------
    def _move_answer(self, reply: str, size: int) -> str:
        text = reply.strip()
        if text.lower() in ("pass", "resign"):
            return text.lower()
        vertex = board_vertex(text, size)
        if vertex is None:
            raise self.error("the engine answered something that is not a move on this board")
        return vertex

    async def genmove(self, position: Position, color: str) -> str:
        letter = color_letter(color)
        await self.stop_analysis()
        await self.sync(position)
        reply = await self._command(f"genmove {letter}", timeout=self.genmove_timeout)
        vertex = self._move_answer(reply, position.size)
        self._played(letter, vertex)
        return vertex

    def _played(self, color: str, vertex: str) -> None:
        if vertex == "resign":
            return
        if self._state is not None:
            self._moves.append([color, vertex])

    async def genmove_analyze(self, position: Position, color: str, callback: AnalysisCallback,
                              *, interval: float = 0.4, include_ownership: bool = False) -> str:
        """Generate a move while streaming the search; plain ``genmove`` without the command."""
        letter = color_letter(color)
        if not self.supports("kata-genmove_analyze"):
            return await self.genmove(position, letter)
        await self.stop_analysis()
        await self.sync(position)
        black = letter == "B"
        async with self._lock:
            await self._stop_stream_locked()
            await self._open_stream("kata-genmove_analyze", letter, interval, include_ownership,
                                    self.genmove_timeout)
            try:
                vertex = None
                while True:
                    text = await self._next_line(self.genmove_timeout)
                    if text == "":
                        break
                    if text.startswith("play "):
                        vertex = self._move_answer(text[5:], position.size)
                        continue
                    await self._report(text, black, position.size, callback, lz=False)
            except asyncio.CancelledError:
                self._unusable("kata-genmove_analyze was cancelled while the engine searched")
                raise
        if vertex is None:
            raise self.error("the engine finished kata-genmove_analyze without a move")
        self._played(letter, vertex)
        return vertex

    # -- analysis ----------------------------------------------------------------------------------
    def _attempts(self, include_ownership: bool) -> list[tuple[bool, bool]]:
        own = include_ownership and self._has_ownership
        root = self._has_root_info
        out: list[tuple[bool, bool]] = []
        for attempt in ((own, root), (own, False), (False, root), (False, False)):
            if attempt not in out:
                out.append(attempt)
        return out

    async def _open_stream(self, name: str, color: str, interval: float,
                           include_ownership: bool, timeout: float) -> None:
        """Send an analyze command, walking the option fallback of §2.3 (lock held)."""
        centiseconds = max(1, int(round(interval * 100)))
        base = f"{name} {color} {centiseconds}"
        kata = name != "lz-analyze"
        attempts = self._attempts(include_ownership) if kata else [(False, False)]
        asked_ownership, asked_root = attempts[0]
        error = ""
        for index, (ownership, root_info) in enumerate(attempts):
            if index:
                dropped = [label for label, was, now in (("ownership", asked_ownership, ownership),
                                                         ("rootInfo", asked_root, root_info))
                           if was and not now]
                self.note(f"# {name} options rejected ({error}); retrying without "
                          f"{' and '.join(dropped) or 'options'}")
            command = base
            if ownership:
                command += " ownership true"
            if root_info:
                command += " rootInfo true"
            deadline = self._deadline(timeout)
            command_id = await self._send(command, timeout)
            try:
                ok, first = await self._head(command_id, timeout, deadline)
                if not ok:
                    error = clip((await self._body(first, timeout, deadline)).strip()
                                 or "rejected", TEXT_LIMIT)
            except asyncio.CancelledError:
                self._unusable(f"{name} was cancelled while waiting for its reply")
                raise
            if ok:
                self.log("recv", "=")
                if asked_ownership and not ownership:
                    self._has_ownership = False
                if asked_root and not root_info:
                    self._has_root_info = False
                return
            self.log("recv", f"? {error}")
        raise self.error(f"the engine rejected {name}: {error}")

    async def start_analysis(self, position: Position, callback: AnalysisCallback, *,
                             max_visits: int | None = None, interval: float = 0.4,
                             include_ownership: bool = False) -> None:
        await self.stop_analysis()
        if self.supports("kata-analyze"):
            name = "kata-analyze"
        elif self.supports("lz-analyze"):
            name = "lz-analyze"
        else:
            raise self.error("the engine supports neither kata-analyze nor lz-analyze")
        await self.set_max_visits(max_visits)
        await self.sync(position)
        color = position.to_play
        async with self._lock:
            await self._stop_stream_locked()
            await self._open_stream(name, color, interval, include_ownership,
                                    self.command_timeout)
            self._stream_task = asyncio.create_task(
                self._read_stream(color == "B", position.size, callback, name == "lz-analyze"))

    async def _report(self, text: str, black: bool, size: int, callback: AnalysisCallback,
                      lz: bool) -> None:
        try:
            analysis = parse_analysis_line(text, black, size, lz=lz)
        except Exception:  # noqa: BLE001 - untrusted input
            analysis = None
        if analysis is None:
            self.note("# skipped an analysis report that could not be read")
            return
        try:
            await deliver(callback, analysis)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a vanished consumer must not end the read
            self.note(f"# dropped an analysis report: {type(exc).__name__}")

    async def _read_stream(self, black: bool, size: int, callback: AnalysisCallback,
                           lz: bool) -> None:
        """Deliver reports until the blank line that ends the response."""
        try:
            while True:
                text = await self._next_line(None)
                if text == "":
                    return
                await self._report(text, black, size, callback, lz)
        except EngineError as exc:
            self.note(f"# analysis stopped: {exc.message}")

    async def stop_analysis(self) -> None:
        """Interrupt the stream and wait a bounded time; past the bound the connection is
        unusable (§2.1)."""
        async with self._stop_lock:
            task, self._stream_task = self._stream_task, None
            if task is None:
                return
            if task.done() or self._failure is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return
            try:
                await self._write("")
                self.log("send", "")
            except EngineError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self.stop_timeout)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._unusable(f"the engine did not stop analysing within "
                               f"{self.stop_timeout:g}s")
                self.note("# the engine did not stop analysing in time")
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise

    # -- scoring ------------------------------------------------------------------------------------
    async def final_score(self, position: Position) -> str:
        await self.stop_analysis()
        await self.sync(position)
        return await self._command("final_score")


def _common_prefix(a: list[list[str]], b: list[list[str]]) -> int:
    count = 0
    for left, right in zip(a, b):
        if list(left) != list(right):
            break
        count += 1
    return count


def _tokens_until_keyword(tokens: list[str], index: int) -> tuple[list[str], int]:
    values = []
    while index < len(tokens) and tokens[index] not in _KEYWORDS:
        values.append(tokens[index])
        index += 1
    return values, index


def parse_analysis_line(text: str, black_to_play: bool, size: int, *, lz: bool = False,
                        source: str = "gtp") -> Analysis | None:
    """One ``kata-analyze`` / ``lz-analyze`` report, sanitised (§2.2) and in Black's view.

    ``lz`` reports carry winrates, priors and bounds on a 0-10000 scale. ``None`` when nothing in
    the line can be read.
    """
    tokens = text.split()
    if not tokens or tokens[0] not in _SECTIONS and tokens[0] not in _LISTS:
        return None
    infos: list[dict[str, Any]] = []
    root: dict[str, Any] | None = None
    current: dict[str, Any] | None = None
    ownership: list[str] | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _SECTIONS:
            current = {}
            if token == "info":
                infos.append(current)
            else:
                root = current
            index += 1
        elif token in _SCALARS:
            if index + 1 < len(tokens) and current is not None:
                current[token] = tokens[index + 1]
            index += 2
        elif token in _LISTS:
            values, index = _tokens_until_keyword(tokens, index + 1)
            if token == "ownership":
                ownership = values
            elif current is not None:
                current[token] = values
        else:
            index += 1

    scale = 10000.0 if lz else 1.0

    def rate(value: Any) -> float | None:
        number = finite(value)
        return None if number is None else number / scale

    analysis = Analysis(source=source, current_player="B" if black_to_play else "W")
    for fields in infos:
        move = board_vertex(fields.get("move"), size)
        if move is None:
            continue
        analysis.move_infos.append(MoveInfo(
            move=move,
            visits=finite_int(fields.get("visits")),
            winrate=flip_rate(rate(fields.get("winrate")), black_to_play),
            score_lead=flip_score(fields.get("scoreLead"), black_to_play),
            score_mean=flip_score(fields.get("scoreMean"), black_to_play),
            score_stdev=finite(fields.get("scoreStdev")),
            prior=rate(fields.get("prior")),
            lcb=flip_rate(rate(fields.get("lcb")), black_to_play),
            utility=flip_score(fields.get("utility"), black_to_play),
            utility_lcb=flip_score(fields.get("utilityLcb"), black_to_play),
            order=finite_int(fields.get("order")),
            pv=board_vertices(fields.get("pv", []), size),
        ))
    sort_by_order(analysis.move_infos)
    if ownership is not None and not lz:
        analysis.ownership = point_values(ownership, flip=not black_to_play, length=size * size)
    if root is not None:
        analysis.root = RootInfo(
            visits=finite_int(root.get("visits")),
            winrate=flip_rate(rate(root.get("winrate")), black_to_play),
            score_lead=flip_score(root.get("scoreLead"), black_to_play),
            score_mean=flip_score(root.get("scoreMean"), black_to_play),
        )
    elif analysis.move_infos:
        best = analysis.move_infos[0]
        analysis.root = RootInfo(visits=sum(m.visits or 0 for m in analysis.move_infos),
                                 winrate=best.winrate, score_lead=best.score_lead,
                                 score_mean=best.score_mean)
    if not analysis.move_infos and not analysis.ownership and root is None:
        return None
    return analysis
