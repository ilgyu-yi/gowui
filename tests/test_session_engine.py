"""The session's engine side (SPEC §3.2, §3.4, §3.5, §3.6, §4.1, §4.2, §6.1, §6.3, §7.7): connecting
through the engine-address policy, the lost engine, engine work that never blocks or is cancelled
by a caller, lifecycle generations, one pending on-demand command per kind, the console, final
score, the traffic log, explicit max visits, handol-mux settings, and hidden addresses.

Observed through broadcast frames and the fake engine's request log. Some tests patch an engine
*client* method (never the session) because there is no other way to inject the fault: an
unexpected exception in auto-play (``GTPEngine.genmove``) or in ``final_score``
(``GTPEngine.final_score``), and a reply timeout short enough for a test
(``GTPEngine.genmove_timeout``); each says so.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from gowui.engine.gtp import GTPEngine
from helpers import gtp_commands, gtp_names, position_queries, wait_for
from session_helpers import (LOOPBACK, SENTINEL_HOST, CatalogResolver, free_port,
                             gowui_tasks, gtp_count, handol_requests, leaks, move_list,
                             random_sgf, route_sentinel_host, settle)

SLOW = 1.0


async def slow_gtp(fake_engine, delay: float = SLOW, **options):
    return await fake_engine("gtp", delay={"genmove": delay, "kata-genmove_analyze": delay},
                             **options)


async def engine_plays_white(h, server) -> None:
    await h.connect_to(server)
    await h.send({"type": "players", "whiteIsEngine": True})


async def catalog_session(make_session, *, expose_address: bool = False,
                          host: str = LOOPBACK):
    catalog = CatalogResolver(host)
    h = make_session(catalog, expose_address=expose_address)
    await h.new_game(9)
    return h, catalog


def engine_view(state: dict, *keys: str) -> tuple:
    return tuple(state["engine"][k] for k in keys)


# -- connecting through the engine-address policy (§6.1, §6.3) -------------------------------------
async def test_a_typed_request_connects_and_reports_the_engine(h, gtp_server):
    state = await h.connect_to(gtp_server)
    request = {"protocol": "gtp", "host": LOOPBACK, "port": gtp_server.port}
    assert engine_view(state, "connected", "protocol", "name", "version", "request",
                       "supportsGenmove", "supportsFinalScore", "console") == \
        (True, "gtp", "FakeKataGo", "0.0-fake", request, True, True, True)


async def test_an_analysis_engine_offers_neither_final_score_nor_console(h, analysis_server):
    state = await h.connect_to(analysis_server)
    assert engine_view(state, "supportsGenmove", "supportsFinalScore", "console") == \
        (True, False, False)


async def test_a_catalog_entry_without_console_offers_no_console(make_session, gtp_server):
    h, catalog = await catalog_session(make_session)
    catalog.add("kata", "gtp", gtp_server.port, console=False)
    state = await h.connect({"engineId": "kata"})
    assert engine_view(state, "console", "request") == (False, {"engineId": "kata"})


async def test_a_request_the_policy_refuses_is_reported_with_its_message(h):
    start = h.rec.mark()
    await h.send({"type": "connect", "protocol": "smoke-signals", "host": LOOPBACK, "port": 1})
    message = await h.rec.wait_error(start)
    assert message is not None and "the protocol must be gtp, analysis or handol" in message


async def test_a_refused_request_leaves_the_space_disconnected(make_session):
    h, catalog = await catalog_session(make_session)
    await h.send({"type": "connect", "engineId": "kata", "host": "10.0.0.1", "port": 6363})
    assert (await h.fresh_state())["engine"]["connected"] is False


async def test_a_connection_failure_names_the_address_when_addresses_are_shown(h):
    port = free_port()
    start = h.rec.mark()
    await h.send({"type": "connect", "protocol": "gtp", "host": LOOPBACK, "port": port})
    message = await h.rec.wait_error(start)
    assert message is not None and str(port) in message


async def test_a_connection_failure_leaves_the_space_disconnected(h):
    start = h.rec.mark()
    await h.send({"type": "connect", "protocol": "gtp", "host": LOOPBACK, "port": free_port()})
    assert await h.rec.wait_error(start) is not None
    assert (await h.fresh_state())["engine"]["connected"] is False


async def test_disconnect_closes_the_engine_connection(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.send({"type": "disconnect"})
    assert await wait_for(lambda: gtp_server.open_connections == 0)


async def test_disconnect_shows_the_engine_disconnected(h, gtp_server):
    await h.connect_to(gtp_server)
    start = h.rec.mark()
    await h.send({"type": "disconnect"})
    assert await h.rec.wait_state(lambda f: not f["engine"]["connected"], start)


# -- the lost engine (§3.2) ------------------------------------------------------------------------
async def test_a_lost_engine_during_self_play_shows_disconnected(h, fake_engine):
    server = await fake_engine("gtp", delay={"genmove": 0.1})
    await h.connect_to(server)
    await h.send({"type": "players", "blackIsEngine": True, "whiteIsEngine": True})
    await h.wait_moves(2)
    start = h.rec.mark()
    await server.stop()
    state = await h.rec.wait_state(lambda f: not f["engine"]["connected"] and not f["thinking"],
                                   start)
    assert state is not None and state["status"] != ""


async def test_self_play_resumes_after_reconnecting_to_a_new_engine(h, fake_engine):
    server = await fake_engine("gtp", delay={"genmove": 0.1})
    await h.connect_to(server)
    await h.send({"type": "players", "blackIsEngine": True, "whiteIsEngine": True})
    await h.wait_moves(2)
    start = h.rec.mark()
    await server.stop()
    assert await h.rec.wait_state(lambda f: not f["engine"]["connected"], start)
    count = (await h.fresh_state())["game"]["moveCount"]
    await h.connect_to(await fake_engine("gtp"))
    await h.wait_moves(count + 2)


async def test_analysis_resumes_after_reconnecting_to_a_new_engine(h, fake_engine):
    server = await fake_engine("gtp", hangup_after_reports=3)
    await h.connect_to(server)
    start = h.rec.mark()
    await h.send({"type": "analysis", "enabled": True})
    assert await h.rec.wait_state(lambda f: not f["engine"]["connected"], start)
    replacement = await fake_engine("gtp")
    later = h.rec.mark()
    await h.connect_to(replacement)
    assert await h.rec.wait("analysis", start=later) is not None


async def lose_the_engine_mid_genmove(h, fake_engine) -> int:
    server = await fake_engine("gtp", hangup_on="genmove")
    await h.connect_to(server)
    start = h.rec.mark()
    await h.send({"type": "genmove"})
    assert await h.rec.wait_state(lambda f: not f["engine"]["connected"], start)
    await settle(0.8)
    return start


async def test_a_connection_lost_mid_genmove_shows_disconnected_once(h, fake_engine):
    start = await lose_the_engine_mid_genmove(h, fake_engine)
    before = [f for f in h.rec.frames[:start] if f.get("type") == "state"][-1]
    shown = [before["engine"]["connected"]] + [f["engine"]["connected"]
                                               for f in h.rec.states(start)]
    drops = sum(1 for was, now in zip(shown, shown[1:]) if was and not now)
    assert drops == 1


async def test_a_connection_lost_mid_genmove_raises_at_most_one_error(h, fake_engine):
    start = await lose_the_engine_mid_genmove(h, fake_engine)
    assert len(h.rec.errors(start)) <= 1


# -- engine work never blocks a caller (§3.2) ------------------------------------------------------
async def cancel_mid_command(h, server, message: dict, command: str) -> None:
    """Start ``handle(message)`` and cancel the caller once the fake has the command."""
    task = asyncio.ensure_future(h.session.handle(message))
    assert await wait_for(lambda: gtp_count(server, command) >= 1 or task.done())
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_cancelling_the_caller_mid_genmove_still_plays_the_move(h, fake_engine):
    server = await slow_gtp(fake_engine)
    await h.connect_to(server)
    await cancel_mid_command(h, server, {"type": "genmove"}, "genmove")
    await h.wait_moves(1)


async def test_cancelling_the_caller_mid_genmove_leaves_the_engine_usable(h, fake_engine):
    server = await slow_gtp(fake_engine)
    await h.connect_to(server)
    await cancel_mid_command(h, server, {"type": "genmove"}, "genmove")
    await h.wait_moves(1)
    start = h.rec.mark()
    await h.send({"type": "genmove"})
    await h.wait_moves(2, start)


async def test_cancelling_the_caller_mid_raw_leaves_the_engine_usable(h, fake_engine):
    server = await fake_engine("gtp", delay={"showboard": SLOW})
    await h.connect_to(server)
    await cancel_mid_command(h, server, {"type": "raw", "command": "showboard"}, "showboard")
    await settle(SLOW + 0.3)
    start = h.rec.mark()
    await h.send({"type": "raw", "command": "name"})
    assert await h.rec.wait("log", lambda f: "FakeKataGo" in f["line"]["text"], start)


@pytest.mark.parametrize("message", [
    {"type": "board_select"},
    {"type": "new_game", "size": 13, "komi": None, "rules": "japanese", "handicap": 0},
], ids=["board_select", "new_game"])
async def test_a_state_change_during_a_long_search_applies_at_once(h, fake_engine, message):
    server = await slow_gtp(fake_engine, delay=3.0)
    await h.connect_to(server)
    first = h.rec.state()["activeBoard"]
    start = h.rec.mark()
    await h.send({"type": "board_duplicate", "id": first})
    assert await h.rec.wait_state(lambda f: f["activeBoard"] != first, start)
    await h.send({"type": "genmove"})
    assert await wait_for(lambda: gtp_count(server, "genmove") == 1)
    if message["type"] == "board_select":
        message = {**message, "id": first}
        applied = lambda f: f["activeBoard"] == first  # noqa: E731
    else:
        applied = lambda f: f["game"]["size"] == 13  # noqa: E731
    loop = asyncio.get_running_loop()
    start, began = h.rec.mark(), loop.time()
    await h.send(message)
    elapsed = loop.time() - began
    shown = await h.rec.wait_state(applied, start, timeout=0.5)
    assert (elapsed < 0.5, shown is not None) == (True, True)


# -- lifecycle generations and one pending command per kind (§3.2, §4.1, §6.3) ---------------------
async def test_a_second_connect_while_one_is_pending_is_refused(h, fake_engine):
    server = await fake_engine("gtp", delay={"name": SLOW})
    request = {"type": "connect", "protocol": "gtp", "host": LOOPBACK, "port": server.port}
    start = h.rec.mark()
    await h.send(request)
    await h.send(request)
    assert await h.rec.wait_error(start, timeout=0.8) is not None


async def test_two_concurrent_connects_leave_one_engine_connection(h, fake_engine):
    server = await fake_engine("gtp", delay={"name": SLOW})
    request = {"type": "connect", "protocol": "gtp", "host": LOOPBACK, "port": server.port}
    await h.send(request)
    await h.send(request)
    await settle(SLOW + 1.0)
    assert server.open_connections == 1


async def test_disconnect_during_a_pending_connect_wins(h, fake_engine):
    server = await fake_engine("gtp", delay={"name": SLOW})
    await h.send({"type": "connect", "protocol": "gtp", "host": LOOPBACK, "port": server.port})
    assert await wait_for(lambda: server.open_connections == 1)
    await h.send({"type": "disconnect"})
    await settle(SLOW + 1.0)
    assert (await h.fresh_state())["engine"]["connected"] is False


async def test_disconnect_during_a_pending_connect_closes_its_socket(h, fake_engine):
    server = await fake_engine("gtp", delay={"name": SLOW})
    await h.send({"type": "connect", "protocol": "gtp", "host": LOOPBACK, "port": server.port})
    assert await wait_for(lambda: server.open_connections == 1)
    await h.send({"type": "disconnect"})
    await settle(SLOW + 1.0)
    assert server.open_connections == 0


async def test_reconnecting_mid_search_raises_no_spurious_error(h, fake_engine):
    server = await slow_gtp(fake_engine, delay=1.5)
    await h.connect_to(server)
    await h.send({"type": "genmove"})
    assert await wait_for(lambda: gtp_count(server, "genmove") == 1)
    start = h.rec.mark()
    await h.send({"type": "connect", "protocol": "gtp", "host": LOOPBACK, "port": server.port})
    await settle(2.5)
    assert h.rec.errors(start) == []


async def test_reconnecting_mid_search_discards_the_old_engines_move(h, fake_engine):
    server = await slow_gtp(fake_engine, delay=1.5)
    await h.connect_to(server)
    await h.send({"type": "genmove"})
    assert await wait_for(lambda: gtp_count(server, "genmove") == 1)
    await h.send({"type": "connect", "protocol": "gtp", "host": LOOPBACK, "port": server.port})
    await settle(2.5)
    assert move_list(await h.fresh_state()) == []


async def test_a_second_genmove_while_one_runs_is_refused(h, fake_engine):
    server = await slow_gtp(fake_engine)
    await h.connect_to(server)
    await h.send({"type": "genmove"})
    assert await wait_for(lambda: gtp_count(server, "genmove") == 1)
    start = h.rec.mark()
    await h.send({"type": "genmove"})
    assert await h.rec.wait_error(start, timeout=0.5) is not None


async def test_a_refused_second_genmove_never_reaches_the_engine(h, fake_engine):
    server = await slow_gtp(fake_engine)
    await h.connect_to(server)
    await h.send({"type": "genmove"})
    assert await wait_for(lambda: gtp_count(server, "genmove") == 1)
    await h.send({"type": "genmove"})
    await settle(2 * SLOW + 0.5)
    assert gtp_count(server, "genmove") == 1


@pytest.mark.parametrize("kind", ["raw", "final_score"])
async def test_a_second_console_or_score_command_while_one_runs_is_refused(h, fake_engine, kind):
    command = "showboard" if kind == "raw" else "final_score"
    server = await fake_engine("gtp", delay={command: SLOW})
    await h.connect_to(server)
    message = {"type": "raw", "command": "showboard"} if kind == "raw" else {"type": kind}
    await h.send(message)
    assert await wait_for(lambda: gtp_count(server, command) == 1)
    start = h.rec.mark()
    await h.send(message)
    assert await h.rec.wait_error(start, timeout=0.5) is not None


# -- unexpected failures and a broken broadcast (§3.2) ---------------------------------------------
def fail_first_genmove(monkeypatch) -> None:
    """Patches the engine client (not the session): the first engine move -- ``genmove`` or
    ``genmove_analyze``, whichever the session uses -- raises a non-engine error."""
    calls = {"n": 0}

    def failing(real):
        async def wrapper(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ZeroDivisionError("injected by the test")
            return await real(self, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(GTPEngine, "genmove", failing(GTPEngine.genmove))
    monkeypatch.setattr(GTPEngine, "genmove_analyze", failing(GTPEngine.genmove_analyze))


async def test_an_unexpected_failure_in_auto_play_clears_thinking(h, gtp_server, monkeypatch):
    fail_first_genmove(monkeypatch)
    await engine_plays_white(h, gtp_server)
    start = h.rec.mark()
    await h.send({"type": "play", "color": "black", "vertex": "D4"})
    assert await h.rec.wait_error(start) is not None
    await settle(0.3)
    assert h.rec.state()["thinking"] is False


async def test_an_unexpected_failure_in_auto_play_is_reported_once(h, gtp_server, monkeypatch):
    fail_first_genmove(monkeypatch)
    await engine_plays_white(h, gtp_server)
    start = h.rec.mark()
    await h.send({"type": "play", "color": "black", "vertex": "D4"})
    await settle(1.0)
    assert len(h.rec.errors(start)) == 1


async def test_an_unexpected_failure_stops_auto_play(h, gtp_server, monkeypatch):
    fail_first_genmove(monkeypatch)
    await engine_plays_white(h, gtp_server)
    await h.send({"type": "play", "color": "black", "vertex": "D4"})
    await settle(1.0)
    assert move_list(await h.fresh_state()) == ["D4"]


async def test_toggling_the_players_restarts_auto_play_after_a_failure(h, gtp_server, monkeypatch):
    fail_first_genmove(monkeypatch)
    await engine_plays_white(h, gtp_server)
    start = h.rec.mark()
    await h.send({"type": "play", "color": "black", "vertex": "D4"})
    assert await h.rec.wait_error(start) is not None
    await h.send({"type": "players", "whiteIsEngine": False})
    await h.send({"type": "players", "whiteIsEngine": True})
    await h.wait_moves(2, start)


async def test_a_raising_broadcast_does_not_escape_handle(h):
    h.rec.fail = True
    try:
        await h.send({"type": "play", "color": "black", "vertex": "D4"})
        escaped = None
    except Exception as exc:  # noqa: BLE001 - the point of the test
        escaped = exc
    h.rec.fail = False
    assert escaped is None


async def test_a_raising_broadcast_does_not_stop_the_session(h):
    h.rec.fail = True
    await h.send({"type": "play", "color": "black", "vertex": "D4"})
    h.rec.fail = False
    await h.fresh_state()  # the lost frames left the recorder a move behind
    state = await h.play("E5")
    assert move_list(state) == ["D4", "E5"]


async def test_engine_work_survives_a_raising_broadcast(h, gtp_server):
    await h.connect_to(gtp_server)
    h.rec.fail = True
    await h.send({"type": "genmove"})
    await settle(1.0)
    h.rec.fail = False
    assert (await h.fresh_state())["game"]["moveCount"] == 1


# -- aclose (§3.2, #9 idle release) ----------------------------------------------------------------
async def test_aclose_during_self_play_leaves_no_pending_task(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.send({"type": "players", "blackIsEngine": True, "whiteIsEngine": True})
    await h.wait_moves(2)
    await h.aclose()
    await asyncio.sleep(0)
    assert gowui_tasks() == []


async def test_aclose_closes_the_engine_connection(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.aclose()
    assert await wait_for(lambda: gtp_server.open_connections == 0)


async def test_aclose_twice_is_harmless(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.aclose()
    await h.aclose()
    assert gowui_tasks() == []


# -- the console (§3.5, §7.6) ----------------------------------------------------------------------
async def test_a_console_command_and_its_reply_reach_the_log(h, gtp_server):
    await h.connect_to(gtp_server)
    start = h.rec.mark()
    await h.send({"type": "raw", "command": "name"})
    assert await h.rec.wait(
        "log", lambda f: f["line"]["direction"] == "recv" and "FakeKataGo" in f["line"]["text"],
        start)


async def test_the_console_is_refused_for_an_engine_without_raw_commands(h, analysis_server):
    await h.connect_to(analysis_server)
    start = h.rec.mark()
    await h.send({"type": "raw", "command": "name"})
    assert await h.rec.wait_error(start) is not None


async def test_the_console_is_refused_when_the_policy_does_not_offer_it(make_session, gtp_server):
    h, catalog = await catalog_session(make_session)
    catalog.add("kata", "gtp", gtp_server.port, console=False)
    await h.connect({"engineId": "kata"})
    sent = len(gtp_commands(gtp_server))
    start = h.rec.mark()
    await h.send({"type": "raw", "command": "showboard"})
    assert await h.rec.wait_error(start) is not None
    await settle(0.3)
    assert "showboard" not in gtp_commands(gtp_server)[sent:]


async def test_the_console_is_refused_without_an_engine(h):
    start = h.rec.mark()
    await h.send({"type": "raw", "command": "name"})
    assert await h.rec.wait_error(start) is not None


async def test_a_console_command_of_1000_characters_is_sent(h, gtp_server):
    await h.connect_to(gtp_server)
    command = "known_command " + "x" * (1000 - len("known_command "))
    await h.send({"type": "raw", "command": command})
    assert await wait_for(lambda: command in gtp_commands(gtp_server))


@pytest.mark.parametrize("command", [
    "known_command " + "x" * (1001 - len("known_command ")),
    "name\nquit",
    "name\rquit",
    "name\x00",
    "kata-analyze 50",
    "lz-analyze 50",
    "kata-genmove_analyze b 50",
    "kata-search_analyze b 50",
], ids=["1001-chars", "LF", "CR", "NUL", "kata-analyze", "lz-analyze", "kata-genmove_analyze",
        "kata-search_analyze"])
async def test_a_console_command_that_is_too_long_multi_line_or_streaming_is_refused(
        h, gtp_server, command):
    await h.connect_to(gtp_server)
    start = h.rec.mark()
    await h.send({"type": "raw", "command": command})
    assert await h.rec.wait_error(start) is not None


@pytest.mark.parametrize("command", [
    "known_command " + "x" * (1001 - len("known_command ")),
    "kata-analyze 50",
    "kata-search_analyze b 50",
], ids=["1001-chars", "kata-analyze", "kata-search_analyze"])
async def test_a_refused_console_command_is_never_sent(h, gtp_server, command):
    await h.connect_to(gtp_server)
    sent = len(gtp_commands(gtp_server))
    await h.send({"type": "raw", "command": command})
    await settle(0.4)
    assert gtp_commands(gtp_server)[sent:] == []


async def test_analysis_restarts_after_a_console_command(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.send({"type": "analysis", "enabled": True})
    assert await h.rec.wait("analysis") is not None
    started = gtp_count(gtp_server, "kata-analyze")
    await h.send({"type": "raw", "command": "name"})
    assert await wait_for(lambda: gtp_count(gtp_server, "kata-analyze") > started)


async def test_the_engine_board_is_rebuilt_after_a_console_command(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.play("D4")
    await h.send({"type": "raw", "command": "clear_board"})
    assert await wait_for(lambda: "clear_board" in gtp_names(gtp_server))
    await settle(0.2)
    index = len(gtp_commands(gtp_server))
    await h.send({"type": "genmove"})
    await h.wait_moves(2)
    later = gtp_commands(gtp_server)[index:]
    assert "play B D4" in later[: later.index("genmove W")]


# -- final score (§3.5) ----------------------------------------------------------------------------
async def test_final_score_records_the_engines_result(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.play("D4")
    start = h.rec.mark()
    await h.send({"type": "final_score"})
    state = await h.rec.wait_state(lambda f: f["game"]["result"] != "", start)
    assert state is not None and state["game"]["result"] == "B+0.5"


async def test_final_score_is_refused_for_an_engine_without_it(h, analysis_server):
    await h.connect_to(analysis_server)
    start = h.rec.mark()
    await h.send({"type": "final_score"})
    assert await h.rec.wait_error(start) is not None


# -- the traffic log (§3.6, §4.2) ------------------------------------------------------------------
async def test_an_attached_tab_gets_the_last_100_log_lines(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.send({"type": "load_sgf", "sgf": random_sgf(80)})
    await h.wait_moves(80)
    await h.send({"type": "genmove"})
    await h.wait_moves(81)
    await settle(0.3)
    broadcast = [f["line"]["text"] for f in h.rec.of("log")]
    assert len(broadcast) > 100
    history = next(f for f in h.session.attach_frames() if f["type"] == "log_history")
    assert [line["text"] for line in history["lines"]] == broadcast[-100:]


# -- explicit max visits (§3.4) --------------------------------------------------------------------
def max_visits_before(commands: list[str], name: str) -> list[str]:
    """The ``kata-set-param maxVisits`` commands sent before the first command called ``name``."""
    names = [c.split()[0] for c in commands]
    head = commands[: names.index(name)]
    return [c for c in head if c.startswith("kata-set-param maxVisits")]


async def test_gtp_genmove_is_preceded_by_the_sessions_max_visits(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.send({"type": "engine_params", "maxVisits": 77})
    await h.send({"type": "genmove"})
    await h.wait_moves(1)
    assert max_visits_before(gtp_commands(gtp_server), "genmove")[-1:] == \
        ["kata-set-param maxVisits 77"]


async def test_gtp_genmove_gets_an_explicit_max_visits_by_default(h, gtp_server):
    state = await h.connect_to(gtp_server)
    await h.send({"type": "genmove"})
    await h.wait_moves(1)
    assert max_visits_before(gtp_commands(gtp_server), "genmove")[-1:] == \
        [f"kata-set-param maxVisits {state['settings']['maxVisits']}"]


async def test_gtp_analysis_is_preceded_by_the_sessions_max_visits(h, gtp_server):
    state = await h.connect_to(gtp_server)
    await h.send({"type": "analysis", "enabled": True})
    assert await h.rec.wait("analysis") is not None
    assert max_visits_before(gtp_commands(gtp_server), "kata-analyze")[-1:] == \
        [f"kata-set-param maxVisits {state['settings']['maxVisits']}"]


@pytest.mark.parametrize("asked, sent", [(123, 123), (5_000_000, 1_000_000), (0, 1)])
async def test_analysis_queries_carry_the_clamped_max_visits(h, analysis_server, asked, sent):
    await h.connect_to(analysis_server)
    await h.send({"type": "engine_params", "maxVisits": asked})
    await h.send({"type": "analysis", "enabled": True})
    assert await h.rec.wait("analysis") is not None
    assert position_queries(analysis_server)[-1]["maxVisits"] == sent


# -- handol-mux settings follow the active board (§3.3, §3.4) --------------------------------------
async def human(h, **fields) -> None:
    start = h.rec.mark()
    await h.send({"type": "human_params", **fields})
    assert await h.rec.wait_state(lambda f: True, start) is not None


async def human_request_for(server, moves: list) -> dict:
    """The latest human request the fake received for ``moves``."""
    assert await wait_for(lambda: any(r["moves"] == moves
                                      for r in handol_requests(server, "human")))
    return [r for r in handol_requests(server, "human") if r["moves"] == moves][-1]


async def test_handol_gets_the_boards_profile_on_connect(h, handol_server):
    await human(h, profile="rank_3k")
    await h.connect_to(handol_server)
    await h.send({"type": "analysis", "enabled": True})
    assert (await human_request_for(handol_server, []))["human"]["profile"] == "rank_3k"


async def test_handol_gets_a_changed_profile(h, handol_server):
    await h.connect_to(handol_server)
    await h.send({"type": "analysis", "enabled": True})
    await human_request_for(handol_server, [])
    await human(h, profile="rank_1d", policy={"min_p": 0.01})
    await h.play("D4")
    request = await human_request_for(handol_server, [["B", "D4"]])
    assert (request["human"]["profile"], request["human"]["policies"]) == \
        ("rank_1d", [{"min_p": 0.01}])


async def test_handol_gets_the_active_boards_settings_after_a_switch(h, handol_server):
    await h.connect_to(handol_server)
    await human(h, profile="rank_1d")
    first = h.rec.state()["activeBoard"]
    start = h.rec.mark()
    await h.send({"type": "board_duplicate", "id": first})
    assert await h.rec.wait_state(lambda f: f["activeBoard"] != first, start)
    await human(h, profile="proyear_2000")
    await h.send({"type": "board_select", "id": first})
    await h.send({"type": "analysis", "enabled": True})
    await h.play("D4")
    request = await human_request_for(handol_server, [["B", "D4"]])
    assert request["human"]["profile"] == "rank_1d"


async def test_handol_analysis_searches_with_the_sessions_max_visits(h, handol_server):
    await h.connect_to(handol_server)
    await h.send({"type": "engine_params", "maxVisits": 7})
    await h.send({"type": "analysis", "enabled": True})
    await h.play("D4")
    request = await human_request_for(handol_server, [["B", "D4"]])
    assert request["human"].get("search") == {"visits": 7}


async def test_handol_winrates_use_the_eval_visits(h, handol_server):
    await h.connect_to(handol_server)
    await human(h, evalVisits=33)
    await h.send({"type": "analysis", "enabled": True})
    await h.play("D4")
    assert await wait_for(lambda: any(r["moves"] == [["B", "D4"]] and r["maxVisits"] == 33
                                      for r in handol_requests(handol_server, "plain")))


async def test_handol_engine_moves_follow_the_colours_move_style(h, handol_server):
    await h.connect_to(handol_server)
    await h.send({"type": "engine_params", "maxVisits": 9})
    await h.send({"type": "players", "whiteIsEngine": True, "whiteStyle": "katago"})
    await h.play("D4")
    await h.wait_moves(2)
    assert any(r["moves"] == [["B", "D4"]] and r["maxVisits"] == 9
               for r in handol_requests(handol_server, "plain"))


# -- human settings are validated when they change (§3.3, §2.5) ------------------------------------
@pytest.mark.parametrize("fields", [
    {"policy": {"min_p": 2}},
    {"policy": {"surprise": 1}},
    {"policy": [1, 2]},
    {"compare": {"temperature": 0}},
    {"compare": "wide"},
    {"profile": "bad name!"},
    {"profile": "p" * 65},
    {"profile": ""},
], ids=["min_p-range", "unknown-key", "not-object", "compare-range", "compare-not-object",
        "profile-chars", "profile-65", "profile-empty"])
async def test_a_refused_human_setting_is_an_error(h, fields):
    start = h.rec.mark()
    await h.send({"type": "human_params", **fields})
    assert await h.rec.wait_error(start) is not None


@pytest.mark.parametrize("fields", [
    {"policy": {"min_p": 2}},
    {"compare": {"temperature": 0}},
    {"profile": "bad name!"},
], ids=["policy", "compare", "profile"])
async def test_a_refused_human_setting_is_not_stored(h, fields):
    before = (await h.fresh_state())["settings"]
    await h.send({"type": "human_params", **fields})
    after = (await h.fresh_state())["settings"]
    keys = ("humanProfile", "humanPolicy", "humanCompare")
    assert [after[k] for k in keys] == [before[k] for k in keys]


async def test_a_tuple_needing_a_search_is_refused_with_max_visits_1(h):
    await h.send({"type": "engine_params", "maxVisits": 1})
    start = h.rec.mark()
    await h.send({"type": "human_params",
                  "policy": {"lambda_utility": 1, "trust_mu": 1, "fill_kappa": 0}})
    assert await h.rec.wait_error(start) is not None


async def test_lowering_max_visits_under_a_tuple_that_needs_a_search_is_refused(h):
    """§3.4: the change is checked against the board's tuples and reported once, at the change,
    instead of leaving every later query to fail."""
    await h.send({"type": "human_params",
                  "policy": {"lambda_utility": 1, "trust_mu": 1, "fill_kappa": 0}})
    start = h.rec.mark()
    await h.send({"type": "engine_params", "maxVisits": 1})
    error = await h.rec.wait_error(start)
    state = await h.fresh_state()
    assert error is not None and "lambda_utility" in error
    assert state["settings"]["maxVisits"] != 1


async def test_a_refused_max_visits_keeps_the_fields_sent_with_it(h):
    """§3.4: `engine_params` is refused whole. The report interval and the ownership flag the
    page sends in the same frame are not applied either, although neither is refused."""
    await h.send({"type": "human_params",
                  "policy": {"lambda_utility": 1, "trust_mu": 1, "fill_kappa": 0}})
    before = (await h.fresh_state())["settings"]
    start = h.rec.mark()
    await h.send({"type": "engine_params", "maxVisits": 1, "reportInterval": 2.5,
                  "includeOwnership": not before["includeOwnership"]})
    error = await h.rec.wait_error(start)
    after = (await h.fresh_state())["settings"]
    keys = ("maxVisits", "reportInterval", "includeOwnership")
    assert error is not None and [after[k] for k in keys] == [before[k] for k in keys]


async def test_lowering_max_visits_is_allowed_without_such_a_tuple(h):
    await h.send({"type": "human_params", "policy": {"temperature": 2}})
    start = h.rec.mark()
    await h.send({"type": "engine_params", "maxVisits": 1})
    state = await h.fresh_state()
    assert (h.rec.errors(start), state["settings"]["maxVisits"]) == ([], 1)


async def test_a_null_compare_clears_the_comparison(h):
    await human(h, compare={"temperature": 2})
    await human(h, compare=None)
    assert (await h.fresh_state())["settings"]["humanCompare"] is None


async def test_an_absent_compare_leaves_the_comparison(h):
    await human(h, compare={"temperature": 2})
    await human(h, profile="rank_2d")
    assert (await h.fresh_state())["settings"]["humanCompare"] == {"temperature": 2}


# -- hidden addresses (§7.7) -----------------------------------------------------------------------
async def hidden_session(make_session, monkeypatch):
    """A catalog space whose engines live at the sentinel host, with addresses hidden."""
    route_sentinel_host(monkeypatch)
    return await catalog_session(make_session, host=SENTINEL_HOST)


async def scenario_connect_failure(h, catalog, fake_engine, monkeypatch) -> int:
    port = free_port()
    catalog.add("kata", "gtp", port, console=True)
    start = h.rec.mark()
    await h.send({"type": "connect", "engineId": "kata"})
    assert await h.rec.wait_error(start) is not None
    return port


async def scenario_timeout(h, catalog, fake_engine, monkeypatch) -> int:
    # Patches the engine client (not the session): a genmove deadline short enough for a test.
    real_init = GTPEngine.__init__

    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        self.genmove_timeout = 0.3

    monkeypatch.setattr(GTPEngine, "__init__", init)
    server = await fake_engine("gtp", delay={"genmove": 2.0})
    catalog.add("kata", "gtp", server.port, console=True)
    await h.connect({"engineId": "kata"})
    start = h.rec.mark()
    await h.send({"type": "genmove"})
    assert await h.rec.wait_error(start) is not None
    return server.port


async def scenario_hangup(h, catalog, fake_engine, monkeypatch) -> int:
    server = await fake_engine("gtp", hangup_on="genmove")
    catalog.add("kata", "gtp", server.port, console=True)
    await h.connect({"engineId": "kata"})
    start = h.rec.mark()
    await h.send({"type": "genmove"})
    assert await h.rec.wait_state(lambda f: not f["engine"]["connected"], start)
    await settle(0.3)
    return server.port


async def scenario_console(h, catalog, fake_engine, monkeypatch) -> int:
    server = await fake_engine("gtp", reject=["printsgf"])
    port = server.port
    server.options.replies["showboard"] = f"{SENTINEL_HOST.upper()}:{port}"
    server.options.reject_message = f"cannot write the file on {SENTINEL_HOST}:{port}"
    catalog.add("kata", "gtp", port, console=True)
    await h.connect({"engineId": "kata"})
    await h.send({"type": "raw", "command": "showboard"})
    assert await wait_for(lambda: "showboard" in gtp_names(server))
    await settle(0.3)
    for command in ("printsgf", f"echo {SENTINEL_HOST}"):
        start = h.rec.mark()
        await h.send({"type": "raw", "command": command})
        assert await h.rec.wait_error(start) is not None
    return port


async def scenario_identity(h, catalog, fake_engine, monkeypatch) -> int:
    server = await fake_engine("gtp")
    port = server.port
    server.options.replies["name"] = f"FakeKataGo on {SENTINEL_HOST.upper()}:{port}"
    server.options.replies["version"] = f"1.0 ({SENTINEL_HOST})"
    catalog.add("kata", "gtp", port, console=True)
    await h.connect({"engineId": "kata"})
    await h.fresh_state()
    return port


HIDDEN_SCENARIOS = {
    "connect-failure": scenario_connect_failure,
    "timeout": scenario_timeout,
    "hangup": scenario_hangup,
    "console": scenario_console,
    "name-version-status": scenario_identity,
}


@pytest.mark.parametrize("scenario", sorted(HIDDEN_SCENARIOS))
async def test_a_hidden_engine_address_never_reaches_a_frame(make_session, fake_engine,
                                                             monkeypatch, scenario):
    h, catalog = await hidden_session(make_session, monkeypatch)
    port = await HIDDEN_SCENARIOS[scenario](h, catalog, fake_engine, monkeypatch)
    frames = h.rec.frames + h.session.attach_frames()
    assert leaks(frames, SENTINEL_HOST, port) == []


async def test_a_hidden_space_still_reports_the_engine_name(make_session, fake_engine,
                                                            monkeypatch):
    h, catalog = await hidden_session(make_session, monkeypatch)
    await scenario_identity(h, catalog, fake_engine, monkeypatch)
    assert "FakeKataGo" in (await h.fresh_state())["engine"]["name"]


#: A catalog host that is also an ordinary word, the case §7.7 must not over-match.
WORD_HOST = "katago"


def route_word_host(monkeypatch) -> None:
    """Make :data:`WORD_HOST` resolve to the loopback address (test-only name service)."""
    import socket

    real = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if isinstance(name, str) and name.lower() == WORD_HOST:
            host = LOOPBACK
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


async def word_host_session(make_session, fake_engine, monkeypatch):
    """A hidden space whose engine lives at :data:`WORD_HOST`, connected and identified."""
    route_word_host(monkeypatch)
    h, catalog = await catalog_session(make_session, host=WORD_HOST)
    server = await fake_engine("gtp")
    server.options.replies["name"] = "FakeKataGo"
    server.options.replies["version"] = (f"1.0 from {WORD_HOST}:{server.port}, host {WORD_HOST}, "
                                         f"built by katagonaut for my-katago.example, "
                                         f"could not reach {WORD_HOST}.")
    catalog.add("kata", "gtp", server.port, console=True)
    return h, await h.connect({"engineId": "kata"}), server


async def test_scrubbing_leaves_a_word_that_merely_contains_the_host(make_session, fake_engine,
                                                                     monkeypatch):
    """§7.7: the host is matched on host boundaries, so a longer name keeps its letters."""
    _, state, _ = await word_host_session(make_session, fake_engine, monkeypatch)
    assert state["engine"]["name"] == "FakeKataGo"
    assert "katagonaut" in state["engine"]["version"]
    assert "my-katago.example" in state["engine"]["version"]


async def test_scrubbing_still_takes_the_host_and_the_host_port(make_session, fake_engine,
                                                                monkeypatch):
    """§7.7: `host:port` and the bare host are replaced, so the address never appears."""
    h, state, server = await word_host_session(make_session, fake_engine, monkeypatch)
    version = state["engine"]["version"]
    assert version == ("1.0 from [engine], host [engine], built by katagonaut "
                       "for my-katago.example, could not reach [engine].")
    assert leaks(h.rec.frames + h.session.attach_frames(), f"{WORD_HOST}:", server.port) == []


async def test_scrubbing_takes_a_printed_address_tuple_whole(make_session, fake_engine,
                                                             monkeypatch):
    """§7.7: an address printed as a Python tuple goes whole, its port with it, and the
    four-element form an IPv6 address takes goes the same way."""
    h, catalog = await hidden_session(make_session, monkeypatch)
    server = await fake_engine("gtp")
    server.options.replies["version"] = (f"1.0 at ('{SENTINEL_HOST}', {server.port}) "
                                         f"and at ('{SENTINEL_HOST}', {server.port}, 0, 0)")
    catalog.add("kata", "gtp", server.port, console=False)
    state = await h.connect({"engineId": "kata"})
    assert state["engine"]["version"] == "1.0 at [engine] and at [engine]"
    assert leaks(h.rec.frames + h.session.attach_frames(), SENTINEL_HOST, server.port) == []


#: SPEC §7.7's own example host: the one host whose own colons meet the `host:port` shapes.
IPV6_HOST = "::1"


async def test_scrubbing_takes_an_ipv6_literal_whole(make_session, fake_engine):
    """§7.7: a host that is an IPv6 literal carries colons of its own, so `[::1]:6363` and the
    four-element tuple an IPv6 address takes still go whole, port with them."""
    h, catalog = await catalog_session(make_session, host=IPV6_HOST)
    server = await fake_engine("gtp", host=IPV6_HOST)
    server.options.replies["version"] = (f"1.0 at [{IPV6_HOST}]:{server.port}, "
                                         f"at ('{IPV6_HOST}', {server.port}) "
                                         f"and at ('{IPV6_HOST}', {server.port}, 0, 0)")
    catalog.add("kata", "gtp", server.port, console=False)
    state = await h.connect({"engineId": "kata"})
    assert state["engine"]["version"] == "1.0 at [engine], at [engine] and at [engine]"
    assert leaks(h.rec.frames + h.session.attach_frames(), IPV6_HOST, server.port) == []


# -- review round 1: containment, lifecycle, result grammar (§3.2, §3.5, §4.1, §6.3, §7.7) ---------
class AwaitableBroadcast:
    """A broadcast that records each frame and then returns an awaitable that would raise.

    §3.2: broadcast is sync; an awaitable it returns is never run, so its failure cannot reach
    the session's tasks."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    def __call__(self, frame: dict):
        self.frames.append(frame)
        return self._fail()

    async def _fail(self) -> None:
        raise RuntimeError("the async broadcast failed")


