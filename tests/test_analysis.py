"""The KataGo analysis-engine client (SPEC §2.4) against the fake engine over real TCP, with the
transport rules of §2.1 and the sanitising of §2.2."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from gowui import coords
from gowui.engine import ConnectionClosed, EngineError, Position
from helpers import (HANG, Disconnects, Log, Reports, color_of, game_with, position_from,
                     position_queries, queries, wait_for)

HOST = "127.0.0.1"


async def analyse(engine, game, count: int = 1, **kwargs) -> Reports:
    reports = Reports()
    kwargs.setdefault("interval", 0.1)
    await asyncio.wait_for(engine.start_analysis(position_from(game), reports, **kwargs), HANG)
    await reports.at_least(count)
    await asyncio.wait_for(engine.stop_analysis(), HANG)
    return reports


async def analyse_to_completion(engine, game, **kwargs) -> Reports:
    reports = Reports()
    kwargs.setdefault("interval", 0.1)
    await asyncio.wait_for(engine.start_analysis(position_from(game), reports, **kwargs), HANG)
    await wait_for(lambda: any(r.complete for r in reports.items))
    await asyncio.wait_for(engine.stop_analysis(), HANG)
    return reports


def legal(game, color_letter: str, vertex: str) -> bool:
    """A stone the side may play (the fake never passes on a board with room)."""
    point = coords.from_gtp(vertex, game.size)
    return point is not None and game.legal_error(color_of(color_letter), point) is None


def report_line(**overrides) -> str:
    """A during-search report for the live query; the fake replaces %ID% with its id."""
    report = {
        "id": "%ID%", "turnNumber": 0, "isDuringSearch": True,
        "moveInfos": [{"move": "E5", "visits": 10, "winrate": 0.7, "scoreLead": 3.0,
                       "scoreMean": 2.0, "scoreStdev": 5.0, "prior": 0.2, "lcb": 0.65,
                       "utility": 0.1, "utilityLcb": 0.09, "order": 0, "pv": ["E5", "D4"]}],
        "rootInfo": {"visits": 10, "winrate": 0.7, "scoreLead": 3.0, "scoreMean": 2.0,
                     "currentPlayer": "W"},
        "ownership": [0.5] + [0.0] * 80,
        "policy": [0.25] + [0.0] * 81,
    }
    report.update(overrides)
    return json.dumps(report)


# -- connecting -------------------------------------------------------------------------
async def test_connect_sends_query_version(analysis_server, connect):
    await connect(analysis_server)
    assert any(q.get("action") == "query_version" for q in queries(analysis_server))


async def test_connect_learns_the_engine_version(analysis_server, connect):
    engine = await connect(analysis_server)
    assert engine.version


async def test_a_gtp_reply_is_refused_with_a_hint_at_the_gtp_protocol(gtp_server, connect):
    with pytest.raises(EngineError, match="(?i)gtp"):
        await connect(gtp_server, protocol="analysis")


# -- the query ---------------------------------------------------------------------------
async def handicap_query(fake_engine, connect, **kwargs) -> dict:
    server = await fake_engine("analysis")
    engine = await connect(server)
    kwargs.setdefault("interval", 0.2)
    await analyse(engine, game_with(9, handicap=2), **kwargs)
    return position_queries(server)[0]


@pytest.mark.parametrize("key, expected", [
    ("boardXSize", 9),
    ("boardYSize", 9),
    ("komi", 0.5),
    ("rules", "japanese"),
    ("initialStones", [["B", "C3"], ["B", "G7"]]),
    ("initialPlayer", "W"),
    ("moves", []),
    ("analyzeTurns", [0]),
    ("includePolicy", True),
    ("maxVisits", 50),
    ("includeOwnership", True),
])
async def test_the_query_carries_the_whole_position(fake_engine, connect, key, expected):
    query = await handicap_query(fake_engine, connect, max_visits=50, include_ownership=True)
    assert query.get(key) == expected


async def test_the_query_asks_for_reports_during_search(fake_engine, connect):
    query = await handicap_query(fake_engine, connect, interval=0.2)
    assert query.get("reportDuringSearchEvery") == pytest.approx(0.2)


async def test_ownership_is_not_asked_for_unless_wanted(fake_engine, connect):
    query = await handicap_query(fake_engine, connect, include_ownership=False)
    assert not query.get("includeOwnership")


async def test_analyze_turns_is_the_last_turn(analysis_server, connect):
    engine = await connect(analysis_server)
    await analyse(engine, game_with(9, "E5", "D4", "C3"))
    assert position_queries(analysis_server)[0]["analyzeTurns"] == [3]


async def test_moves_are_sent_in_order(analysis_server, connect):
    engine = await connect(analysis_server)
    await analyse(engine, game_with(9, "E5", "D4"))
    assert position_queries(analysis_server)[0]["moves"] == [["B", "E5"], ["W", "D4"]]


# -- reports ---------------------------------------------------------------------------------
async def test_partial_reports_come_before_the_final_one(analysis_server, connect):
    engine = await connect(analysis_server)
    reports = await analyse_to_completion(engine, game_with(9, "E5"))
    assert (reports[0].complete, any(r.complete for r in reports.items)) == (False, True)


async def test_the_policy_has_one_entry_per_point_plus_pass(analysis_server, connect):
    engine = await connect(analysis_server)
    reports = await analyse(engine, game_with(9, "E5"))
    assert len(reports[0].policy) == 9 * 9 + 1


async def test_ownership_arrives_when_asked(analysis_server, connect):
    engine = await connect(analysis_server)
    reports = await analyse(engine, game_with(9, "E5"), include_ownership=True)
    assert len(reports[0].ownership) == 81


async def test_the_report_turn_is_the_analysed_turn(analysis_server, connect):
    engine = await connect(analysis_server)
    reports = await analyse(engine, game_with(9, "E5"))
    assert reports[0].turn == 1


async def test_reports_are_valid_json(analysis_server, connect):
    engine = await connect(analysis_server)
    reports = await analyse_to_completion(engine, game_with(9, "E5"), include_ownership=True)
    for report in reports.items:
        json.dumps(report.to_dict(), allow_nan=False)


async def test_a_raising_callback_does_not_kill_the_reader(analysis_server, connect):
    engine = await connect(analysis_server)
    calls = []

    def explode(analysis):
        calls.append(analysis)
        raise RuntimeError("browser went away")

    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), explode,
                                                 interval=0.1), HANG)
    await wait_for(lambda: any(r.complete for r in calls))
    await asyncio.wait_for(engine.stop_analysis(), HANG)
    assert len(calls) >= 2


async def test_the_engine_answers_after_a_raising_callback(analysis_server, connect):
    engine = await connect(analysis_server)

    def explode(analysis):
        raise RuntimeError("browser went away")

    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), explode,
                                                 interval=0.1), HANG)
    await asyncio.sleep(0.3)
    game = game_with(9)
    assert legal(game, "B", await asyncio.wait_for(engine.genmove(position_from(game), "B"),
                                                   HANG))


# -- several queries in flight ------------------------------------------------------------------
async def test_queries_in_flight_are_matched_by_id(fake_engine, connect):
    # The 19x19 query is held back, so its answer arrives after the 5x5 one.
    server = await fake_engine("analysis",
                               query_delay=lambda q: 1.0 if q.get("boardXSize") == 19 else 0.0)
    engine = await connect(server)
    big, small = game_with(19), game_with(5)
    big_move, small_move = await asyncio.wait_for(asyncio.gather(
        engine.genmove(position_from(big), "B"), engine.genmove(position_from(small), "B")),
        HANG)
    assert (legal(big, "B", big_move), legal(small, "B", small_move)) == (True, True)


async def test_an_error_for_one_query_leaves_the_others_running(analysis_server, connect):
    engine = await connect(analysis_server)
    broken = position_from(game_with(9, "E5"))
    broken.moves.append(["W", "E5"])  # the fake refuses an occupied point
    good = game_with(9, "E5")
    results = await asyncio.wait_for(asyncio.gather(
        engine.genmove(broken, "B"), engine.genmove(position_from(good), "W"),
        return_exceptions=True), HANG)
    assert (isinstance(results[0], EngineError), legal(good, "W", results[1])) == (True, True)


async def test_an_illegal_query_is_an_engine_error(analysis_server, connect):
    engine = await connect(analysis_server)
    broken = position_from(game_with(9, "E5"))
    broken.moves.append(["W", "E5"])
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.genmove(broken, "B"), HANG)


async def test_the_connection_carries_on_after_a_query_error(analysis_server, connect):
    engine = await connect(analysis_server)
    broken = position_from(game_with(9, "E5"))
    broken.moves.append(["W", "E5"])
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.genmove(broken, "B"), HANG)
    game = game_with(9, "E5")
    assert legal(game, "W", await asyncio.wait_for(engine.genmove(position_from(game), "W"),
                                                   HANG))


# -- restarting --------------------------------------------------------------------------------
async def restart(fake_engine, connect, **options):
    server = await fake_engine("analysis", **options)
    engine = await connect(server)
    first, second = Reports(), Reports()
    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), first,
                                                 interval=0.1, max_visits=100000), HANG)
    await first.at_least(1)
    await asyncio.wait_for(engine.start_analysis(position_from(game_with(13)), second,
                                                 interval=0.1, max_visits=100000), HANG)
    at_restart = len(first)
    await second.at_least(2)
    await asyncio.sleep(0.3)
    await asyncio.wait_for(engine.stop_analysis(), HANG)
    return server, first, second, at_restart


async def test_restarting_terminates_the_previous_query(fake_engine, connect):
    server, _, _, _ = await restart(fake_engine, connect)
    first_id = position_queries(server)[0]["id"]
    assert any(q.get("action") == "terminate" and q.get("terminateId") == first_id
               for q in queries(server))


async def test_a_terminated_query_reports_no_more(fake_engine, connect):
    _, first, _, at_restart = await restart(fake_engine, connect, ignore_interrupt=True)
    assert len(first) == at_restart


async def test_reports_of_a_terminated_query_do_not_reach_the_new_one(fake_engine, connect):
    _, _, second, _ = await restart(fake_engine, connect, ignore_interrupt=True)
    assert all(len(r.policy) == 13 * 13 + 1 for r in second.items)


# -- engine moves ------------------------------------------------------------------------------
async def test_genmove_plays_a_legal_move(analysis_server, connect):
    engine = await connect(analysis_server)
    game = game_with(9, "E5")
    assert legal(game, "W", await asyncio.wait_for(engine.genmove(position_from(game), "W"),
                                                   HANG))


async def test_genmove_for_the_side_not_to_move_is_refused(analysis_server, connect):
    engine = await connect(analysis_server)
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.genmove(position_from(game_with(9, "E5")), "B"), HANG)


async def test_genmove_in_a_handicap_game_is_for_white(analysis_server, connect):
    engine = await connect(analysis_server)
    game = game_with(9, handicap=2)
    assert legal(game, "W", await asyncio.wait_for(engine.genmove(position_from(game), "W"),
                                                   HANG))


@pytest.mark.parametrize("max_visits", [1, 37, 1_000_000])
async def test_genmove_searches_with_the_max_visits_it_is_given(analysis_server, connect,
                                                                max_visits):
    """§3.4: the session passes an explicit max visits with every engine move."""
    engine = await connect(analysis_server)
    await asyncio.wait_for(engine.genmove(position_from(game_with(9)), "B",
                                          max_visits=max_visits), HANG)
    assert position_queries(analysis_server)[-1]["maxVisits"] == max_visits


# -- perspective -------------------------------------------------------------------------------
async def white_report(fake_engine, connect, line=None):
    engine = await connect(await fake_engine("analysis", emit_line=line or report_line()))
    reports = await analyse(engine, game_with(9, handicap=2), include_ownership=True)
    return reports[0]


async def test_white_to_move_winrate_is_flipped_to_black(fake_engine, connect):
    report = await white_report(fake_engine, connect)
    assert report.move_infos[0].winrate == pytest.approx(0.3)


async def test_white_to_move_score_lead_is_flipped_to_black(fake_engine, connect):
    report = await white_report(fake_engine, connect)
    assert report.move_infos[0].score_lead == pytest.approx(-3.0)


async def test_white_to_move_root_is_flipped_to_black(fake_engine, connect):
    report = await white_report(fake_engine, connect)
    assert (report.root.winrate, report.root.score_lead, report.root.score_mean) == (
        pytest.approx(0.3), pytest.approx(-3.0), pytest.approx(-2.0))


async def test_white_to_move_ownership_is_flipped_to_black(fake_engine, connect):
    report = await white_report(fake_engine, connect)
    assert report.ownership[0] == pytest.approx(-0.5)


async def test_the_side_to_move_is_known_without_current_player(fake_engine, connect):
    root = {"visits": 10, "winrate": 0.7, "scoreLead": 3.0, "scoreMean": 2.0}
    report = await white_report(fake_engine, connect, report_line(rootInfo=root))
    assert report.move_infos[0].winrate == pytest.approx(0.3)


async def test_white_to_move_report_names_the_current_player(fake_engine, connect):
    report = await white_report(fake_engine, connect)
    assert report.current_player == "W"


async def test_the_policy_is_not_flipped(fake_engine, connect):
    report = await white_report(fake_engine, connect)
    assert report.policy[0] == pytest.approx(0.25)


# -- sanitising (§2.2) --------------------------------------------------------------------------
async def nonfinite_report(fake_engine, connect):
    engine = await connect(await fake_engine("analysis", nonfinite=True))
    return (await analyse(engine, game_with(9), include_ownership=True))[0]


async def test_a_nonfinite_report_is_valid_json(fake_engine, connect):
    report = await nonfinite_report(fake_engine, connect)
    json.dumps(report.to_dict(), allow_nan=False)


async def test_nan_becomes_null(fake_engine, connect):
    report = await nonfinite_report(fake_engine, connect)
    assert report.move_infos[0].winrate is None


async def test_infinity_becomes_null(fake_engine, connect):
    report = await nonfinite_report(fake_engine, connect)
    assert report.move_infos[0].score_lead is None


async def test_an_overflowing_number_becomes_null(fake_engine, connect):
    report = await nonfinite_report(fake_engine, connect)
    assert report.move_infos[0].score_mean is None


async def test_a_nonfinite_ownership_value_becomes_zero(fake_engine, connect):
    report = await nonfinite_report(fake_engine, connect)
    assert report.ownership[0] == 0


async def test_a_nonfinite_policy_value_becomes_zero(fake_engine, connect):
    report = await nonfinite_report(fake_engine, connect)
    assert report.policy[0] == 0


async def test_an_off_board_candidate_is_dropped(fake_engine, connect):
    infos = [{"move": "J10", "visits": 10, "winrate": 0.5, "order": 0, "pv": ["J10"]},
             {"move": "E5", "visits": 5, "winrate": 0.5, "order": 1, "pv": ["E5", "J10", "pass"]}]
    report = await white_report(fake_engine, connect, report_line(moveInfos=infos))
    assert [m.move for m in report.move_infos] == ["E5"]


async def test_an_off_board_pv_vertex_is_dropped(fake_engine, connect):
    infos = [{"move": "E5", "visits": 5, "winrate": 0.5, "order": 0, "pv": ["E5", "J10", "pass"]}]
    report = await white_report(fake_engine, connect, report_line(moveInfos=infos))
    assert report.move_infos[0].pv == ["E5", "pass"]


async def test_a_malformed_field_does_not_end_the_stream(fake_engine, connect):
    line = report_line(turnNumber=[1], moveInfos=[{"move": "E5", "visits": "many",
                                                   "order": {"x": 1}}])
    engine = await connect(await fake_engine("analysis", emit_line=line))
    reports = await analyse_to_completion(engine, game_with(9))
    assert any(r.complete for r in reports.items)


async def test_reports_after_a_malformed_field_are_valid_json(fake_engine, connect):
    line = report_line(turnNumber=[1], moveInfos=[{"move": "E5", "visits": "many",
                                                   "order": {"x": 1}}])
    engine = await connect(await fake_engine("analysis", emit_line=line))
    reports = await analyse_to_completion(engine, game_with(9))
    for report in reports.items:
        json.dumps(report.to_dict(), allow_nan=False)


async def test_an_unreadable_line_is_skipped_with_a_note(fake_engine, connect):
    log = Log()
    engine = await connect(await fake_engine("analysis", emit_line="this is not json"), log=log)
    reports = await analyse_to_completion(engine, game_with(9))
    assert (any(r.complete for r in reports.items), bool(log.notes)) == (True, True)


async def test_an_overlong_line_is_an_engine_error(fake_engine, connect):
    engine = await connect(await fake_engine("analysis", overlong_line="query"))
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.genmove(position_from(game_with(9)), "B"), HANG)


# -- lost connection ------------------------------------------------------------------------------
async def test_hangup_mid_query_is_connection_closed(fake_engine, connect):
    engine = await connect(await fake_engine("analysis", hangup_on="query"))
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(engine.genmove(position_from(game_with(9)), "B"), HANG)


async def test_hangup_mid_query_carries_the_address_separately(fake_engine, connect):
    server = await fake_engine("analysis", hangup_on="query")
    engine = await connect(server)
    try:
        await asyncio.wait_for(engine.genmove(position_from(game_with(9)), "B"), HANG)
    except EngineError as error:
        assert (error.address, str(server.port) in str(error)) == ((HOST, server.port), False)
    else:
        pytest.fail("genmove survived a hang-up")


async def analysing_until_hangup(fake_engine, connect, log=None):
    server = await fake_engine("analysis", hangup_after_reports=1)
    engine = await connect(server, log=log)
    disconnects = Disconnects()
    engine.on_disconnect = disconnects
    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), Reports(),
                                                 interval=0.1), HANG)
    await disconnects.fired()
    return engine, disconnects, server


async def test_hangup_mid_stream_is_reported(fake_engine, connect):
    _, disconnects, _ = await analysing_until_hangup(fake_engine, connect)
    assert len(disconnects.errors) == 1


async def test_hangup_mid_stream_is_reported_only_once(fake_engine, connect):
    engine, disconnects, _ = await analysing_until_hangup(fake_engine, connect)
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.stop_analysis(), HANG)
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.genmove(position_from(game_with(9)), "B"), HANG)
    await asyncio.wait_for(engine.close(), HANG)
    await asyncio.sleep(0.2)
    assert len(disconnects.errors) == 1


async def test_the_disconnect_callback_gets_an_engine_error(fake_engine, connect):
    _, disconnects, _ = await analysing_until_hangup(fake_engine, connect)
    assert isinstance(disconnects.errors[0], EngineError)


async def test_after_a_hangup_calls_fail_with_connection_closed_at_once(fake_engine, connect):
    engine, _, _ = await analysing_until_hangup(fake_engine, connect)
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(engine.genmove(position_from(game_with(9)), "B"), 1.0)


async def test_notes_never_contain_the_address(fake_engine, connect):
    log = Log()
    _, _, server = await analysing_until_hangup(fake_engine, connect, log=log)
    assert not [n for n in log.notes if str(server.port) in n or HOST in n]


async def test_a_hangup_while_idle_is_reported(fake_engine, connect):
    server = await fake_engine("analysis")
    engine = await connect(server)
    disconnects = Disconnects()
    engine.on_disconnect = disconnects
    await asyncio.wait_for(server.stop(), HANG)
    assert await disconnects.fired()


async def test_the_disconnect_callback_may_close_the_engine(fake_engine, connect):
    engine = await connect(await fake_engine("analysis", hangup_after_reports=1))
    closed = asyncio.Event()

    async def on_disconnect(error):
        await engine.close()
        closed.set()

    engine.on_disconnect = on_disconnect
    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), Reports(),
                                                 interval=0.1), HANG)
    await asyncio.wait_for(closed.wait(), HANG)


async def test_close_after_a_hangup_does_not_hang(fake_engine, connect):
    engine, _, _ = await analysing_until_hangup(fake_engine, connect)
    await asyncio.wait_for(engine.close(), HANG)


# -- per-point arrays and the turn (§2.2) ------------------------------------------------------------
async def emitted_report(fake_engine, connect, **overrides):
    engine = await connect(await fake_engine("analysis", emit_line=report_line(**overrides)))
    return (await analyse(engine, game_with(9), include_ownership=True))[0]


@pytest.mark.parametrize("length", [80, 82, 361])
async def test_ownership_of_the_wrong_length_is_dropped(fake_engine, connect, length):
    report = await emitted_report(fake_engine, connect, ownership=[0.5] * length)
    assert report.ownership == []


@pytest.mark.parametrize("length", [81, 83, 362])
async def test_a_policy_of_the_wrong_length_is_dropped(fake_engine, connect, length):
    report = await emitted_report(fake_engine, connect, policy=[0.25] * length)
    assert report.policy == []


async def test_per_point_arrays_of_the_right_length_are_kept(fake_engine, connect):
    report = await emitted_report(fake_engine, connect)
    assert (len(report.ownership), len(report.policy)) == (81, 82)


@pytest.mark.parametrize("reported, expected", [(1e308, 10000), (10001, 10000), (-5, 0),
                                                (42, 42)])
async def test_the_turn_is_clamped(fake_engine, connect, reported, expected):
    report = await emitted_report(fake_engine, connect, turnNumber=reported)
    assert report.turn == expected


# -- bounded sends (§2.1) -------------------------------------------------------------------------
def flood_position() -> Position:
    """A position whose query is more than a peer that never reads can absorb."""
    return Position(size=9, komi=6.5, rules="j" * (16 * 1024 * 1024), initial_stones=[],
                    moves=[])


async def test_a_send_the_engine_never_accepts_is_an_engine_error(fake_engine, connect):
    engine = await connect(await fake_engine("analysis", never_read=True))
    engine.send_timeout = 0.5
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.start_analysis(flood_position(), Reports()), HANG)


async def test_a_send_the_engine_never_accepts_leaves_the_connection_unusable(fake_engine,
                                                                              connect):
    engine = await connect(await fake_engine("analysis", never_read=True))
    engine.send_timeout = 0.5
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.start_analysis(flood_position(), Reports()), HANG)
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), Reports()),
                               1.0)


async def test_a_query_send_is_bounded_by_the_query_timeout(fake_engine, connect):
    engine = await connect(await fake_engine("analysis", never_read=True))
    engine.query_timeout = 1.0
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.genmove(flood_position(), "B"), HANG)


@pytest.mark.parametrize("operation", ["genmove", "start_analysis", "stop_analysis", "close"])
async def test_nothing_hangs_behind_a_send_the_engine_never_accepts(fake_engine, connect,
                                                                    operation):
    engine = await connect(await fake_engine("analysis", never_read=True))
    engine.send_timeout = 0.5
    stuck = asyncio.create_task(engine.start_analysis(flood_position(), Reports()))
    await asyncio.sleep(0.1)
    position = position_from(game_with(9))
    calls = {
        "genmove": lambda: engine.genmove(position, "B"),
        "start_analysis": lambda: engine.start_analysis(position, Reports()),
        "stop_analysis": engine.stop_analysis,
        "close": engine.close,
    }
    try:
        with contextlib.suppress(EngineError):
            await asyncio.wait_for(calls[operation](), HANG)
    finally:
        stuck.cancel()
        await asyncio.gather(stuck, return_exceptions=True)


# -- reconnecting (§2.1) ----------------------------------------------------------------------------
async def test_a_reconnect_while_connected_closes_the_old_connection(analysis_server, connect):
    engine = await connect(analysis_server)
    await asyncio.wait_for(engine.connect(), HANG)
    assert await wait_for(lambda: analysis_server.open_connections == 1)


async def test_a_reconnect_to_another_engine_analyses_there(fake_engine, connect):
    first, second = await fake_engine("analysis"), await fake_engine("analysis")
    engine = await connect(first)
    engine.port = second.port
    await asyncio.wait_for(engine.connect(), HANG)
    await analyse(engine, game_with(9))
    assert position_queries(second)


# -- callback-failure notes (§2.1) ------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_a_failing_disconnect_handler_is_noted_by_type_only(fake_engine, connect, kind):
    log = Log()
    server = await fake_engine("analysis")
    engine = await connect(server, log=log)
    secret = f"{HOST}:{server.port} secret-detail"
    called = asyncio.Event()

    def sync_handler(error):
        called.set()
        raise OSError(secret)

    async def async_handler(error):
        called.set()
        raise OSError(secret)

    engine.on_disconnect = sync_handler if kind == "sync" else async_handler
    await asyncio.wait_for(server.stop(), HANG)
    await asyncio.wait_for(called.wait(), HANG)
    assert await wait_for(lambda: any("OSError" in n for n in log.notes))
    assert not [n for n in log.notes if "secret-detail" in n or str(server.port) in n]
