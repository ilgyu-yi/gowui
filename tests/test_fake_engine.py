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
def test_the_registered_fake_protocols_are_exactly_gtp_and_analysis():
    assert set(fake_module.PROTOCOLS) == {"gtp", "analysis"}


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
        await asyncio.wait_for(fake_module.start_fake_engine("handol"), HANG)


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


@pytest.mark.parametrize("protocol", ["handol", "telepathy"])
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