async def test_an_awaitable_broadcast_causes_no_task_storm():
    from gowui.session import GameSession
    from session_helpers import typed_resolver

    broadcast = AwaitableBroadcast()
    session = GameSession(typed_resolver, broadcast=broadcast)
    try:
        await session.handle({"type": "state"})
        await settle(0.3)
        storm = (len(broadcast.frames), len(asyncio.all_tasks()))
        await session.handle({"type": "play", "color": "black", "vertex": "D4"})
        await settle(0.1)
        last = [f for f in broadcast.frames if f.get("type") == "state"][-1]
        assert (storm[0] <= 5, storm[1] <= 5, move_list(last)) == (True, True, ["D4"])
    finally:
        await asyncio.wait_for(session.aclose(), 5.0)


async def test_a_superseded_connect_does_not_block_a_new_one(h, fake_engine):
    slow = await fake_engine("gtp", delay={"name": 3.0})
    live = await fake_engine("gtp")
    await h.send({"type": "connect", "protocol": "gtp", "host": LOOPBACK, "port": slow.port})
    assert await wait_for(lambda: slow.open_connections == 1)
    await h.send({"type": "disconnect"})
    start = h.rec.mark()
    await h.send({"type": "connect", "protocol": "gtp", "host": LOOPBACK, "port": live.port})
    assert h.rec.errors(start) == []
    state = await h.rec.wait_state(lambda f: f["engine"]["connected"], start)
    assert state is not None and state["engine"]["request"]["port"] == live.port


