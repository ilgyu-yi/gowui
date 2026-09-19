"""Fixtures: fake engines (SPEC §2.6), connected clients, sessions and the running web app."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

#: Every "does not hang" check waits at most this long (seconds); mirrors helpers.HANG.
HANG = 5.0


@pytest.fixture
async def fake_engine():
    """``await fake_engine(protocol, **options)`` starts a fake engine, stopped at teardown."""
    from fake_engine import start_fake_engine

    servers = []

    async def start(protocol: str, **options):
        server = await asyncio.wait_for(start_fake_engine(protocol, **options), HANG)
        servers.append(server)
        return server

    yield start
    for server in servers:
        await asyncio.wait_for(server.stop(), HANG)


@pytest.fixture
async def gtp_server(fake_engine):
    return await fake_engine("gtp")


@pytest.fixture
async def analysis_server(fake_engine):
    return await fake_engine("analysis")


@pytest.fixture
async def handol_server(fake_engine):
    return await fake_engine("handol")


@pytest.fixture
async def connect():
    """``await connect(server, log=None)`` returns a connected client, closed at teardown."""
    from gowui.engine import create_engine

    engines = []

    async def make(server, log=None, protocol: str | None = None):
        engine = create_engine(protocol or server.protocol, "127.0.0.1", server.port, log)
        engines.append(engine)
        await asyncio.wait_for(engine.connect(), HANG)
        return engine

    yield make
    for engine in engines:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(engine.close(), HANG)


@pytest.fixture
async def make_session(fake_engine):
    """``make_session(resolver=None, expose_address=True)`` returns a session ``Harness``
    (tests/session_helpers.py), closed at teardown before the fake engines stop."""
    from session_helpers import Harness

    made = []

    def make(resolver=None, *, expose_address: bool = True):
        harness = Harness(resolver, expose_address=expose_address)
        made.append(harness)
        return harness

    yield make
    for harness in made:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(harness.session.aclose(), HANG)


@pytest.fixture
async def h(make_session):
    """A session under the typed (local) engine-address policy, on a fresh 9x9 game."""
    harness = make_session()
    await harness.new_game(9)
    return harness


# -- the web app (tests/app_helpers.py) -----------------------------------------------------------
@pytest.fixture
async def serve():
    """``await serve(app)`` serves ``app`` on 127.0.0.1 and a free port through the CLI's config
    builder and returns an ``app_helpers.Running``; every server is stopped at teardown."""
    from app_helpers import start_server

    running = []

    async def start(app):
        server = await start_server(app)
        running.append(server)
        return server

    yield start
    for server in reversed(running):
        with contextlib.suppress(Exception):
            await server.stop()


@pytest.fixture
async def tabs(serve):
    """``await tabs(running, host=None, origin=None, headers=None)`` opens a WebSocket tab
    (``app_helpers.open_tab``); every tab is closed at teardown, before the servers stop."""
    from app_helpers import open_tab

    opened = []

    async def open_(running, **kwargs):
        tab = await open_tab(running, **kwargs)
        if not isinstance(tab, int):
            opened.append(tab)
        return tab

    yield open_
    for tab in opened:
        await tab.close()


@pytest.fixture
async def local_app(serve):
    """The local app (local policies, memory storage), running."""
    from app_helpers import local_bundle

    from gowui.app import create_app

    return await serve(create_app(local_bundle()))
