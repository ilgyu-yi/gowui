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
#: How long a send may wait for the engine to accept the data (seconds).
DEFAULT_SEND_TIMEOUT = 10.0
#: How long closing waits for buffered data to go out before dropping the connection.
CLOSE_GRACE = 0.5
MAX_HOST = 253
#: A received line may be at most this long (without its terminator).
MAX_LINE = 1024 * 1024
_FORBIDDEN = ("\r", "\n", "\x00")


def check_line(text: str) -> None:
    """Refuse a command that is not exactly one line (CR, LF or NUL), before anything is sent."""
    if any(ch in text for ch in _FORBIDDEN):
        raise EngineError("refused a command containing a line break or NUL: send one line")


def _valid_address(host: object, port: object) -> bool:
    """A non-empty host of at most 253 characters without NUL, and an int port in 1-65535."""
    if not isinstance(host, str) or not host or "\x00" in host or len(host) > MAX_HOST:
        return False
    return isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536


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
        #: How long a send may wait for the engine to accept the data.
        self.send_timeout = DEFAULT_SEND_TIMEOUT
        #: Why the stream position is unknown (an overlong line, a stalled send), if it is.
        self._broken = ""

    @property
    def address(self) -> tuple[str, int]:
        return (self.host, self.port)

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing() and not self._broken

    @property
    def broken(self) -> bool:
        """The stream position is unknown: every later read and write fails."""
        return bool(self._broken)

    def _closed(self, message: str) -> ConnectionClosed:
        return ConnectionClosed(message, address=self.address)

    async def connect(self, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> None:
        invalid = "cannot connect to the engine: invalid address"
        if not _valid_address(self.host, self.port):
            raise self._closed(invalid)
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, limit=MAX_LINE), timeout=timeout)
        except asyncio.TimeoutError:
            raise self._closed(f"timed out connecting to the engine after {timeout:g}s") from None
        except OSError as exc:
            raise self._closed(f"cannot connect to the engine: {_os_reason(exc)}") from None
        except (UnicodeError, ValueError, OverflowError, TypeError):
            # A host the resolver cannot encode (IDNA) and similar: the text may hold the host.
            raise self._closed(invalid) from None
        self._broken = ""

    async def write_line(self, text: str, timeout: float | None = None) -> None:
        """Send one line, waiting at most ``timeout`` (default :attr:`send_timeout`) for the
        engine to accept it; past that the connection is broken and dropped."""
        check_line(text)
        try:
            data = (text + "\n").encode(self.encoding)
        except UnicodeEncodeError:
            raise EngineError("refused a command that cannot be encoded as UTF-8 "
                              "(a lone surrogate)", address=self.address) from None
        if self._broken:
            raise EngineError(self._broken, address=self.address)
        if self._writer is None or self._writer.is_closing():
            raise self._closed("the engine connection is closed")
        limit = self.send_timeout if timeout is None else timeout
        try:
            await asyncio.wait_for(self._send(data), limit)
        except asyncio.TimeoutError:
            reason = f"the engine did not accept data within {limit:g}s"
            self._broken = f"{reason}; the connection is unusable"
            self._abort()
            raise EngineError(reason, address=self.address) from None

    async def _send(self, data: bytes) -> None:
        async with self._write_lock:
            writer = self._writer
            if writer is None or writer.is_closing():
                raise self._closed("the engine connection is closed")
            try:
                writer.write(data)
                await writer.drain()
            except (ConnectionError, OSError) as exc:
                raise self._closed(f"lost the engine connection while writing: "
                                   f"{_os_reason(exc)}") from None

    def _abort(self) -> None:
        """Drop the connection at once, discarding whatever is still buffered."""
        writer = self._writer
        if writer is not None:
            writer.transport.abort()

    def abort(self) -> None:
        """Drop the connection at once, without awaiting anything; it is then closed."""
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.transport.abort()

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
            if self._broken:  # we dropped it ourselves (a stalled send)
                raise EngineError(self._broken, address=self.address) from None
            raise self._closed(f"lost the engine connection: {_os_reason(exc)}") from None
        if not raw.endswith(b"\n"):
            if self._broken:
                raise EngineError(self._broken, address=self.address)
            raise self._closed("the engine closed the connection")
        return raw.decode(self.encoding, errors="replace").rstrip("\r\n")

    async def close(self) -> None:
        """Close gracefully if the buffered data goes out within :data:`CLOSE_GRACE`; otherwise
        (or when the connection is broken) drop it, so closing never waits on a peer that does
        not read."""
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        if self._broken:
            writer.transport.abort()
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), CLOSE_GRACE)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            writer.transport.abort()