async def test_a_connect_while_two_superseded_attempts_run_is_refused(h, fake_engine):
    """§3.2, §4.1: at most two connect attempts run, the live one and one superseded."""
    slow = await fake_engine("gtp", delay={"name": 3.0})
    connect = {"type": "connect", "protocol": "gtp", "host": LOOPBACK, "port": slow.port}
    for _ in range(2):
        await h.send(connect)
        await h.send({"type": "disconnect"})
    assert await wait_for(lambda: slow.open_connections == 2)
    start = h.rec.mark()
    await h.send(connect)
    error = await h.rec.wait_error(start)
    await settle(0.3)
    assert error == "a previous engine is still closing"
    assert slow.open_connections == 2


async def test_a_connect_disconnect_flood_opens_at_most_two_engine_connections(h, fake_engine):
    """§3.2, §4.1: a flood of connect/disconnect pairs never runs more than two attempts."""
    slow = await fake_engine("gtp", delay={"name": 3.0})
    connect = {"type": "connect", "protocol": "gtp", "host": LOOPBACK, "port": slow.port}
    most = 0
    for _ in range(200):
        await h.send(connect)
        await h.send({"type": "disconnect"})
        most = max(most, slow.open_connections)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 0.5
    while loop.time() < deadline:
        most = max(most, slow.open_connections)
        await asyncio.sleep(0.02)
    assert most <= 2


