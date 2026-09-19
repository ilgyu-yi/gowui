"""The fake engine itself (SPEC §2.6), checked over raw TCP so it does not lean on the clients it
stands in front of."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import pytest

import fake_engine as fake_module
from gowui import coords
from helpers import HANG, RawClient, color_of, game_with

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "fake_engine.py"
LISTENING = re.compile(r"listening on 127\.0\.0\.1:([0-9]+)")


# -- module surface ---------------------------------------------------------------------
def test_the_registered_fake_protocols_are_exactly_gtp_analysis_and_handol():
    assert set(fake_module.PROTOCOLS) == {"gtp", "analysis", "handol"}


def test_fake_options_is_exported():
    assert fake_module.FakeOptions is not None


async def test_start_fake_engine_binds_a_free_port(gtp_server):
    assert gtp_server.port > 0


async def test_start_fake_engine_listens_on_loopback_by_default(gtp_server):
    assert gtp_server.host == "127.0.0.1"


async def test_the_server_knows_its_protocol(analysis_server):
    assert analysis_server.protocol == "analysis"


async def test_an_unknown_protocol_is_refused():
    with pytest.raises(ValueError):
        await asyncio.wait_for(fake_module.start_fake_engine("telepathy"), HANG)


async def test_stop_with_a_client_still_connected_does_not_hang(fake_engine):
    server = await fake_engine("gtp")
    client = await RawClient.open(server.port)
    try:
        await asyncio.wait_for(server.stop(), HANG)
    finally:
        await client.close()


async def test_stop_twice_is_harmless(fake_engine):
    server = await fake_engine("gtp")
    await asyncio.wait_for(server.stop(), HANG)
    await asyncio.wait_for(server.stop(), HANG)


async def test_the_request_log_records_received_lines(gtp_server):
    client = await RawClient.open(gtp_server.port)
    try:
        await client.gtp("name")
    finally:
        await client.close()
    assert gtp_server.requests == ["1 name"]


# -- command line --------------------------------------------------------------------------
async def run_cli(*args: str):
    return await asyncio.create_subprocess_exec(
        sys.executable, str(SCRIPT), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)


async def reap(process) -> None:
    if process.returncode is None:
        process.kill()
    await asyncio.wait_for(process.wait(), HANG)


async def cli_port(process) -> int:
    line = (await asyncio.wait_for(process.stdout.readline(), HANG)).decode().strip()
    match = LISTENING.fullmatch(line)
    assert match, f"unexpected first line {line!r}"
    return int(match.group(1))


async def test_cli_gtp_prints_its_port_and_answers():
    process = await run_cli("--protocol", "gtp", "--port", "0")
    try:
        client = await RawClient.open(await cli_port(process))
        try:
            ok, _ = await client.gtp("name")
        finally:
            await client.close()
        assert ok
    finally:
        await reap(process)


async def test_cli_analysis_prints_its_port_and_answers():
    process = await run_cli("--protocol", "analysis", "--port", "0")
    try:
        client = await RawClient.open(await cli_port(process))
        try:
            await client.json({"id": "v", "action": "query_version"})
            reply = await client.json_line()
        finally:
            await client.close()
        assert reply.get("id") == "v"
    finally:
        await reap(process)


async def test_cli_prints_exactly_the_listening_line():
    process = await run_cli("--protocol", "gtp", "--port", "0")
    try:
        line = (await asyncio.wait_for(process.stdout.readline(), HANG)).decode()
        assert LISTENING.fullmatch(line.rstrip("\n"))
    finally:
        await reap(process)


async def test_cli_delay_holds_the_named_command_only():
    process = await run_cli("--protocol", "gtp", "--port", "0", "--delay", "name=0.3")
    try:
        client = await RawClient.open(await cli_port(process))
        try:
            loop = asyncio.get_running_loop()
            start = loop.time()
            await client.gtp("version")
            quick = loop.time() - start
            start = loop.time()
            ok, _ = await client.gtp("name")
            held = loop.time() - start
        finally:
            await client.close()
        assert ok and held >= 0.3 > quick
    finally:
        await reap(process)


@pytest.mark.parametrize("value", ["name", "=1", "name=x", "name=-1", "name=nan", "name=inf"])
async def test_cli_refuses_a_malformed_delay(value):
    process = await run_cli("--protocol", "gtp", "--port", "0", "--delay", value)
    try:
        assert await asyncio.wait_for(process.wait(), HANG) == 2
    finally:
        await reap(process)


@pytest.mark.parametrize("protocol", ["telepathy", "kgs"])
async def test_cli_refuses_an_unregistered_protocol(protocol):
    process = await run_cli("--protocol", protocol, "--port", "0")
    try:
        code = await asyncio.wait_for(process.wait(), HANG)
        assert code != 0
    finally:
        await reap(process)


# -- GTP --------------------------------------------------------------------------------------
async def gtp_client(fake_engine, **options):
    server = await fake_engine("gtp", **options)
    return server, await RawClient.open(server.port)


async def test_gtp_genmove_is_legal(fake_engine):
    _, client = await gtp_client(fake_engine)
    try:
        for command in ("boardsize 9", "clear_board", "komi 6.5", "play B E5"):
            await client.gtp(command)
        ok, vertex = await client.gtp("genmove W")
    finally:
        await client.close()
    game = game_with(9, "E5")
    assert ok and game.legal_error(color_of("W"), coords.from_gtp(vertex, 9)) is None


async def test_gtp_refuses_a_play_on_an_occupied_point(fake_engine):
    _, client = await gtp_client(fake_engine)
    try:
        for command in ("boardsize 9", "clear_board", "play B E5"):
            await client.gtp(command)
        ok, _ = await client.gtp("play W E5")
    finally:
        await client.close()
    assert not ok


async def test_gtp_lists_kata_commands_by_default(fake_engine):
    _, client = await gtp_client(fake_engine)
    try:
        _, listed = await client.gtp("list_commands")
    finally:
        await client.close()
    assert {"kata-analyze", "kata-genmove_analyze", "lz-analyze", "kata-set-rules",
            "kata-set-param", "final_score", "undo"} <= set(listed.split())


async def test_gtp_kata_false_lists_only_lz_analyze(fake_engine):
    _, client = await gtp_client(fake_engine, kata=False)
    try:
        _, listed = await client.gtp("list_commands")
    finally:
        await client.close()
    names = set(listed.split())
    assert ("lz-analyze" in names, "kata-analyze" in names,
            "kata-genmove_analyze" in names) == (True, False, False)


async def test_gtp_kata_false_refuses_kata_analyze(fake_engine):
    _, client = await gtp_client(fake_engine, kata=False)
    try:
        ok, _ = await client.gtp("kata-analyze B 10")
    finally:
        await client.close()
    assert not ok


async def test_gtp_list_commands_option_replaces_the_list(fake_engine):
    _, client = await gtp_client(fake_engine, list_commands=[])
    try:
        _, listed = await client.gtp("list_commands")
    finally:
        await client.close()
    assert listed.strip() == ""


async def first_stream_line(client, command: str) -> tuple[str, str]:
    """Start an analysis stream; return its head and first report, then interrupt it."""
    await client.send(command)
    head = await client.line()
    report = await client.line()
    await client.send("")
    return head, report


@pytest.mark.parametrize("options", ["ownership true", "ownership false"])
async def test_gtp_ownership_false_refuses_the_ownership_key(fake_engine, options):
    _, client = await gtp_client(fake_engine, ownership=False)
    try:
        await client.send(f"7 kata-analyze B 10 {options} rootInfo true")
        head = await client.line()
    finally:
        await client.close()
    assert head.startswith("?")


async def test_gtp_ownership_false_accepts_a_command_without_the_key(fake_engine):
    _, client = await gtp_client(fake_engine, ownership=False)
    try:
        head, _ = await first_stream_line(client, "7 kata-analyze B 10 rootInfo true")
    finally:
        await client.close()
    assert head.startswith("=7")


async def test_gtp_root_info_false_refuses_root_info(fake_engine):
    _, client = await gtp_client(fake_engine, root_info=False)
    try:
        await client.send("7 kata-analyze B 10 rootInfo true")
        head = await client.line()
    finally:
        await client.close()
    assert head.startswith("?")


async def test_gtp_kata_analyze_streams_reports(fake_engine):
    _, client = await gtp_client(fake_engine)
    try:
        for command in ("boardsize 9", "clear_board"):
            await client.gtp(command)
        _, report = await first_stream_line(client, "7 kata-analyze B 10 rootInfo true")
    finally:
        await client.close()
    assert report.startswith("info ") and "rootInfo" in report


async def test_gtp_lz_analyze_reports_winrates_on_a_ten_thousand_scale(fake_engine):
    _, client = await gtp_client(fake_engine, kata=False)
    try:
        for command in ("boardsize 9", "clear_board"):
            await client.gtp(command)
        _, report = await first_stream_line(client, "7 lz-analyze B 10")
    finally:
        await client.close()
    winrate = float(report.split("winrate ")[1].split()[0])
    assert 1 < winrate <= 10000


async def test_gtp_emit_line_comes_first_in_a_stream(fake_engine):
    _, client = await gtp_client(fake_engine, emit_line="info move E5 visits 1 order 0 pv E5")
    try:
        for command in ("boardsize 9", "clear_board"):
            await client.gtp(command)
        _, report = await first_stream_line(client, "7 kata-analyze B 10")
    finally:
        await client.close()
    assert report == "info move E5 visits 1 order 0 pv E5"


async def test_gtp_nonfinite_writes_nonfinite_numbers(fake_engine):
    _, client = await gtp_client(fake_engine, nonfinite=True)
    try:
        for command in ("boardsize 9", "clear_board"):
            await client.gtp(command)
        _, report = await first_stream_line(client, "7 kata-analyze B 10 ownership true")
    finally:
        await client.close()
    assert {"nan", "inf", "1e999"} <= set(report.split())


async def test_gtp_hangup_on_closes_the_connection(fake_engine):
    _, client = await gtp_client(fake_engine, hangup_on="final_score")
    try:
        await client.send("1 final_score")
        closed = await client.at_eof()
    finally:
        await client.close()
    assert closed


async def test_gtp_hangup_after_reports_closes_the_stream(fake_engine):
    _, client = await gtp_client(fake_engine, hangup_after_reports=1)
    try:
        for command in ("boardsize 9", "clear_board"):
            await client.gtp(command)
        await client.send("7 kata-analyze B 10")
        closed = await client.at_eof()
    finally:
        await client.close()
    assert closed


async def test_gtp_delay_holds_the_reply(fake_engine):
    _, client = await gtp_client(fake_engine, delay={"final_score": 0.5})
    try:
        start = time.monotonic()
        await client.gtp("final_score")
        elapsed = time.monotonic() - start
    finally:
        await client.close()
    assert elapsed >= 0.5


async def test_gtp_replies_override_the_payload(fake_engine):
    _, client = await gtp_client(fake_engine, replies={"genmove": "Z99"})
    try:
        _, payload = await client.gtp("genmove B")
    finally:
        await client.close()
    assert payload == "Z99"


async def test_gtp_reject_answers_with_an_error(fake_engine):
    _, client = await gtp_client(fake_engine, reject=["final_score"])
    try:
        ok, _ = await client.gtp("final_score")
    finally:
        await client.close()
    assert not ok


async def test_gtp_wrong_id_on_answers_with_another_id(fake_engine):
    _, client = await gtp_client(fake_engine, wrong_id_on="final_score")
    try:
        await client.send("41 final_score")
        head = await client.line()
    finally:
        await client.close()
    assert re.match(r"[=?]([0-9]+)", head).group(1) != "41"


async def test_gtp_overlong_line_is_over_one_mib(fake_engine):
    _, client = await gtp_client(fake_engine, overlong_line="final_score")
    try:
        await client.send("1 final_score")
        head = await client.line()
    finally:
        await client.close()
    assert len(head.encode()) > 1024 * 1024


async def test_gtp_big_reply_has_the_requested_size(fake_engine):
    _, client = await gtp_client(fake_engine, big_reply=("final_score", 5))
    try:
        _, payload = await client.gtp("final_score")
    finally:
        await client.close()
    assert len(payload.encode()) > 4 * 1024 * 1024


async def test_gtp_ignore_interrupt_keeps_streaming(fake_engine):
    _, client = await gtp_client(fake_engine, ignore_interrupt=True)
    try:
        for command in ("boardsize 9", "clear_board"):
            await client.gtp(command)
        await first_stream_line(client, "7 kata-analyze B 10")
        lines = [await client.line() for _ in range(3)]
    finally:
        await client.close()
    assert all(line.startswith("info ") for line in lines)


# -- analysis -----------------------------------------------------------------------------------
def query(size: int = 9, **extra) -> dict:
    base = {"id": "q1", "boardXSize": size, "boardYSize": size, "komi": 6.5,
            "rules": "japanese", "initialStones": [], "moves": [], "analyzeTurns": [0],
            "includePolicy": True, "maxVisits": 50}
    base.update(extra)
    return base


async def analysis_client(fake_engine, **options):
    server = await fake_engine("analysis", **options)
    return server, await RawClient.open(server.port)


async def results_until_final(client) -> list[dict]:
    out = []
    while True:
        result = await client.json_line()
        out.append(result)
        if not result.get("isDuringSearch"):
            return out


async def test_analysis_answers_query_version(fake_engine):
    _, client = await analysis_client(fake_engine)
    try:
        await client.json({"id": "v", "action": "query_version"})
        reply = await client.json_line()
    finally:
        await client.close()
    assert reply["id"] == "v" and reply.get("version")


async def test_analysis_policy_has_one_entry_per_point_plus_pass(fake_engine):
    _, client = await analysis_client(fake_engine)
    try:
        await client.json(query(9))
        results = await results_until_final(client)
    finally:
        await client.close()
    assert len(results[-1]["policy"]) == 9 * 9 + 1


async def test_analysis_reports_during_search_when_asked(fake_engine):
    _, client = await analysis_client(fake_engine)
    try:
        await client.json(query(9, reportDuringSearchEvery=0.1))
        results = await results_until_final(client)
    finally:
        await client.close()
    assert len(results) >= 2 and results[0]["isDuringSearch"]


async def test_analysis_candidates_are_legal(fake_engine):
    _, client = await analysis_client(fake_engine)
    try:
        await client.json(query(9, moves=[["B", "E5"]], analyzeTurns=[1]))
        final = (await results_until_final(client))[-1]
    finally:
        await client.close()
    game = game_with(9, "E5")
    assert final["moveInfos"] and all(
        game.legal_error(color_of("W"), coords.from_gtp(info["move"], 9)) is None
        for info in final["moveInfos"])


async def test_analysis_honours_initial_player(fake_engine):
    _, client = await analysis_client(fake_engine)
    try:
        await client.json(query(9, initialStones=[["B", "C3"], ["B", "G7"]], initialPlayer="W"))
        final = (await results_until_final(client))[-1]
    finally:
        await client.close()
    assert final["rootInfo"]["currentPlayer"] == "W"


async def test_analysis_refuses_an_illegal_move_with_an_error_for_that_id(fake_engine):
    _, client = await analysis_client(fake_engine)
    try:
        await client.json(query(9, moves=[["B", "E5"], ["W", "E5"]], analyzeTurns=[2]))
        reply = await client.json_line()
    finally:
        await client.close()
    assert reply["id"] == "q1" and "error" in reply


async def test_analysis_ownership_when_asked(fake_engine):
    _, client = await analysis_client(fake_engine)
    try:
        await client.json(query(9, includeOwnership=True))
        final = (await results_until_final(client))[-1]
    finally:
        await client.close()
    assert len(final["ownership"]) == 81


async def test_analysis_emit_line_carries_the_query_id(fake_engine):
    _, client = await analysis_client(fake_engine, emit_line='{"id": "%ID%", "marker": 1}')
    try:
        await client.json(query(9))
        first = await client.json_line()
    finally:
        await client.close()
    assert first == {"id": "q1", "marker": 1}


async def test_analysis_nonfinite_writes_nonfinite_numbers(fake_engine):
    _, client = await analysis_client(fake_engine, nonfinite=True)
    try:
        await client.json(query(9, includeOwnership=True))
        text = await client.line()
    finally:
        await client.close()
    assert "NaN" in text and ("Infinity" in text or "1e999" in text)


async def test_analysis_hangup_on_query_closes_the_connection(fake_engine):
    _, client = await analysis_client(fake_engine, hangup_on="query")
    try:
        await client.json(query(9))
        closed = await client.at_eof()
    finally:
        await client.close()
    assert closed


async def test_analysis_terminate_stops_the_reports(fake_engine):
    _, client = await analysis_client(fake_engine)
    try:
        await client.json(query(9, reportDuringSearchEvery=0.1, maxVisits=100000))
        await client.json_line()
        await client.json({"id": "t", "action": "terminate", "terminateId": "q1"})
        await asyncio.sleep(0.3)  # reports already on the wire may still arrive
        while True:
            try:
                await asyncio.wait_for(client.reader.readline(), 0.05)
            except asyncio.TimeoutError:
                break
        seen = []
        end = asyncio.get_running_loop().time() + 0.6
        while asyncio.get_running_loop().time() < end:
            try:
                seen.append(json.loads((await asyncio.wait_for(
                    client.reader.readline(), 0.3)).decode()))
            except asyncio.TimeoutError:
                break
    finally:
        await client.close()
    assert [r for r in seen if r.get("id") == "q1" and r.get("isDuringSearch")] == []


async def test_analysis_query_delay_holds_back_a_query(fake_engine):
    _, client = await analysis_client(
        fake_engine, query_delay=lambda q: 1.0 if q["id"] == "slow" else 0.0)
    try:
        await client.json(query(9, id="slow"))
        await client.json(query(9, id="fast"))
        first = await client.json_line()
    finally:
        await client.close()
    assert first["id"] == "fast"


async def test_analysis_overlong_line_is_over_one_mib(fake_engine):
    _, client = await analysis_client(fake_engine, overlong_line="query")
    try:
        await client.json(query(9))
        line = await client.line()
    finally:
        await client.close()
    assert len(line.encode()) > 1024 * 1024


# -- fault options added for the transport rules (§2.1) -----------------------------------------
async def test_gtp_no_reply_id_answers_without_an_id(fake_engine):
    _, client = await gtp_client(fake_engine, no_reply_id=True)
    try:
        await client.send("41 name")
        head = await client.line()
    finally:
        await client.close()
    assert head == "= FakeKataGo"


async def test_gtp_reply_id_answers_with_the_given_id(fake_engine):
    _, client = await gtp_client(fake_engine, reply_id={"final_score": "9" * 30})
    try:
        await client.send("41 final_score")
        head = await client.line()
    finally:
        await client.close()
    assert head.startswith("=" + "9" * 30 + " ")


async def test_gtp_reply_id_expands_the_commands_own_id(fake_engine):
    _, client = await gtp_client(fake_engine, reply_id={"final_score": "00%ID%"})
    try:
        await client.send("41 final_score")
        head = await client.line()
    finally:
        await client.close()
    assert head.startswith("=0041 ")


async def test_gtp_stray_writes_lines_while_a_reply_is_held(fake_engine):
    _, client = await gtp_client(fake_engine, delay={"final_score": 0.35}, stray=(0.1, "noise"))
    try:
        await client.send("1 final_score")
        lines = [await client.line() for _ in range(4)]
    finally:
        await client.close()
    assert lines[:3] == ["noise"] * 3 and lines[3].startswith("=1")


async def test_gtp_reject_message_is_the_error_text(fake_engine):
    _, client = await gtp_client(fake_engine, reject=["final_score"], reject_message="nope")
    try:
        ok, payload = await client.gtp("final_score")
    finally:
        await client.close()
    assert (ok, payload) == (False, "nope")


async def test_gtp_never_read_stops_reading_after_the_handshake(fake_engine):
    server, client = await gtp_client(fake_engine, never_read=True)
    try:
        for command in ("name", "version", "list_commands"):
            await client.gtp(command)
        await client.send("4 final_score")
        await asyncio.sleep(0.3)
    finally:
        await client.close()
    assert [line.split()[-1] for line in server.requests] == ["name", "version", "list_commands"]


async def test_analysis_never_read_stops_reading_after_query_version(fake_engine):
    server, client = await analysis_client(fake_engine, never_read=True)
    try:
        await client.json({"id": "v", "action": "query_version"})
        await client.json_line()
        await client.json(query())
        await asyncio.sleep(0.3)
    finally:
        await client.close()
    assert len(server.requests) == 1


async def test_open_connections_counts_live_connections(gtp_server):
    client = await RawClient.open(gtp_server.port)
    try:
        await client.gtp("name")
        live = gtp_server.open_connections
    finally:
        await client.close()
    assert live == 1


# -- handol (SPEC §2.6 handol mode) ---------------------------------------------------------------
def human_query(size: int = 9, moves=None, policies=None, search: int | None = 10,
                **extra) -> dict:
    human = {"profile": "preaz_1d", "policies": [{}] if policies is None else policies}
    if search is not None:
        human["search"] = {"visits": search}
    base = {"id": "h1", "boardXSize": size, "boardYSize": size, "komi": 6.5,
            "rules": "japanese", "initialStones": [], "moves": moves or [], "human": human}
    base.update(extra)
    return base


def plain_query(size: int = 9, moves=None, **extra) -> dict:
    base = {"id": "p1", "boardXSize": size, "boardYSize": size, "komi": 6.5,
            "rules": "japanese", "initialStones": [], "moves": moves or [], "maxVisits": 20}
    base.update(extra)
    return base


async def handol_client(fake_engine, **options):
    server = await fake_engine("handol", **options)
    return server, await RawClient.open(server.port)


async def handol_ask(fake_engine, request: dict, **options) -> dict:
    _, client = await handol_client(fake_engine, **options)
    try:
        await client.json(request)
        return await client.json_line()
    finally:
        await client.close()


def distribution(reply: dict, index: int = 0) -> list[dict]:
    return reply["policies"][index]["distribution"]


async def test_cli_handol_prints_its_port_and_answers():
    process = await run_cli("--protocol", "handol", "--port", "0")
    try:
        client = await RawClient.open(await cli_port(process))
        try:
            await client.json(human_query())
            reply = await client.json_line()
        finally:
            await client.close()
        assert reply.get("id") == "h1" and reply.get("policies")
    finally:
        await reap(process)


async def test_handol_answers_with_the_request_id(fake_engine):
    reply = await handol_ask(fake_engine, human_query())
    assert reply["id"] == "h1"


async def test_handol_answers_one_distribution_per_tuple(fake_engine):
    reply = await handol_ask(fake_engine, human_query(policies=[{}, {"distance_slope": 1}]))
    assert len(reply["policies"]) == 2


async def test_handol_distribution_moves_are_legal(fake_engine):
    reply = await handol_ask(fake_engine, human_query(moves=[["B", "E5"]]))
    game = game_with(9, "E5")
    assert all(game.legal_error(color_of("W"), coords.from_gtp(e["move"], 9)) is None
               for e in distribution(reply))


async def test_handol_distribution_has_a_pass_entry_with_positive_p(fake_engine):
    reply = await handol_ask(fake_engine, human_query())
    assert [e["p"] > 0 for e in distribution(reply) if e["move"] == "pass"] == [True]


async def test_handol_pass_is_among_the_twenty_most_likely(fake_engine):
    reply = await handol_ask(fake_engine, human_query())
    ranked = sorted(distribution(reply), key=lambda e: -e["p"])
    assert "pass" in [e["move"] for e in ranked[:20]]


async def test_handol_distribution_has_some_zero_points(fake_engine):
    reply = await handol_ask(fake_engine, human_query())
    assert any(e["p"] == 0 for e in distribution(reply))


async def test_handol_distribution_spreads_over_several_moves(fake_engine):
    reply = await handol_ask(fake_engine, human_query())
    assert len([e for e in distribution(reply) if e["p"] > 0]) >= 3


async def test_handol_distribution_is_deterministic(fake_engine):
    first = await handol_ask(fake_engine, human_query())
    assert await handol_ask(fake_engine, human_query()) == first


async def test_handol_distribution_depends_on_the_tuple(fake_engine):
    reply = await handol_ask(fake_engine, human_query(policies=[{}, {"distance_slope": 1}]))
    assert distribution(reply, 0) != distribution(reply, 1)


@pytest.mark.parametrize("request_", [
    human_query(bogus=1),
    human_query(initialPlayer="W"),
    human_query(analyzeTurns=[0]),
    human_query(maxVisits=10),
    human_query(policies=[{"lambda_utility": 0.1, "trust_mu": 0.05, "fill_kappa": 1}],
                search=None),
    {"id": "a", "action": "query_version"},
    plain_query(bogus=1),
    plain_query(initialPlayer="W"),
])
async def test_handol_refuses_what_the_surface_refuses(fake_engine, request_):
    reply = await handol_ask(fake_engine, request_)
    assert "error" in reply


async def test_handol_plain_answer_carries_root_and_move_infos(fake_engine):
    reply = await handol_ask(fake_engine, plain_query())
    assert reply["id"] == "p1" and reply["rootInfo"] and reply["moveInfos"]


async def test_handol_plain_answer_is_from_whites_view(fake_engine):
    # Black to move on an empty board with komi: from White's view, White is ahead.
    reply = await handol_ask(fake_engine, plain_query(komi=7.5))
    assert (reply["rootInfo"]["winrate"] > 0.5, reply["rootInfo"]["scoreLead"] > 0) == (True,
                                                                                        True)


async def test_handol_plain_answer_moves_are_listed_out_of_order(fake_engine):
    reply = await handol_ask(fake_engine, plain_query())
    assert reply["moveInfos"][0].get("order") != 0


async def test_handol_plain_answer_has_exactly_one_order_zero_move(fake_engine):
    reply = await handol_ask(fake_engine, plain_query())
    assert [m.get("order") for m in reply["moveInfos"]].count(0) == 1


async def test_handol_plain_answer_spells_pass_in_capitals(fake_engine):
    reply = await handol_ask(fake_engine, plain_query())
    assert "PASS" in [m["move"] for m in reply["moveInfos"]]


async def test_handol_plain_answer_carries_ownership_when_asked(fake_engine):
    reply = await handol_ask(fake_engine, plain_query(includeOwnership=True))
    assert len(reply["ownership"]) == 81


async def test_handol_plain_answer_is_deterministic(fake_engine):
    first = await handol_ask(fake_engine, plain_query())
    assert await handol_ask(fake_engine, plain_query()) == first


async def test_handol_plain_and_human_answers_share_candidate_moves(fake_engine):
    human = await handol_ask(fake_engine, human_query())
    plain = await handol_ask(fake_engine, plain_query())
    likely = {e["move"].upper() for e in distribution(human) if e["p"] > 0}
    assert likely & {m["move"].upper() for m in plain["moveInfos"] if m["move"] != "PASS"}


async def test_handol_answers_requests_on_one_connection_in_order(fake_engine):
    _, client = await handol_client(fake_engine)
    try:
        await client.json(human_query(id="a"))
        await client.json(plain_query(id="b"))
        ids = [(await client.json_line())["id"], (await client.json_line())["id"]]
    finally:
        await client.close()
    assert ids == ["a", "b"]


# -- handol fault options and bookkeeping --------------------------------------------------------
async def test_handol_idle_close_closes_an_idle_connection(fake_engine):
    _, client = await handol_client(fake_engine, idle_close=0.2)
    try:
        closed = await client.at_eof()
    finally:
        await client.close()
    assert closed


async def test_handol_idle_close_is_logged_by_connection(fake_engine):
    server, client = await handol_client(fake_engine, idle_close=0.2)
    try:
        await client.at_eof()
    finally:
        await client.close()
    assert server.idle_closed == [0]


async def test_handol_query_delay_holds_the_answer(fake_engine):
    _, client = await handol_client(fake_engine, query_delay=lambda q: 0.5)
    try:
        start = time.monotonic()
        await client.json(human_query())
        await client.json_line()
        elapsed = time.monotonic() - start
    finally:
        await client.close()
    assert elapsed >= 0.5


@pytest.mark.parametrize("kind, request_", [
    ("human", human_query()),
    ("plain", plain_query()),
    ("query", human_query()),
    ("query", plain_query()),
])
async def test_handol_hangup_on_closes_the_connection(fake_engine, kind, request_):
    _, client = await handol_client(fake_engine, hangup_on=kind)
    try:
        await client.json(request_)
        closed = await client.at_eof()
    finally:
        await client.close()
    assert closed


async def test_handol_hangup_on_another_kind_answers(fake_engine):
    reply = await handol_ask(fake_engine, plain_query(), hangup_on="human")
    assert reply["id"] == "p1"


async def test_handol_error_reply_carries_the_id(fake_engine):
    reply = await handol_ask(fake_engine, human_query(), error_reply="human")
    assert (reply.get("id"), "error" in reply) == ("h1", True)


async def test_handol_error_reply_without_id(fake_engine):
    reply = await handol_ask(fake_engine, human_query(), error_reply="human",
                             error_reply_id=False)
    assert ("id" in reply, "error" in reply) == (False, True)


async def test_handol_error_reply_for_plain_queries_only(fake_engine):
    reply = await handol_ask(fake_engine, human_query(), error_reply="plain")
    assert "policies" in reply


async def test_handol_wrong_id_on_answers_with_another_id(fake_engine):
    reply = await handol_ask(fake_engine, human_query(), wrong_id_on="human")
    assert reply.get("id") not in (None, "h1")


async def test_handol_fault_times_limits_a_fault(fake_engine):
    _, client = await handol_client(fake_engine, wrong_id_on="human", fault_times=1)
    try:
        await client.json(human_query(id="a"))
        await client.json_line()
        await client.json(human_query(id="b"))
        second = await client.json_line()
    finally:
        await client.close()
    assert second["id"] == "b"


async def test_handol_illegal_point_puts_mass_on_an_occupied_point(fake_engine):
    reply = await handol_ask(fake_engine, human_query(moves=[["B", "E5"], ["W", "D4"]]),
                             illegal_point=True)
    occupied = {"E5", "D4"}
    assert any(e["move"].upper() in occupied and e["p"] > 0 for e in distribution(reply))


async def test_handol_empty_katago_answers_without_moves(fake_engine):
    reply = await handol_ask(fake_engine, plain_query(), empty_katago=True)
    assert reply.get("moveInfos") == []


async def test_handol_empty_distribution_has_no_positive_p(fake_engine):
    reply = await handol_ask(fake_engine, human_query(), empty_distribution=True)
    assert not [e for e in distribution(reply) if e["p"] > 0]


async def test_handol_request_log_is_tagged_by_connection(fake_engine):
    server = await fake_engine("handol")
    first, second = await RawClient.open(server.port), await RawClient.open(server.port)
    try:
        await first.json(human_query(id="a"))
        await first.json_line()
        await second.json(plain_query(id="b"))
        await second.json_line()
    finally:
        await first.close()
        await second.close()
    assert [(i, json.loads(line)["id"]) for i, line in server.connection_requests] == [
        (0, "a"), (1, "b")]


async def test_handol_reply_log_is_tagged_by_connection(fake_engine):
    server, client = await handol_client(fake_engine)
    try:
        await client.json(human_query(id="a"))
        await client.json_line()
    finally:
        await client.close()
    assert [(i, json.loads(line)["id"]) for i, line in server.replies] == [(0, "a")]


async def test_handol_max_in_flight_counts_pipelined_requests(fake_engine):
    server, client = await handol_client(fake_engine, query_delay=lambda q: 0.3)
    try:
        await client.json(human_query(id="a"))
        await client.json(human_query(id="b"))
        await client.json_line()
        await client.json_line()
    finally:
        await client.close()
    assert server.max_in_flight == 2


async def test_handol_max_in_flight_is_one_for_a_serial_client(fake_engine):
    server, client = await handol_client(fake_engine)
    try:
        for name in ("a", "b"):
            await client.json(human_query(id=name))
            await client.json_line()
    finally:
        await client.close()
    assert server.max_in_flight == 1


# -- handol parsing faults (SPEC §2.5 "Parsing") --------------------------------------------------
async def test_handol_short_policies_answers_one_policy_fewer(fake_engine):
    reply = await handol_ask(fake_engine, human_query(policies=[{}, {"distance_slope": 1}]),
                             short_policies=True)
    assert len(reply["policies"]) == 1


async def test_handol_offboard_move_adds_off_board_entries_with_positive_p(fake_engine):
    reply = await handol_ask(fake_engine, human_query(), offboard_move=True)

    def on_board(move) -> bool:
        try:
            coords.from_gtp(move, 9)
        except (ValueError, TypeError):
            return False
        return True

    assert any(not on_board(e["move"]) and e["p"] > 0 for e in distribution(reply))


async def test_handol_bad_p_sends_nan_negative_and_overflowing_p(fake_engine):
    _, client = await handol_client(fake_engine, bad_p=True)
    try:
        await client.json(human_query())
        line = await client.line()
    finally:
        await client.close()
    ps = [e["p"] for e in json.loads(line)["policies"][0]["distribution"]]
    assert ("NaN" in line, "1e999" in line, any(p < 0 for p in ps)) == (True, True, True)
