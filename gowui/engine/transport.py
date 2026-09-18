"""A newline-delimited TCP connection (SPEC §2.1).

KataGo speaks over stdin/stdout, so the far end is usually the engine behind something like
``socat TCP-LISTEN:6363,reuseaddr,fork EXEC:"katago gtp ..."``. Both KataGo protocols and
handol-mux are line protocols, so one transport serves every client.

Every failure is an :class:`EngineError` whose message never names the host or port (the address
rides along in ``error.address``); OS error text is never copied into a message, because the
connect error text of asyncio includes the address tuple.
"""

from __future__ import annotations

import asyncio
import errno as errno_module
import os
import socket

from .errors import ConnectionClosed, EngineError

DEFAULT_CONNECT_TIMEOUT = 10.0
#: A received line may be at most this long (without its terminator).
MAX_LINE = 1024 * 1024
_FORBIDDEN = ("\r", "\n", "\x00")


def check_line(text: str) -> None:
    """Refuse a command that is not exactly one line (CR, LF or NUL), before anything is sent."""
    if any(ch in text for ch in _FORBIDDEN):
        raise EngineError("refused a command containing a line break or NUL: send one line")


def _os_reason(exc: BaseException) -> str:
    """Why an OS-level call failed, without the OS message text (which may hold the address)."""
    if isinstance(exc, socket.gaierror):
        return "the host name could not be resolved"
    code = getattr(exc, "errno", None)
    if isinstance(code, int) and code in errno_module.errorcode:
        return os.strerror(code).lower()
    return type(exc).__name__


class LineConnection:
    """Newline-delimited UTF-8 framing over :mod:`asyncio` streams, with bounded lines."""

    def __init__(self, host: str, port: int, encoding: str = "utf-8") -> None:
        self.host = host
        self.port = port
        self.encoding = encoding
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        #: Why the stream position is unknown (an overlong line), if it is.
        self._broken = ""

    @property
    def address(self) -> tuple[str, int]:
        return (self.host, self.port)

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing() and not self._broken

    def _closed(self, message: str) -> ConnectionClosed:
        return ConnectionClosed(message, address=self.address)

    async def connect(self, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, limit=MAX_LINE), timeout=timeout)
        except asyncio.TimeoutError:
            raise self._closed(f"timed out connecting to the engine after {timeout:g}s") from None
        except OSError as exc:
            raise self._closed(f"cannot connect to the engine: {_os_reason(exc)}") from None
        self._broken = ""

    async def write_line(self, text: str) -> None:
        check_line(text)
        if self._broken:
            raise EngineError(self._broken, address=self.address)
        if self._writer is None or self._writer.is_closing():
            raise self._closed("the engine connection is closed")
        async with self._write_lock:
            writer = self._writer
            if writer is None or writer.is_closing():
                raise self._closed("the engine connection is closed")
            try:
                writer.write((text + "\n").encode(self.encoding))
                await writer.drain()
            except (ConnectionError, OSError) as exc:
                raise self._closed(f"lost the engine connection while writing: "
                                   f"{_os_reason(exc)}") from None

    async def read_line(self, timeout: float | None = None) -> str:
        """One line without its terminator. EOF (even mid-line) is :class:`ConnectionClosed`."""
        if self._broken:
            raise EngineError(self._broken, address=self.address)
        reader = self._reader
        if reader is None:
            raise self._closed("the engine connection is closed")
        try:
            if timeout is None:
                raw = await reader.readline()
            else:
                raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            raise EngineError(f"the engine sent nothing for {timeout:g}s",
                              address=self.address) from None
        except (ValueError, asyncio.LimitOverrunError):
            self._broken = (f"the engine sent a line longer than {MAX_LINE // (1024 * 1024)} MiB; "
                            "the connection is unusable")
            raise EngineError(self._broken, address=self.address) from None
        except (ConnectionError, OSError) as exc:
            raise self._closed(f"lost the engine connection: {_os_reason(exc)}") from None
        if not raw.endswith(b"\n"):
            raise self._closed("the engine closed the connection")
        return raw.decode(self.encoding, errors="replace").rstrip("\r\n")

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 2.0)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            pass