async def test_a_players_change_rearms_auto_play_halted_while_it_was_still_searching(
        h, fake_engine, monkeypatch):
    """§3.2: an unexpected failure halts automatic play until something re-arms it, such as a
    change of the players — also when the halted auto-play task is still finishing a search."""
    # Patches the engine client (not the session): final_score raises a non-engine error. It
    # waits behind the running genmove, so the halt lands while the next genmove is in flight.
    async def broken(self, *args, **kwargs):
        raise RuntimeError("injected by the test")

    monkeypatch.setattr(GTPEngine, "final_score", broken)
    server = await slow_gtp(fake_engine, 0.5)
    await h.connect_to(server)
    await h.send({"type": "players", "blackIsEngine": True, "whiteIsEngine": True})
    await h.wait_moves(1)
    start = h.rec.mark()
    await h.send({"type": "final_score"})
    assert await h.rec.wait_error(start) is not None
    at_error = h.rec.state()["game"]["moveCount"]
    await h.send({"type": "players", "blackStyle": "katago", "whiteStyle": "katago"})
    await h.wait_moves(at_error + 2, start, timeout=3.0)


def empty_host_resolver(request):
    from gowui.session import EngineTarget
    return EngineTarget(protocol="gtp", host="", port=1, request_echo={"engineId": "x"})


