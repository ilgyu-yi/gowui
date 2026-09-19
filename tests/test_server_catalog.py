"""Server mode: engines only from the catalog, and no catalog address ever reaches a browser
(SPEC §5, §6.3, §7.7, §10; issue #9 AC2).

A session that connects through the catalog, plays, analyses, uses the console and fails a
connect to a dead entry is recorded: every WebSocket frame and every HTTP body it received. None of
them names a catalog host or port. A ``connect`` carrying an address instead of a catalog id is
refused with the fixed text.
"""

from __future__ import annotations

import re

from helpers import HANG
from server_helpers import catalog, get, login, start, ws_headers
from session_helpers import free_port
from test_app_local import play

REFUSAL = "choose an engine from the list"


def leaks(text: str, host: str, *ports: int) -> list[str]:
    found = []
    if host.lower() in text.lower():
        found.append(host)
    for port in ports:
        if re.search(rf"(?<![0-9.]){port}(?![0-9])", text):
            found.append(str(port))
    return found


async def setup(serve, tabs, tmp_path, fake_engine):
    engine = await fake_engine("gtp")
    dead = free_port()
    entries = catalog(
        {"id": "kata", "label": "KataGo", "protocol": "gtp", "host": "localhost",
         "port": engine.port, "console": True},
        {"id": "gone", "protocol": "gtp", "host": "localhost", "port": dead})
    server = await start(serve, tmp_path, engines=entries)
    _, token = await login(server.running, "alice")
    tab = await tabs(server.running, origin=server.running.origin, headers=ws_headers(token))
    assert not isinstance(tab, int)
    await tab.wait_state()
    return server, tab, token, engine, dead


async def test_no_payload_names_a_catalog_host_or_port(serve, tabs, tmp_path, fake_engine):
    server, tab, token, engine, dead = await setup(serve, tabs, tmp_path, fake_engine)
    running = server.running
    bodies: list[str] = []

    health = await get(running, "/api/health", token)
    bodies.append(health.text)

    start_mark = tab.mark()
    await tab.send({"type": "connect", "engineId": "kata"})
    assert await tab.wait_state(lambda f: f["engine"]["connected"], start_mark) is not None
    await tab.send({"type": "engine_params", "maxVisits": 50})
    await play(tab, "C3", "G7")
    mark = tab.mark()
    await tab.send({"type": "analysis", "enabled": True})
    assert await tab.wait("analysis", start=mark) is not None
    await tab.send({"type": "raw", "command": "name"})
    await tab.send({"type": "genmove"})
    await tab.wait_state(lambda f: f["game"]["moveCount"] >= 3, mark)
    mark = tab.mark()
    await tab.send({"type": "connect", "engineId": "gone"})
    await tab.wait_state(lambda f: not f["engine"]["connected"], mark)
    await tab.wait("error", start=mark, timeout=HANG)

    for path in ("/api/sgf", "/api/health", "/api/nothing", "/login"):
        bodies.append((await get(running, path, token)).text)
    async with running.client() as client:
        bad = await client.post("/api/sgf", content="not an sgf",
                                headers={"Host": running.host, "Origin": running.origin,
                                         "Cookie": f"gowui_session={token}"})
    bodies.append(bad.text)
    bodies.append((await get(running, "/api/health")).text)  # the 401 body

    received = tab.texts + bodies
    found = [(i, leaks(text, "localhost", engine.port, dead)) for i, text in enumerate(received)]
    assert [f for f in found if f[1]] == []


async def test_health_carries_only_the_public_catalog(serve, tabs, tmp_path, fake_engine):
    server, tab, token, engine, dead = await setup(serve, tabs, tmp_path, fake_engine)
    info = (await get(server.running, "/api/health", token)).json()
    assert info["engineAddress"] == {"kind": "catalog", "engines": [
        {"id": "kata", "label": "KataGo", "protocol": "gtp", "console": True},
        {"id": "gone", "label": "gone", "protocol": "gtp", "console": False}]}
    assert info["console"] is True


async def test_a_connect_with_an_address_is_refused(serve, tabs, tmp_path, fake_engine):
    server, tab, token, engine, dead = await setup(serve, tabs, tmp_path, fake_engine)
    requests = [
        {"protocol": "gtp", "host": "127.0.0.1", "port": engine.port},
        {"engineId": "kata", "host": "127.0.0.1"},
        {"engineId": "kata", "port": engine.port},
        {"engineId": "nope"},
        {"engineId": 1},
        {},
    ]
    for request in requests:
        mark = tab.mark()
        await tab.send({"type": "connect", **request})
        error = await tab.wait("error", start=mark)
        assert error is not None and error["message"] == REFUSAL, request
    assert not tab.state()["engine"]["connected"]
