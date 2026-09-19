"""The line transport (SPEC §2.1): one error kind, no address in the text, bounded input, one line
out, nothing hangs on a dead socket."""

from __future__ import annotations

import asyncio
import contextlib
import socket

import pytest

from gowui.engine import ConnectionClosed, EngineError
from gowui.engine.transport import LineConnection
from helpers import HANG

MIB = 1024 * 1024
HOST = "127.0.0.1"


def closed_port() -> int:
    """A port on 127.0.0.1 with nothing listening on it."""
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


class Peer:
    """A one-shot TCP server; ``script(reader, writer)`` plays the far end."""

    def __init__(self, script=None) -> None:
        self.script = script
        self.received = bytearray()
        self.port = 0
        self._server = None
        self._writers: list[asyncio.StreamWriter] = []

    async def __aenter__(self) -> "Peer":
        async def handle(reader, writer):
            self._writers.append(writer)
            if self.script is not None:
                await self.script(reader, writer)
            else:
                while chunk := await reader.read(65536):
                    self.received.extend(chunk)

        self._server = await asyncio.start_server(handle, HOST, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        # A connection the loop accepted but not yet handed to handle() is in no list here; let it
        # arrive, else (Python 3.12+) wait_closed() waits for it forever.
        for _ in range(5):
            await asyncio.sleep(0)
        self._server.close()
        if hasattr(self._server, "close_clients"):  # Python 3.13+
            self._server.close_clients()
        for writer in self._writers:
            writer.close()
        await asyncio.wait_for(self._server.wait_closed(), HANG)


def sends(*chunks: bytes, then_close: bool = False, hold: float = 30.0):
    async def script(reader, writer):
        for chunk in chunks:
            writer.write(chunk)
            await writer.drain()
        if then_close:
            writer.close()
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(reader.read(), hold)
    return script


async def connected(port: int) -> LineConnection:
    conn = LineConnection(HOST, port)
    await asyncio.wait_for(conn.connect(timeout=HANG), HANG)
    return conn


async def refused() -> tuple[EngineError, int]:
    port = closed_port()
    try:
        await asyncio.wait_for(LineConnection(HOST, port).connect(timeout=HANG), HANG)
    except EngineError as error:
        return error, port
    pytest.fail("connecting to a closed port did not raise")


# -- connecting -----------------------------------------------------------------------
async def test_refused_connect_is_connection_closed():
    error, _ = await refused()
    assert isinstance(error, ConnectionClosed)


async def test_refused_connect_message_has_no_host():
    error, _ = await refused()
    assert HOST not in str(error)


async def test_refused_connect_message_has_no_port():
    error, port = await refused()
    assert str(port) not in str(error)


async def test_refused_connect_carries_the_address_separately():
    error, port = await refused()
    assert error.address == (HOST, port)


async def test_connect_timeout_is_an_engine_error(monkeypatch):
    async def never(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(asyncio, "open_connection", never)
    async with Peer() as peer:
        with pytest.raises(EngineError):
            await asyncio.wait_for(LineConnection(HOST, peer.port).connect(timeout=0.2), HANG)


async def test_connect_timeout_message_has_no_address(monkeypatch):
    async def never(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(asyncio, "open_connection", never)
    async with Peer() as peer:
        try:
            await asyncio.wait_for(LineConnection(HOST, peer.port).connect(timeout=0.2), HANG)
        except EngineError as error:
            text = str(error)
        else:
            pytest.fail("the connect did not time out")
    assert HOST not in text and str(peer.port) not in text


async def test_connected_after_connect():
    async with Peer() as peer:
        conn = await connected(peer.port)
        try:
            assert conn.connected
        finally:
            await conn.close()


async def test_not_connected_after_close():
    async with Peer() as peer:
        conn = await connected(peer.port)
        await conn.close()
        assert not conn.connected


# -- reading --------------------------------------------------------------------------
async def test_read_line_strips_crlf():
    async with Peer(sends(b"= FakeKataGo\r\n")) as peer:
        conn = await connected(peer.port)
        try:
            assert await conn.read_line(timeout=HANG) == "= FakeKataGo"
        finally:
            await conn.close()


async def test_read_line_decodes_utf8():
    async with Peer(sends("흑 백 ☗\n".encode())) as peer:
        conn = await connected(peer.port)
        try:
            assert await conn.read_line(timeout=HANG) == "흑 백 ☗"
        finally:
            await conn.close()


async def test_eof_mid_read_is_connection_closed():
    async with Peer(sends(b"partial", then_close=True)) as peer:
        conn = await connected(peer.port)
        try:
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(conn.read_line(), HANG)
        finally:
            await conn.close()


async def test_read_timeout_is_an_engine_error():
    async with Peer(sends()) as peer:
        conn = await connected(peer.port)
        try:
            with pytest.raises(EngineError):
                await asyncio.wait_for(conn.read_line(timeout=0.2), HANG)
        finally:
            await conn.close()


async def test_read_after_close_is_connection_closed():
    async with Peer() as peer:
        conn = await connected(peer.port)
        await conn.close()
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(conn.read_line(timeout=HANG), HANG)


# -- bounded input ----------------------------------------------------------------------
async def test_a_line_well_under_one_mib_is_read():
    async with Peer(sends(b"x" * 1_000_000 + b"\n")) as peer:
        conn = await connected(peer.port)
        try:
            assert len(await asyncio.wait_for(conn.read_line(timeout=HANG), HANG)) == 1_000_000
        finally:
            await conn.close()


async def test_a_line_over_one_mib_is_an_engine_error():
    async with Peer(sends(b"x" * (MIB + 1) + b"\n")) as peer:
        conn = await connected(peer.port)
        try:
            with pytest.raises(EngineError):
                await asyncio.wait_for(conn.read_line(timeout=HANG), HANG)
        finally:
            await conn.close()


async def test_a_line_over_one_mib_without_a_terminator_is_an_engine_error():
    async with Peer(sends(b"x" * (2 * MIB))) as peer:
        conn = await connected(peer.port)
        try:
            with pytest.raises(EngineError):
                await asyncio.wait_for(conn.read_line(timeout=HANG), HANG)
        finally:
            await conn.close()


async def test_overflow_leaves_the_connection_unusable():
    async with Peer(sends(b"x" * (MIB + 1) + b"\nok\n")) as peer:
        conn = await connected(peer.port)
        try:
            with contextlib.suppress(EngineError):
                await asyncio.wait_for(conn.read_line(timeout=HANG), HANG)
            with pytest.raises(EngineError):
                await asyncio.wait_for(conn.read_line(timeout=HANG), 1.0)
        finally:
            await conn.close()


# -- writing -----------------------------------------------------------------------------
async def test_write_line_sends_one_terminated_line():
    async with Peer() as peer:
        conn = await connected(peer.port)
        await conn.write_line("1 name")
        await conn.close()
        await asyncio.sleep(0.1)
    assert bytes(peer.received) == b"1 name\n"


async def test_write_after_close_is_connection_closed():
    async with Peer() as peer:
        conn = await connected(peer.port)
        await conn.close()
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(conn.write_line("1 name"), HANG)


@pytest.mark.parametrize("text", ["name\nquit", "name\rquit", "name\x00quit", "name\r\n"])
async def test_write_line_refuses_more_than_one_line(text):
    async with Peer() as peer:
        conn = await connected(peer.port)
        try:
            with pytest.raises(EngineError):
                await asyncio.wait_for(conn.write_line(text), HANG)
        finally:
            await conn.close()


async def test_a_refused_line_sends_nothing():
    async with Peer() as peer:
        conn = await connected(peer.port)
        with contextlib.suppress(EngineError):
            await conn.write_line("name\nquit")
        await conn.close()
        await asyncio.sleep(0.1)
    assert bytes(peer.received) == b""


async def test_a_line_that_cannot_be_encoded_is_an_engine_error():
    async with Peer() as peer:
        conn = await connected(peer.port)
        try:
            with pytest.raises(EngineError):
                await asyncio.wait_for(conn.write_line("kata-set-rules \ud800"), HANG)
        finally:
            await conn.close()


async def test_a_line_that_cannot_be_encoded_sends_nothing():
    async with Peer() as peer:
        conn = await connected(peer.port)
        with contextlib.suppress(EngineError):
            await conn.write_line("name \ud800")
        await conn.write_line("1 name")
        await conn.close()
        await asyncio.sleep(0.1)
    assert bytes(peer.received) == b"1 name\n"


# -- valid address ----------------------------------------------------------------------------
BAD_ADDRESSES = [
    ("", 6363),
    ("bad\x00host", 6363),
    ("a" * 254, 6363),
    ("a" * 64 + ".example", 6363),  # a label the resolver cannot encode (IDNA)
    (None, 6363),
    (HOST, 0),
    (HOST, 70000),
    (HOST, -1),
    (HOST, True),
    (HOST, "6363"),
    (HOST, 63.5),
]
BAD_IDS = ["empty-host", "nul-in-host", "host-over-253", "idna-label-too-long", "host-none",
           "port-0", "port-70000", "port-negative", "port-bool", "port-str", "port-float"]


@pytest.mark.parametrize("host, port", BAD_ADDRESSES, ids=BAD_IDS)
async def test_an_invalid_address_is_an_engine_error(host, port):
    with pytest.raises(EngineError):
        await asyncio.wait_for(LineConnection(host, port).connect(timeout=HANG), HANG)


@pytest.mark.parametrize("host, port", BAD_ADDRESSES, ids=BAD_IDS)
async def test_an_invalid_address_error_names_no_address(host, port):
    try:
        await asyncio.wait_for(LineConnection(host, port).connect(timeout=HANG), HANG)
    except EngineError as error:
        text = str(error)
        assert (bool(host) and isinstance(host, str) and host in text, str(port) in text) == (
            False, False)
    else:
        pytest.fail("an invalid address connected")


@pytest.mark.parametrize("case", ["port-str", "host-none"])
async def test_an_invalid_address_is_refused_even_when_something_listens_there(case):
    # The OS would accept a string port or a missing host against a live listener, so only the
    # address check can refuse these.
    async with Peer() as peer:
        host, port = (HOST, str(peer.port)) if case == "port-str" else (None, peer.port)
        with pytest.raises(EngineError):
            await asyncio.wait_for(LineConnection(host, port).connect(timeout=HANG), HANG)


# -- bounded sends ----------------------------------------------------------------------------
async def never_reads(reader, writer):
    await asyncio.sleep(30)


#: More than a peer that never reads can absorb in socket buffers.
FLOOD = "x" * (16 * MIB)


async def test_a_send_the_peer_never_accepts_is_an_engine_error():
    async with Peer(never_reads) as peer:
        conn = await connected(peer.port)
        conn.send_timeout = 0.3
        try:
            with pytest.raises(EngineError):
                await asyncio.wait_for(conn.write_line(FLOOD), HANG)
        finally:
            await asyncio.wait_for(conn.close(), HANG)


async def test_a_send_the_peer_never_accepts_leaves_the_connection_unusable():
    async with Peer(never_reads) as peer:
        conn = await connected(peer.port)
        conn.send_timeout = 0.3
        with contextlib.suppress(EngineError):
            await asyncio.wait_for(conn.write_line(FLOOD), HANG)
        try:
            with pytest.raises(EngineError):
                await asyncio.wait_for(conn.write_line("1 name"), 0.5)
            assert not conn.connected
        finally:
            await asyncio.wait_for(conn.close(), HANG)


async def test_close_does_not_wait_on_a_peer_that_never_reads():
    async with Peer(never_reads) as peer:
        conn = await connected(peer.port)
        conn.send_timeout = 30.0
        sending = asyncio.create_task(conn.write_line(FLOOD))
        await asyncio.sleep(0.3)
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.wait_for(conn.close(), HANG)
        elapsed = loop.time() - start
        await asyncio.wait_for(asyncio.gather(sending, return_exceptions=True), HANG)
    assert elapsed < 1.5


# -- abort (§3.2: a close cut short) --------------------------------------------------------
async def test_abort_drops_the_connection_at_once():
    ended = asyncio.Event()

    async def until_dropped(reader, writer):
        with contextlib.suppress(ConnectionError, OSError):
            await reader.read()
        ended.set()

    async with Peer(until_dropped) as peer:
        conn = await connected(peer.port)
        conn.abort()
        conn.abort()  # twice is harmless
        await asyncio.wait_for(ended.wait(), 1.0)
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(conn.read_line(), HANG)
        await asyncio.wait_for(conn.close(), HANG)
    assert not conn.connected