async def test_a_target_with_an_empty_host_is_refused(make_session):
    h = make_session(empty_host_resolver, expose_address=False)
    await h.new_game(9)
    start = h.rec.mark()
    await h.send({"type": "connect", "engineId": "x"})
    error = await h.rec.wait_error(start)
    state = await h.fresh_state()
    assert (error is not None, "[engine]" in (error or ""), "[engine]" in state["status"],
            state["engine"]["connected"]) == (True, False, False, False)


async def test_an_unexpected_failure_outside_an_engine_move_stops_auto_play(
        h, fake_engine, monkeypatch):
    # Patches the engine client (not the session): final_score raises a non-engine error.
    async def broken(self, *args, **kwargs):
        raise RuntimeError("injected by the test")

    monkeypatch.setattr(GTPEngine, "final_score", broken)
    server = await fake_engine("gtp", delay={"genmove": 0.05})
    await h.connect_to(server)
    await h.send({"type": "players", "blackIsEngine": True, "whiteIsEngine": True})
    await h.wait_moves(3)
    start = h.rec.mark()
    await h.send({"type": "final_score"})
    assert await h.rec.wait_error(start) is not None
    at_error = h.rec.state()["game"]["moveCount"]
    await settle(1.0)
    after = (await h.fresh_state())["game"]["moveCount"]
    assert after <= at_error + 1


