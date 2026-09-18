"""The protocol-neutral engine surface the session uses, with the shared failure bookkeeping of
SPEC §2.1: an unusable connection fails every later call at once, and a lost connection is marked
dead first and then reported exactly once through a scheduled ``on_disconnect`` callback."""

from __future__ import annotations

import abc
import asyncio
import inspect
from typing import Any, Awaitable, Callable

from .errors import ConnectionClosed, EngineError
from .types import Analysis, Position

AnalysisCallback = Callable[[Analysis], "Awaitable[None] | None"]
LogCallback = Callable[[str, str], Any]
DisconnectCallback = Callable[[EngineError], "Awaitable[None] | None"]

__all__ = ["AnalysisCallback", "Engine", "EngineError", "LogCallback", "Position", "deliver"]


async def deliver(callback: Callable[[Any], Any], payload: Any) -> None:
    """Call a sync or async callback."""
    result = callback(payload)
    if inspect.isawaitable(result):
        await result


class Engine(abc.ABC):
    """A connection to one engine. Subclasses implement one protocol each."""

    protocol: str = "unknown"
    supports_genmove: bool = False
    supports_final_score: bool = False
    supports_raw: bool = False

    def __init__(self, host: str, port: int, log: LogCallback | None = None) -> None:
        self.host = host
        self.port = port
        self.name = ""
        self.version = ""
        self._log = log
        #: Called once with an :class:`EngineError` when the connection is lost (sync or async).
        self.on_disconnect: DisconnectCallback | None = None
        self.connect_timeout = 10.0
        self._failure: EngineError | None = None
        self._failed: asyncio.Future | None = None
        self._closing = False
        self._disconnect_reported = False
        self._callback_tasks: set[asyncio.Task] = set()

    # -- traffic log ------------------------------------------------------------------
    def log(self, direction: str, text: str) -> None:
        if self._log is None:
            return
        try:
            self._log(direction, text)
        except Exception:  # noqa: BLE001 - a broken log must not break the engine
            pass

    def note(self, text: str) -> None:
        """A client-side note in the traffic log (never contains the engine's address)."""
        self.log("note", text if text.startswith("#") else f"# {text}")

    @property
    def address(self) -> tuple[str, int]:
        return (self.host, self.port)

    def error(self, message: str) -> EngineError:
        return EngineError(message, address=self.address)

    # -- failure bookkeeping -------------------------------------------------------------
    def _arm(self) -> None:
        """Reset the failure state for a fresh connection (inside the running loop)."""
        self._failure = None
        self._failed = asyncio.get_running_loop().create_future()
        self._closing = False
        self._disconnect_reported = False

    def _set_failure(self, error: EngineError) -> None:
        if self._failure is not None and (isinstance(self._failure, ConnectionClosed)
                                          or not isinstance(error, ConnectionClosed)):
            return
        if error.address is None:
            error.address = self.address
        self._failure = error
        if self._failed is not None and not self._failed.done():
            self._failed.set_result(None)
        self._on_failure(error)

    def _on_failure(self, error: EngineError) -> None:
        """Hook: fail whatever is waiting on the connection."""

    def _unusable(self, reason: str) -> EngineError:
        """Mark the connection unusable (the stream position is unknown) and return the error."""
        self._set_failure(self.error(f"{reason}; the engine connection is unusable"))
        failure = self._failure
        assert failure is not None
        return failure.copy()

    def _check(self) -> None:
        """Fail at once when the connection is dead or unusable."""
        if self._failure is not None:
            raise self._failure.copy()

    def _lost(self, error: EngineError) -> None:
        """The connection is gone: mark it dead first, then schedule the report (once)."""
        already = isinstance(self._failure, ConnectionClosed)
        self._set_failure(ConnectionClosed(error.message or "the engine closed the connection",
                                           address=self.address))
        if already or self._closing or self._disconnect_reported:
            return
        self._disconnect_reported = True
        self.note(f"# engine connection lost: {error.message}")
        failure = self._failure
        assert failure is not None
        asyncio.get_running_loop().call_soon(self._report_disconnect, failure.copy())

    def _report_disconnect(self, error: EngineError) -> None:
        callback = self.on_disconnect
        if callback is None:
            return
        try:
            result = callback(error)
        except Exception as exc:  # noqa: BLE001
            self.note(f"# disconnect handler failed: {exc!r}")
            return
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(result)
            self._callback_tasks.add(task)
            task.add_done_callback(self._callback_done)

    def _callback_done(self, task: asyncio.Task) -> None:
        self._callback_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.note(f"# disconnect handler failed: {task.exception()!r}")

    # -- the surface -------------------------------------------------------------------------
    @abc.abstractmethod
    async def connect(self) -> None:
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        ...

    @abc.abstractmethod
    async def genmove(self, position: Position, color: str) -> str:
        """The engine's move as a GTP vertex, ``pass`` or ``resign``."""

    @abc.abstractmethod
    async def start_analysis(self, position: Position, callback: AnalysisCallback, *,
                             max_visits: int | None = None, interval: float = 0.4,
                             include_ownership: bool = False) -> None:
        """Begin analysing ``position``, delivering reports to ``callback``."""

    @abc.abstractmethod
    async def stop_analysis(self) -> None:
        ...

    @property
    def description(self) -> str:
        parts = [p for p in (self.name, self.version) if p]
        return " ".join(parts) or self.protocol


def color_letter(color: str) -> str:
    """``B``/``W`` for a colour name the session passes (``B``, ``W``, ``black``, ``white``)."""
    key = color.strip().lower() if isinstance(color, str) else ""
    if key in ("b", "black"):
        return "B"
    if key in ("w", "white"):
        return "W"
    raise EngineError(f"unknown colour {color!r}")