async def test_a_final_score_that_is_not_a_result_is_not_recorded(make_session, fake_engine,
                                                                  monkeypatch):
    route_sentinel_host(monkeypatch)
    h, catalog = await catalog_session(make_session, host=SENTINEL_HOST)
    server = await fake_engine("gtp")
    server.options.replies["final_score"] = f"B+3.5 scored by {SENTINEL_HOST.upper()}:7777"
    catalog.add("kata", "gtp", server.port)
    await h.connect({"engineId": "kata"})
    await h.play("D4")
    start = h.rec.mark()
    await h.send({"type": "final_score"})
    state = await h.rec.wait_state(lambda f: f["status"].startswith("The engine"), start)
    snapshot = json.dumps(h.session.snapshot()).lower()
    assert (state is not None, (await h.fresh_state())["game"]["result"],
            SENTINEL_HOST in snapshot, leaks(h.rec.frames, SENTINEL_HOST, server.port)) == \
        (True, "", False, [])


@pytest.mark.parametrize("reply", ["W+12.5", "B+R", "W+Resign", "B+T", "W+Forfeit", "B+", "0",
                                   "Draw", "Void", "?"])
async def test_a_final_score_in_the_result_grammar_is_recorded(h, fake_engine, reply):
    server = await fake_engine("gtp")
    server.options.replies["final_score"] = reply
    await h.connect_to(server)
    await h.play("D4")
    start = h.rec.mark()
    await h.send({"type": "final_score"})
    state = await h.rec.wait_state(lambda f: f["game"]["result"] != "", start)
    assert state is not None and state["game"]["result"] == reply


# -- engines still closing count against the connect bound (§3.2, §4.1) ------------------------------
async def connect_wait_disconnect_flood(h, server, cycles: int, hold: float = 0.0) -> int:
    """Run ``cycles`` of connect → wait until connected → (hold) → disconnect, sampling the fake's
    open connections all along; a connect refused because engines are still closing counts as a
    cycle. Returns the most connections seen open at once."""
    connect = {"type": "connect", "protocol": "gtp", "host": LOOPBACK, "port": server.port}
    most = 0
    running = True

    async def sample() -> None:
        nonlocal most
        while running:
            most = max(most, server.open_connections)
            await asyncio.sleep(0.005)

    sampler = asyncio.create_task(sample())
    try:
        for _ in range(cycles):
            start = h.rec.mark()
            await h.send(connect)
            if h.rec.errors(start):
                await asyncio.sleep(0.02)
                continue
            await h.rec.wait_state(lambda f: f["engine"]["connected"], start, timeout=2.0)
            if hold:
                await asyncio.sleep(hold)
            await h.send({"type": "disconnect"})
        await asyncio.sleep(0.3)
    finally:
        running = False
        await sampler
    return max(most, server.open_connections)


async def test_a_connect_disconnect_loop_against_a_slow_quit_opens_at_most_two_connections(
        h, fake_engine):
    """§3.2, §4.1: an engine released by a disconnect is still closing (its quit is slow), and it
    counts against the bound of two with the connect attempts."""
    server = await fake_engine("gtp", delay={"quit": 1.9})
    assert await connect_wait_disconnect_flood(h, server, 200) <= 2


async def test_a_connect_disconnect_loop_against_an_engine_that_will_not_stop_analysing(
        h, fake_engine):
    """§3.2, §4.1: an engine that ignores the interrupt holds its close for the stop timeout; the
    engines still closing count against the bound of two."""
    server = await fake_engine("gtp", ignore_interrupt=True)
    await h.send({"type": "analysis", "enabled": True})
    assert await connect_wait_disconnect_flood(h, server, 200, hold=0.03) <= 2


async def test_a_connect_that_would_release_the_engine_while_another_closes_is_refused(
        h, fake_engine):
    """§3.2, §4.1: a connect counts the current engine it releases as one being closed."""
    server = await fake_engine("gtp", delay={"quit": 1.9})
    await h.connect_to(server)
    await h.send({"type": "disconnect"})
    await h.connect_to(server)
    start = h.rec.mark()
    await h.send({"type": "connect", "protocol": "gtp", "host": LOOPBACK, "port": server.port})
    error = await h.rec.wait_error(start, timeout=1.0)
    await settle(0.3)
    assert error == "a previous engine is still closing"
    assert server.open_connections <= 2


async def test_a_normal_disconnect_then_connect_still_connects(h, gtp_server):
    """§3.2: an engine that closes quickly frees its place in the bound at once."""
    for _ in range(5):
        await h.connect_to(gtp_server)
        await h.send({"type": "disconnect"})
    state = await h.connect_to(gtp_server)
    assert state["engine"]["connected"] is True


async def test_a_close_cut_short_at_shutdown_drops_the_engine_connection(h, fake_engine):
    """§3.2: aclose cancels a close still running past its grace; the connection is dropped
    rather than left open."""
    server = await fake_engine("gtp", ignore_interrupt=True)
    await h.connect_to(server)
    start = h.rec.mark()
    await h.send({"type": "analysis", "enabled": True})
    assert await h.rec.wait("analysis", start=start) is not None
    await h.send({"type": "disconnect"})  # the close now waits out the stop timeout (5 s)
    await h.aclose()
    assert await wait_for(lambda: server.open_connections == 0, timeout=1.0)


async def test_a_switch_behind_an_engine_still_closing_is_refused_as_such(h, fake_engine):
    """§4.1: A → B → C while A's close is slow — the connect to C would release B while A still
    closes, so it is refused, and the refusal says why."""
    a = await fake_engine("gtp", delay={"quit": 1.9})
    b = await fake_engine("gtp")
    c = await fake_engine("gtp")
    await h.connect_to(a)
    await h.connect_to(b)
    start = h.rec.mark()
    await h.send({"type": "connect", "protocol": "gtp", "host": LOOPBACK, "port": c.port})
    error = await h.rec.wait_error(start, timeout=1.0)
    assert error == "a previous engine is still closing"
    assert c.open_connections == 0


async def analysing_engine_that_ignores_the_interrupt(h, fake_engine):
    server = await fake_engine("gtp", ignore_interrupt=True)
    await h.connect_to(server)
    start = h.rec.mark()
    await h.send({"type": "analysis", "enabled": True})
    assert await h.rec.wait("analysis", start=start) is not None
    return server


async def cancel_aclose_after(h, seconds: float) -> None:
    closing = asyncio.ensure_future(h.session.aclose())
    await asyncio.sleep(seconds)
    closing.cancel()
    await asyncio.gather(closing, return_exceptions=True)


async def test_a_cancelled_aclose_drops_the_engine_it_was_closing(h, fake_engine):
    """§3.2: aclose closes the current engine inline; when aclose is itself cancelled mid-close
    (the engine ignores the interrupt, so the close stalls), the connection is dropped at once."""
    server = await analysing_engine_that_ignores_the_interrupt(h, fake_engine)
    await cancel_aclose_after(h, 0.5)
    await asyncio.sleep(0.5)
    assert server.open_connections == 0


async def test_a_cancelled_aclose_leaves_no_engine_still_closing(h, fake_engine):
    """§3.2: a shutdown that is itself cancelled while a released engine is still closing
    drops that engine, so no session task is left behind."""
    await analysing_engine_that_ignores_the_interrupt(h, fake_engine)
    await h.send({"type": "disconnect"})  # the close now waits out the stop timeout (5 s)
    await cancel_aclose_after(h, 0.3)
    await settle(0.1)
    assert gowui_tasks() == []


async def test_a_cancelled_aclose_leaves_no_session_task_pending(h, fake_engine):
    """§3.2: a shutdown that is itself cancelled cancels every session task still running
    before the cancellation propagates, here a connect still in its handshake."""
    server = await fake_engine("gtp", delay={"name": 3.0})
    await h.send({"type": "connect", "protocol": "gtp", "host": LOOPBACK, "port": server.port})
    assert await wait_for(lambda: server.open_connections == 1)
    await cancel_aclose_after(h, 0.3)
    await settle(0.1)
    assert gowui_tasks() == []
