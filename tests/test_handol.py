"""The handol-mux human-policy client (SPEC §2.5) against the fake handol surface (§2.6) over real
TCP: the wire rules, the request/reply channel, idle close and resend, latest position wins, the
winrate merge, and engine moves."""

from __future__ import annotations

import asyncio
import json
import math
import random

import pytest

from gowui import coords
from gowui.engine import PROTOCOLS, EngineError, Position, create_engine
from gowui.engine.handol import HandolEngine, engine_to_play
from gowui.engine.types import board_vertex
from helpers import (HANG, Disconnects, Log, RawClient, Reports, color_of, game_with,
                     position_from, queries, wait_for)

HOST = "127.0.0.1"
POSITION_KEYS = {"boardXSize", "boardYSize", "komi", "rules", "initialStones", "moves"}
LAMBDA = {"lambda_utility": 0.1, "trust_mu": 0.05, "fill_kappa": 1}
SETTINGS = dict(profile="preaz_1d", policy={}, compare=None, eval_visits=20, max_visits=10,
                move_style={"B": "human", "W": "human"})
EMPTY = position_from(game_with(9))


# -- helpers ---------------------------------------------------------------------------------
def human_requests(server) -> list[dict]:
    return [q for q in queries(server) if "human" in q]


def plain_requests(server) -> list[dict]:
    return [q for q in queries(server) if "human" not in q]


def connections(server, human: bool) -> list[int]:
    """The connection index of every human (or plain) request, in order."""
    out = []
    for index, line in server.connection_requests:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and ("human" in value) == human:
            out.append(index)
    return out


def human_replies(server) -> list[str]:
    return [line for _, line in server.replies if '"policies"' in line]


def legal(game, color_letter: str, vertex: str) -> bool:
    try:
        point = coords.from_gtp(vertex, game.size)
    except ValueError:
        return False
    return game.legal_error(color_of(color_letter), point) is None


def key(move: str) -> str:
    return str(move).upper()


class FirstOnly:
    """A fake ``query_delay``: hold back only the first human request (or the first plain one)."""

    def __init__(self, seconds: float, human: bool = True) -> None:
        self.seconds = seconds
        self.human = human
        self.seen = False

    def __call__(self, request: dict) -> float:
        if ("human" in request) != self.human or self.seen:
            return 0.0
        self.seen = True
        return self.seconds


async def handol(connect, server, log=None, **settings) -> HandolEngine:
    engine = await connect(server, log=log)
    engine.configure(**{**SETTINGS, **settings})
    return engine


async def analyse(engine, position: Position, max_visits: int = 10, **kwargs) -> Reports:
    reports = Reports()
    await asyncio.wait_for(engine.start_analysis(position, reports, max_visits=max_visits,
                                                 **kwargs), HANG)
    await reports.at_least(1)
    return reports


async def replay(server, request: dict) -> dict:
    """Send a request the client sent once more over a raw connection; return the answer."""
    client = await RawClient.open(server.port)
    try:
        await client.json(request)
        return await client.json_line()
    finally:
        await client.close()


async def genmove(engine, position: Position, color: str) -> str:
    return await asyncio.wait_for(engine.genmove(position, color), HANG)


# -- registry and capabilities ------------------------------------------------------------------
def test_handol_is_a_registered_protocol():
    assert PROTOCOLS["handol"] is HandolEngine


def test_create_engine_makes_a_handol_client():
    assert isinstance(create_engine("handol", HOST, 1), HandolEngine)


@pytest.mark.parametrize("flag, expected", [
    ("supports_genmove", True),
    ("supports_final_score", False),
    ("supports_raw", False),
])
def test_the_capability_flags(flag, expected):
    assert getattr(HandolEngine(HOST, 1), flag) is expected


def test_the_default_read_timeout_is_600_seconds():
    assert HandolEngine(HOST, 1).read_timeout == 600


async def test_connect_sends_nothing(handol_server, connect):
    await connect(handol_server)
    await asyncio.sleep(0.1)
    assert handol_server.requests == []


async def test_configure_only_stores_and_sends_nothing(handol_server, connect):
    await handol(connect, handol_server)
    await asyncio.sleep(0.1)
    assert handol_server.requests == []


# -- side to move -----------------------------------------------------------------------------
def position(stones=(), moves=(), first="B") -> Position:
    return Position(size=9, komi=6.5, rules="japanese", initial_stones=[list(s) for s in stones],
                    moves=[list(m) for m in moves], first_player=first)


@pytest.mark.parametrize("pos, expected", [
    (position(), "B"),
    (position(moves=[("B", "E5")]), "W"),
    (position(moves=[("W", "E5")]), "B"),
    (position(stones=[("B", "C3")]), "B"),
    (position(stones=[("B", "C3"), ("B", "G7")]), "W"),
    (position(stones=[("B", "C3"), ("B", "G7"), ("W", "E5")]), "B"),
    (position(stones=[("W", "C3"), ("W", "G7")]), "B"),
    (position(stones=[("B", "C3"), ("B", "G7")], moves=[("W", "E5")]), "B"),
])
def test_engine_to_play_is_what_katago_infers(pos, expected):
    assert engine_to_play(pos) == expected


UNANALYSABLE = [
    position(stones=[("B", "C3"), ("B", "G7")], first="B"),
    position(stones=[("B", "C3")], first="W"),
    position(stones=[("B", "C3"), ("B", "G7"), ("W", "E5")], first="W"),
    position(first="W"),
]


@pytest.mark.parametrize("pos", UNANALYSABLE)
async def test_an_unanalysable_setup_is_refused_by_start_analysis(handol_server, connect, pos):
    engine = await handol(connect, handol_server)
    with pytest.raises(EngineError, match="(?i)play a move first"):
        await asyncio.wait_for(engine.start_analysis(pos, Reports(), max_visits=10), HANG)


@pytest.mark.parametrize("pos", UNANALYSABLE)
async def test_an_unanalysable_setup_sends_nothing(handol_server, connect, pos):
    engine = await handol(connect, handol_server)
    try:
        await asyncio.wait_for(engine.start_analysis(pos, Reports(), max_visits=10), HANG)
    except EngineError:
        pass
    await asyncio.sleep(0.2)
    assert handol_server.requests == []


@pytest.mark.parametrize("pos", UNANALYSABLE)
async def test_an_unanalysable_setup_is_refused_by_genmove(handol_server, connect, pos):
    engine = await handol(connect, handol_server)
    with pytest.raises(EngineError):
        await genmove(engine, pos, pos.to_play)


async def test_a_handicap_position_with_white_to_move_is_sent(handol_server, connect):
    engine = await handol(connect, handol_server)
    await analyse(engine, position_from(game_with(9, handicap=2)))
    assert human_requests(handol_server)[0]["initialStones"] == [["B", "C3"], ["B", "G7"]]


async def test_a_handicap_analysis_is_for_white(handol_server, connect):
    engine = await handol(connect, handol_server)
    reports = await analyse(engine, position_from(game_with(9, handicap=2)))
    assert reports[0].current_player == "W"


# -- AC1: the human request --------------------------------------------------------------------
async def test_a_human_request_carries_exactly_id_the_position_and_human(handol_server, connect):
    engine = await handol(connect, handol_server)
    await analyse(engine, position_from(game_with(9, "E5")), max_visits=50)
    assert set(human_requests(handol_server)[0]) == {"id", "human"} | POSITION_KEYS


async def test_the_human_block_carries_profile_tuple_and_search(handol_server, connect):
    engine = await handol(connect, handol_server, profile="rank_5k", policy={"min_p": 0.05},
                          max_visits=50)
    await analyse(engine, EMPTY, max_visits=50)
    assert human_requests(handol_server)[0]["human"] == {
        "profile": "rank_5k", "policies": [{"min_p": 0.05}], "search": {"visits": 50}}


async def test_max_visits_one_leaves_the_search_out(handol_server, connect):
    engine = await handol(connect, handol_server, max_visits=1)
    await analyse(engine, EMPTY, max_visits=1)
    assert "search" not in human_requests(handol_server)[0]["human"]


async def test_the_position_is_sent_as_is(handol_server, connect):
    engine = await handol(connect, handol_server)
    await analyse(engine, position_from(game_with(9, "E5", "D4")))
    request = human_requests(handol_server)[0]
    assert {k: request[k] for k in POSITION_KEYS} == {
        "boardXSize": 9, "boardYSize": 9, "komi": EMPTY.komi, "rules": EMPTY.rules,
        "initialStones": [], "moves": [["B", "E5"], ["W", "D4"]]}


async def test_a_null_lambda_is_left_out_of_the_wire_tuple(handol_server, connect):
    engine = await handol(connect, handol_server, policy={"lambda_utility": None, "min_p": 0.05})
    await analyse(engine, EMPTY)
    assert human_requests(handol_server)[0]["human"]["policies"] == [{"min_p": 0.05}]


async def test_the_candidates_are_the_moves_with_positive_p(handol_server, connect):
    engine = await handol(connect, handol_server)
    reports = await analyse(engine, EMPTY)
    assert reports[0].move_infos and all((m.prior or 0) > 0 for m in reports[0].move_infos)


async def test_the_candidates_are_most_likely_first(handol_server, connect):
    engine = await handol(connect, handol_server)
    reports = await analyse(engine, EMPTY)
    priors = [m.prior for m in reports[0].move_infos]
    assert priors == sorted(priors, reverse=True)


async def test_there_are_at_most_twenty_candidates(handol_server, connect):
    engine = await handol(connect, handol_server)
    reports = await analyse(engine, position_from(game_with(19)))
    assert len(reports[0].move_infos) <= 20


async def test_a_candidate_prior_is_its_p(handol_server, connect):
    engine = await handol(connect, handol_server)
    reports = await analyse(engine, EMPTY)
    answer = await replay(handol_server, human_requests(handol_server)[0])
    ps = {key(e["move"]): e["p"] for e in answer["policies"][0]["distribution"]}
    got = {key(m.move): m.prior for m in reports[0].move_infos}
    assert got == pytest.approx({k: ps[k] for k in got})


async def test_the_policy_fills_every_point_and_pass(handol_server, connect):
    engine = await handol(connect, handol_server)
    reports = await analyse(engine, EMPTY)
    assert len(reports[0].policy) == 9 * 9 + 1


async def test_the_analysis_is_for_the_side_to_move(handol_server, connect):
    engine = await handol(connect, handol_server)
    reports = await analyse(engine, position_from(game_with(9, "E5")))
    assert reports[0].current_player == "W"


# -- AC1: compare ---------------------------------------------------------------------------------
async def test_a_compare_tuple_goes_in_the_same_query(handol_server, connect):
    engine = await handol(connect, handol_server, policy={"min_p": 0.05},
                          compare={"distance_slope": 1})
    await analyse(engine, EMPTY)
    assert human_requests(handol_server)[0]["human"]["policies"] == [
        {"min_p": 0.05}, {"distance_slope": 1}]


async def test_the_compare_distribution_arrives_as_compare(handol_server, connect):
    engine = await handol(connect, handol_server, compare={"distance_slope": 1})
    reports = await analyse(engine, EMPTY)
    assert set(reports[0].compare) == {"policy", "moveInfos", "probabilities"}


async def test_the_compare_policy_fills_every_point_and_pass(handol_server, connect):
    engine = await handol(connect, handol_server, compare={"distance_slope": 1})
    reports = await analyse(engine, EMPTY)
    assert len(reports[0].compare["policy"]) == 9 * 9 + 1


async def test_without_a_compare_tuple_there_is_no_compare(handol_server, connect):
    engine = await handol(connect, handol_server)
    reports = await analyse(engine, EMPTY)
    assert reports[0].compare is None


# -- AC1: the winrate query ------------------------------------------------------------------------
async def test_a_plain_query_carries_exactly_id_the_position_and_max_visits(handol_server,
                                                                              connect):
    engine = await handol(connect, handol_server)
    await analyse(engine, EMPTY)
    await wait_for(lambda: plain_requests(handol_server))
    assert set(plain_requests(handol_server)[0]) == {"id", "maxVisits"} | POSITION_KEYS


async def test_the_plain_query_uses_the_eval_visits(handol_server, connect):
    engine = await handol(connect, handol_server, eval_visits=37)
    await analyse(engine, EMPTY)
    await wait_for(lambda: plain_requests(handol_server))
    assert plain_requests(handol_server)[0]["maxVisits"] == 37


async def test_the_plain_query_asks_for_ownership_when_wanted(handol_server, connect):
    engine = await handol(connect, handol_server)
    await analyse(engine, EMPTY, include_ownership=True)
    await wait_for(lambda: plain_requests(handol_server))
    assert plain_requests(handol_server)[0].get("includeOwnership") is True


async def test_eval_visits_zero_sends_no_plain_query(handol_server, connect):
    engine = await handol(connect, handol_server, eval_visits=0)
    await analyse(engine, EMPTY)
    await asyncio.sleep(0.2)
    assert plain_requests(handol_server) == []


async def test_eval_visits_zero_leaves_the_winrate_empty(handol_server, connect):
    engine = await handol(connect, handol_server, eval_visits=0)
    reports = await analyse(engine, EMPTY)
    assert reports[0].root.winrate is None


async def evaluated(server, connect, **settings):
    """One analysis with the winrate query, and the fake's own (White-view) plain answer."""
    engine = await handol(connect, server, **settings)
    reports = Reports()
    await asyncio.wait_for(engine.start_analysis(EMPTY, reports, max_visits=10,
                                                 include_ownership=True), HANG)
    await wait_for(lambda: any(r.root.winrate is not None for r in reports.items))
    analysis = next(r for r in reports.items if r.root.winrate is not None)
    raw = await replay(server, plain_requests(server)[0])
    return analysis, raw


async def test_the_root_winrate_is_flipped_to_black(handol_server, connect):
    analysis, raw = await evaluated(handol_server, connect)
    assert analysis.root.winrate == pytest.approx(1 - raw["rootInfo"]["winrate"])


async def test_the_root_score_lead_is_flipped_to_black(handol_server, connect):
    analysis, raw = await evaluated(handol_server, connect)
    assert analysis.root.score_lead == pytest.approx(-raw["rootInfo"]["scoreLead"])


async def test_the_ownership_is_flipped_to_black(handol_server, connect):
    analysis, raw = await evaluated(handol_server, connect)
    assert analysis.ownership == pytest.approx([-v for v in raw["ownership"]])


def winrates(infos) -> dict[str, float]:
    return {key(m["move"]): m["winrate"] for m in infos}


async def test_the_candidate_winrates_are_flipped_to_black(handol_server, connect):
    analysis, raw = await evaluated(handol_server, connect)
    white = winrates(raw["moveInfos"])
    got = {key(m.move): m.winrate for m in analysis.move_infos if key(m.move) in white}
    assert got and got == pytest.approx({k: 1 - white[k] for k in got})


async def test_the_winrates_are_merged_into_the_compare_candidates(handol_server, connect):
    analysis, raw = await evaluated(handol_server, connect, compare={"distance_slope": 1})
    white = winrates(raw["moveInfos"])
    got = {key(m["move"]): m["winrate"] for m in analysis.compare["moveInfos"]
           if key(m["move"]) in white}
    assert got and got == pytest.approx({k: 1 - white[k] for k in got})


async def test_pass_is_matched_regardless_of_case(handol_server, connect):
    # The fake spells pass "pass" in distributions and "PASS" in plain answers (§2.6).
    analysis, raw = await evaluated(handol_server, connect)
    white = winrates(raw["moveInfos"])
    passes = [m.winrate for m in analysis.move_infos if m.move.lower() == "pass"]
    assert passes == [pytest.approx(1 - white["PASS"])]


async def test_a_failed_winrate_query_keeps_the_distribution(fake_engine, connect):
    server = await fake_engine("handol", error_reply="plain")
    engine = await handol(connect, server)
    reports = await analyse(engine, EMPTY)
    assert reports[0].move_infos and reports[0].root.winrate is None


async def test_a_failed_winrate_query_leaves_a_note(fake_engine, connect):
    server = await fake_engine("handol", error_reply="plain")
    log = Log()
    engine = await handol(connect, server, log=log)
    await analyse(engine, EMPTY)
    await wait_for(lambda: log.notes)
    assert log.notes


# -- AC3: settings validation --------------------------------------------------------------------
BAD = [
    dict(policy={"foo": 1}),
    dict(policy={"min_p": 2}),
    dict(policy={"temperature": float("nan")}),
    dict(policy={"lambda_utility": 0.1}),
    dict(compare={"foo": 1}),
    dict(policy=LAMBDA, max_visits=1),
]
#: An engine move carries only the primary tuple (SPEC §2.5), so a bad compare tuple does not
#: refuse it; the bad-compare case is left out of the engine-move refusals.
BAD_FOR_MOVES = [s for s in BAD if "compare" not in s]


@pytest.mark.parametrize("settings", BAD)
async def test_configure_stores_a_bad_setting_without_raising(handol_server, connect, settings):
    engine = await connect(handol_server)
    engine.configure(**{**SETTINGS, **settings})


@pytest.mark.parametrize("settings", BAD)
async def test_start_analysis_refuses_a_bad_tuple(handol_server, connect, settings):
    engine = await handol(connect, handol_server, **settings)
    visits = settings.get("max_visits", 10)
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.start_analysis(EMPTY, Reports(), max_visits=visits), HANG)


@pytest.mark.parametrize("settings", BAD)
async def test_a_refused_analysis_sends_nothing(handol_server, connect, settings):
    engine = await handol(connect, handol_server, **settings)
    visits = settings.get("max_visits", 10)
    try:
        await asyncio.wait_for(engine.start_analysis(EMPTY, Reports(), max_visits=visits), HANG)
    except EngineError:
        pass
    await asyncio.sleep(0.2)
    assert handol_server.requests == []


@pytest.mark.parametrize("settings", BAD_FOR_MOVES)
async def test_genmove_refuses_a_bad_tuple(handol_server, connect, settings):
    engine = await handol(connect, handol_server, **settings)
    with pytest.raises(EngineError):
        await genmove(engine, EMPTY, "B")


@pytest.mark.parametrize("settings", BAD_FOR_MOVES)
async def test_a_refused_genmove_sends_nothing(handol_server, connect, settings):
    engine = await handol(connect, handol_server, **settings)
    try:
        await genmove(engine, EMPTY, "B")
    except EngineError:
        pass
    await asyncio.sleep(0.2)
    assert handol_server.requests == []


async def test_a_lambda_tuple_is_analysed_with_a_search(handol_server, connect):
    engine = await handol(connect, handol_server, policy=LAMBDA, max_visits=50)
    reports = await analyse(engine, EMPTY, max_visits=50)
    assert reports[0].move_infos


async def test_a_lambda_tuple_genmove_searches_with_max_visits(handol_server, connect):
    engine = await handol(connect, handol_server, policy=LAMBDA, max_visits=50)
    await genmove(engine, EMPTY, "B")
    assert human_requests(handol_server)[0]["human"]["search"] == {"visits": 50}


async def test_a_lambda_tuple_genmove_plays_a_legal_move(handol_server, connect):
    engine = await handol(connect, handol_server, policy=LAMBDA, max_visits=50)
    assert legal(game_with(9), "B", await genmove(engine, EMPTY, "B"))


# -- AC2: latest position wins -------------------------------------------------------------------
BURST = ("E5", "D4", "C3")


async def burst(fake_engine, connect):
    """Analyse four positions in a row while the first human query is held back."""
    server = await fake_engine("handol", query_delay=FirstOnly(0.6))
    engine = await handol(connect, server)
    reports = Reports()
    positions = [position_from(game_with(9, *BURST[:n])) for n in range(len(BURST) + 1)]
    await asyncio.wait_for(engine.start_analysis(positions[0], reports, max_visits=10), HANG)
    await wait_for(lambda: len(human_requests(server)) == 1)
    for pos in positions[1:]:
        await asyncio.wait_for(engine.start_analysis(pos, reports, max_visits=10), HANG)
    await wait_for(lambda: len(reports) >= 1 and len(human_replies(server)) >= 2)
    await asyncio.sleep(0.3)
    return server, reports, positions


async def test_a_burst_sends_only_the_first_and_the_last_human_request(fake_engine, connect):
    server, _, positions = await burst(fake_engine, connect)
    assert [q["moves"] for q in human_requests(server)] == [positions[0].moves,
                                                            positions[-1].moves]


async def test_a_burst_sends_only_the_first_and_the_last_plain_request(fake_engine, connect):
    server, _, positions = await burst(fake_engine, connect)
    assert [q["moves"] for q in plain_requests(server)] == [positions[0].moves,
                                                            positions[-1].moves]


async def test_a_burst_keeps_one_query_in_flight_per_connection(fake_engine, connect):
    server, _, _ = await burst(fake_engine, connect)
    assert server.max_in_flight <= 1


async def test_a_burst_delivers_only_the_last_position(fake_engine, connect):
    server, reports, _ = await burst(fake_engine, connect)
    # The first position has Black to move, the last White.
    assert [r.current_player for r in reports.items] == ["W"]


async def test_an_answer_after_stop_analysis_is_dropped(fake_engine, connect):
    server = await fake_engine("handol", query_delay=FirstOnly(0.4))
    engine = await handol(connect, server)
    reports = Reports()
    await asyncio.wait_for(engine.start_analysis(EMPTY, reports, max_visits=10), HANG)
    await wait_for(lambda: len(human_requests(server)) == 1)
    await asyncio.wait_for(engine.stop_analysis(), HANG)
    await wait_for(lambda: human_replies(server))
    await asyncio.sleep(0.3)
    assert len(reports) == 0


# -- engine move while an analysis answer is in flight ---------------------------------------------
async def move_during_analysis(fake_engine, connect):
    """Start analysis, stop it while its answer is held back, then ask for a move at once."""
    server = await fake_engine("handol", query_delay=FirstOnly(0.5))
    engine = await handol(connect, server)
    reports = Reports()
    await asyncio.wait_for(engine.start_analysis(EMPTY, reports, max_visits=10), HANG)
    await wait_for(lambda: len(human_requests(server)) == 1)
    await asyncio.wait_for(engine.stop_analysis(), HANG)
    game = game_with(9, "E5")
    move = await genmove(engine, position_from(game), "W")
    await asyncio.sleep(0.3)
    return server, reports, game, move


async def test_a_move_during_analysis_keeps_one_query_in_flight(fake_engine, connect):
    server, _, _, _ = await move_during_analysis(fake_engine, connect)
    assert server.max_in_flight <= 1


async def test_a_move_during_analysis_gets_its_own_answer(fake_engine, connect):
    _, _, game, move = await move_during_analysis(fake_engine, connect)
    assert legal(game, "W", move)


async def test_a_move_during_analysis_asks_for_its_own_position(fake_engine, connect):
    server, _, _, _ = await move_during_analysis(fake_engine, connect)
    assert human_requests(server)[-1]["moves"] == [["B", "E5"]]


async def test_a_move_during_analysis_delivers_no_analysis(fake_engine, connect):
    _, reports, _, _ = await move_during_analysis(fake_engine, connect)
    assert len(reports) == 0


# -- AC4: idle close and resend ----------------------------------------------------------------
async def idle_round(fake_engine, connect):
    """Analyse, let the fake close both idle connections, analyse again."""
    server = await fake_engine("handol", idle_close=0.3)
    log, disconnects = Log(), Disconnects()
    engine = await handol(connect, server, log=log)
    engine.on_disconnect = disconnects
    await analyse(engine, EMPTY)
    await wait_for(lambda: len(plain_requests(server)) == 1)
    first = set(connections(server, True)) | set(connections(server, False))
    await wait_for(lambda: len(server.idle_closed) >= 2)
    notes_before = len(log.notes)
    reports = Reports()
    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9, "E5")), reports,
                                                 max_visits=10), HANG)
    await wait_for(lambda: any(r.root.winrate is not None for r in reports.items))
    await asyncio.sleep(0.1)
    return server, reports, log, notes_before, disconnects, first


async def test_idle_close_the_fake_closes_both_connections(fake_engine, connect):
    server, *_ = await idle_round(fake_engine, connect)
    assert len(server.idle_closed) >= 2


async def test_after_idle_close_the_next_analysis_arrives_with_its_winrate(fake_engine, connect):
    _, reports, *_ = await idle_round(fake_engine, connect)
    assert any(r.root.winrate is not None for r in reports.items)


async def test_after_idle_close_both_connections_are_reopened(fake_engine, connect):
    server, _, _, _, _, first = await idle_round(fake_engine, connect)
    later = {connections(server, True)[-1], connections(server, False)[-1]}
    assert len(later) == 2 and not later & first


async def test_idle_close_reports_no_disconnect(fake_engine, connect):
    _, _, _, _, disconnects, _ = await idle_round(fake_engine, connect)
    assert disconnects.errors == []


async def test_a_reopen_leaves_a_note(fake_engine, connect):
    _, _, log, before, _, _ = await idle_round(fake_engine, connect)
    assert len(log.notes) > before


async def hang_up_every_time(fake_engine, connect):
    server = await fake_engine("handol", hangup_on="human")
    disconnects = Disconnects()
    engine = await handol(connect, server)
    engine.on_disconnect = disconnects
    error = None
    try:
        await genmove(engine, EMPTY, "B")
    except EngineError as exc:
        error = exc
    await asyncio.sleep(0.3)
    return server, disconnects, error


async def test_a_hangup_on_every_attempt_is_an_engine_error(fake_engine, connect):
    _, _, error = await hang_up_every_time(fake_engine, connect)
    assert isinstance(error, EngineError)


async def test_a_lost_request_is_resent_once(fake_engine, connect):
    server, _, _ = await hang_up_every_time(fake_engine, connect)
    assert len(human_requests(server)) == 2


async def test_a_second_loss_is_reported_once(fake_engine, connect):
    _, disconnects, _ = await hang_up_every_time(fake_engine, connect)
    assert len(disconnects.errors) == 1


async def test_a_single_hangup_is_resent_silently(fake_engine, connect):
    server = await fake_engine("handol", hangup_on="human", fault_times=1)
    engine = await handol(connect, server)
    assert legal(game_with(9), "B", await genmove(engine, EMPTY, "B"))


async def stopped_fake_error(fake_engine, connect) -> tuple[EngineError, int]:
    server = await fake_engine("handol")
    engine = await handol(connect, server)
    await asyncio.wait_for(server.stop(), HANG)
    with pytest.raises(EngineError) as caught:
        await genmove(engine, EMPTY, "B")
    return caught.value, server.port


async def test_a_stopped_fake_is_an_engine_error_without_the_port(fake_engine, connect):
    error, port = await stopped_fake_error(fake_engine, connect)
    assert str(port) not in str(error)


async def test_a_stopped_fake_is_an_engine_error_without_the_host(fake_engine, connect):
    error, _ = await stopped_fake_error(fake_engine, connect)
    assert HOST not in str(error)


# -- the request/reply channel ------------------------------------------------------------------
@pytest.mark.parametrize("with_id", [True, False])
async def test_an_error_reply_is_an_engine_error(fake_engine, connect, with_id):
    # Without an id, the error still answers the request just sent (and nothing hangs).
    server = await fake_engine("handol", error_reply="human", error_reply_id=with_id)
    engine = await handol(connect, server)
    with pytest.raises(EngineError):
        await genmove(engine, EMPTY, "B")


async def test_a_mismatched_id_is_an_engine_error(fake_engine, connect):
    server = await fake_engine("handol", wrong_id_on="human", fault_times=1)
    engine = await handol(connect, server)
    with pytest.raises(EngineError):
        await genmove(engine, EMPTY, "B")


async def after_mismatch(fake_engine, connect):
    server = await fake_engine("handol", wrong_id_on="human", fault_times=1)
    engine = await handol(connect, server)
    try:
        await genmove(engine, EMPTY, "B")
    except EngineError:
        pass
    move = await genmove(engine, EMPTY, "B")
    return server, move


async def test_reopen_notes_name_a_failed_request_then_an_idle_close(fake_engine, connect):
    server = await fake_engine("handol", wrong_id_on="human", fault_times=1, idle_close=0.3)
    log = Log()
    engine = await handol(connect, server, log=log)
    with pytest.raises(EngineError):
        await genmove(engine, EMPTY, "B")
    await genmove(engine, EMPTY, "B")
    reopened = [n for n in log.notes if "reopened" in n]
    assert len(reopened) == 1 and "failed request" in reopened[0], reopened
    closed = len(server.idle_closed)
    await wait_for(lambda: len(server.idle_closed) > closed)
    await genmove(engine, EMPTY, "B")
    reopened = [n for n in log.notes if "reopened" in n]
    assert len(reopened) == 2, reopened
    assert "idle connections" in reopened[1] and "failed request" not in reopened[1], reopened


async def test_after_a_mismatched_id_the_next_request_succeeds(fake_engine, connect):
    _, move = await after_mismatch(fake_engine, connect)
    assert legal(game_with(9), "B", move)


async def test_after_a_mismatched_id_the_next_request_reopens(fake_engine, connect):
    server, _ = await after_mismatch(fake_engine, connect)
    first, second = connections(server, True)[:2]
    assert first != second


async def after_timeout(fake_engine, connect):
    """A reply held past the read timeout, then the next query."""
    server = await fake_engine("handol", query_delay=FirstOnly(1.0))
    engine = await handol(connect, server)
    engine.read_timeout = 0.3
    error = None
    try:
        await genmove(engine, EMPTY, "B")
    except EngineError as exc:
        error = exc
    await asyncio.sleep(0.2)
    sent = len(human_requests(server))
    move = await genmove(engine, EMPTY, "B")
    return server, error, sent, move


async def test_a_read_timeout_is_an_engine_error(fake_engine, connect):
    _, error, _, _ = await after_timeout(fake_engine, connect)
    assert isinstance(error, EngineError)


async def test_a_read_timeout_does_not_resend(fake_engine, connect):
    _, _, sent, _ = await after_timeout(fake_engine, connect)
    assert sent == 1


async def test_after_a_read_timeout_the_next_query_succeeds(fake_engine, connect):
    _, _, _, move = await after_timeout(fake_engine, connect)
    assert legal(game_with(9), "B", move)


async def test_after_a_read_timeout_the_next_query_uses_a_new_connection(fake_engine, connect):
    server, _, _, _ = await after_timeout(fake_engine, connect)
    first, second = connections(server, True)[:2]
    assert first != second


# -- AC5: engine moves ------------------------------------------------------------------------------
async def test_genmove_for_the_wrong_colour_is_an_engine_error(handol_server, connect):
    engine = await handol(connect, handol_server)
    with pytest.raises(EngineError):
        await genmove(engine, EMPTY, "W")


async def test_genmove_for_the_wrong_colour_sends_nothing(handol_server, connect):
    engine = await handol(connect, handol_server)
    try:
        await genmove(engine, EMPTY, "W")
    except EngineError:
        pass
    await asyncio.sleep(0.2)
    assert handol_server.requests == []


async def katago_move(server, connect):
    engine = await handol(connect, server, max_visits=40,
                          move_style={"B": "katago", "W": "human"})
    move = await genmove(engine, EMPTY, "B")
    return engine, move


async def test_katago_style_sends_a_plain_query_with_max_visits(handol_server, connect):
    await katago_move(handol_server, connect)
    assert plain_requests(handol_server)[0]["maxVisits"] == 40


async def test_katago_style_sends_only_the_plain_keys(handol_server, connect):
    await katago_move(handol_server, connect)
    assert set(plain_requests(handol_server)[0]) == {"id", "maxVisits"} | POSITION_KEYS


async def test_katago_style_sends_no_human_request(handol_server, connect):
    await katago_move(handol_server, connect)
    assert human_requests(handol_server) == []


async def test_katago_style_plays_the_move_of_order_zero(handol_server, connect):
    # The fake lists its plain moveInfos out of order (§2.6), so the first listed is not it.
    _, move = await katago_move(handol_server, connect)
    raw = await replay(handol_server, plain_requests(handol_server)[0])
    best = next(m["move"] for m in raw["moveInfos"] if m.get("order") == 0)
    assert key(move) == key(best)


async def test_katago_style_without_moves_is_an_engine_error(fake_engine, connect):
    server = await fake_engine("handol", empty_katago=True)
    engine = await handol(connect, server, move_style={"B": "katago", "W": "katago"})
    with pytest.raises(EngineError):
        await genmove(engine, EMPTY, "B")


async def test_human_style_searches_with_max_visits(handol_server, connect):
    engine = await handol(connect, handol_server, max_visits=30)
    await genmove(engine, EMPTY, "B")
    assert human_requests(handol_server)[0]["human"]["search"] == {"visits": 30}


async def test_human_style_with_max_visits_one_sends_no_search(handol_server, connect):
    engine = await handol(connect, handol_server, max_visits=1)
    await genmove(engine, EMPTY, "B")
    assert "search" not in human_requests(handol_server)[0]["human"]


async def test_human_style_plays_a_move_with_positive_p(handol_server, connect):
    engine = await handol(connect, handol_server)
    engine.rng = random.Random(1)
    move = await genmove(engine, EMPTY, "B")
    answer = await replay(handol_server, human_requests(handol_server)[0])
    ps = {key(e["move"]): e["p"] for e in answer["policies"][0]["distribution"]}
    assert ps.get(key(move), 0) > 0


async def test_human_style_with_the_same_seed_plays_the_same_move(handol_server, connect):
    engine = await handol(connect, handol_server)
    engine.rng = random.Random(7)
    first = await genmove(engine, EMPTY, "B")
    engine.rng = random.Random(7)
    assert await genmove(engine, EMPTY, "B") == first


async def test_human_style_samples_rather_than_taking_the_top_move(handol_server, connect):
    engine = await handol(connect, handol_server)
    moves = set()
    for seed in range(25):
        engine.rng = random.Random(seed)
        moves.add(await genmove(engine, EMPTY, "B"))
    assert len(moves) > 1


async def test_human_style_never_plays_an_illegal_point(fake_engine, connect):
    # The fake puts most of the mass on an occupied point.
    server = await fake_engine("handol", illegal_point=True)
    engine = await handol(connect, server)
    game = game_with(9, "E5", "D4")
    moves = []
    for seed in range(25):
        engine.rng = random.Random(seed)
        moves.append(await genmove(engine, position_from(game), "B"))
    assert all(legal(game, "B", m) for m in moves)


async def test_an_empty_distribution_is_an_engine_error(fake_engine, connect):
    server = await fake_engine("handol", empty_distribution=True)
    engine = await handol(connect, server)
    with pytest.raises(EngineError):
        await genmove(engine, EMPTY, "B")


async def test_a_white_move_is_generated_for_white(handol_server, connect):
    engine = await handol(connect, handol_server, move_style={"B": "katago", "W": "human"})
    game = game_with(9, "E5")
    assert legal(game, "W", await genmove(engine, position_from(game), "W"))


# -- parsing hardening (SPEC §2.5 "Parsing") -------------------------------------------------------
def point_index(move: str, size: int) -> int:
    """Row-major index from the top-left, pass last."""
    point = coords.from_gtp(move, size)
    return size * size if point is None else point[1] * size + point[0]


async def test_fewer_policies_than_tuples_delivers_no_analysis(fake_engine, connect):
    # Two tuples go out, the fake answers with one: an engine error, so nothing is shown.
    server = await fake_engine("handol", short_policies=True)
    engine = await handol(connect, server, compare={"distance_slope": 1}, eval_visits=0)
    reports = Reports()
    await asyncio.wait_for(engine.start_analysis(EMPTY, reports, max_visits=10), HANG)
    await wait_for(lambda: human_replies(server))
    await asyncio.sleep(0.3)
    assert len(reports) == 0


async def test_fewer_policies_than_tuples_leaves_a_note(fake_engine, connect):
    server = await fake_engine("handol", short_policies=True)
    log = Log()
    engine = await handol(connect, server, log=log, eval_visits=0)
    await asyncio.wait_for(engine.start_analysis(EMPTY, Reports(), max_visits=10), HANG)
    await wait_for(lambda: log.notes)
    assert log.notes


async def test_an_off_board_entry_is_not_a_candidate(fake_engine, connect):
    # The fake gives its off-board entries the largest p.
    server = await fake_engine("handol", offboard_move=True)
    engine = await handol(connect, server, eval_visits=0)
    reports = await analyse(engine, EMPTY)
    moves = [m.move for m in reports[0].move_infos]
    assert moves and all(board_vertex(m, 9) is not None for m in moves)


async def test_an_off_board_entry_is_not_a_compare_probability(fake_engine, connect):
    server = await fake_engine("handol", offboard_move=True)
    engine = await handol(connect, server, compare={"distance_slope": 1}, eval_visits=0)
    reports = await analyse(engine, EMPTY)
    probabilities = reports[0].compare["probabilities"]
    assert probabilities and all(board_vertex(m, 9) is not None for m in probabilities)


async def test_an_off_board_entry_is_never_played(fake_engine, connect):
    server = await fake_engine("handol", offboard_move=True)
    engine = await handol(connect, server)
    moves = []
    for seed in range(25):
        engine.rng = random.Random(seed)
        moves.append(await genmove(engine, EMPTY, "B"))
    assert all(legal(game_with(9), "B", m) for m in moves)


async def bad_p_round(fake_engine, connect):
    server = await fake_engine("handol", bad_p=True)
    engine = await handol(connect, server, eval_visits=0)
    reports = await analyse(engine, EMPTY)
    answer = await replay(server, human_requests(server)[0])
    bad = [e["move"] for e in answer["policies"][0]["distribution"]
           if not (math.isfinite(e["p"]) and e["p"] >= 0)]
    return engine, reports[0], bad


async def test_the_fake_sends_three_bad_ps(fake_engine, connect):
    _, _, bad = await bad_p_round(fake_engine, connect)
    assert len(bad) == 3


async def test_a_bad_p_is_zero_in_the_policy(fake_engine, connect):
    _, analysis, bad = await bad_p_round(fake_engine, connect)
    assert [analysis.policy[point_index(m, 9)] for m in bad] == [0, 0, 0]


async def test_the_policy_holds_only_finite_non_negative_numbers(fake_engine, connect):
    _, analysis, _ = await bad_p_round(fake_engine, connect)
    assert all(math.isfinite(v) and v >= 0 for v in analysis.policy)


async def test_a_bad_p_is_not_a_candidate(fake_engine, connect):
    _, analysis, bad = await bad_p_round(fake_engine, connect)
    assert not {key(m.move) for m in analysis.move_infos} & {key(m) for m in bad}


async def test_a_bad_p_is_never_played(fake_engine, connect):
    engine, _, bad = await bad_p_round(fake_engine, connect)
    moves = []
    for seed in range(25):
        engine.rng = random.Random(seed)
        moves.append(await genmove(engine, EMPTY, "B"))
    assert not {key(m) for m in moves} & {key(m) for m in bad}


# -- review round 1 ----------------------------------------------------------------------------------
class Scripted:
    """A handol surface whose answers the test writes: ``await answer(request)`` returns the reply
    lines (raw text) for each request line, in order."""

    protocol = "handol"

    def __init__(self, answer) -> None:
        self.answer = answer
        self.port = 0
        self.requests: list[dict] = []
        self.accepted = 0
        self._writers: set = set()
        self._tasks: set = set()
        self._server = None

    async def start(self) -> "Scripted":
        self._server = await asyncio.start_server(self._client, HOST, 0, limit=16 * 1024 * 1024)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    @property
    def open_connections(self) -> int:
        return len(self._writers)

    async def _client(self, reader, writer) -> None:
        self.accepted += 1
        self._writers.add(writer)
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                request = json.loads(raw)
                self.requests.append(request)
                for line in await self.answer(request):
                    writer.write((line + "\n").encode())
                await writer.drain()
        except (asyncio.CancelledError, ConnectionError, OSError, ValueError):
            pass
        finally:
            self._writers.discard(writer)
            self._tasks.discard(task)
            writer.close()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.close()
        tasks = [t for t in self._tasks if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
async def scripted():
    """``await scripted(answer)`` starts a :class:`Scripted` surface, stopped at teardown."""
    servers = []

    async def start(answer) -> Scripted:
        server = await Scripted(answer).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        await asyncio.wait_for(server.stop(), HANG)


def human_answer(request: dict, entries=(("E5", 0.5), ("D4", 0.3))) -> str:
    distribution = [{"move": move, "p": p} for move, p in entries]
    count = len(request["human"]["policies"])
    return json.dumps({"id": request["id"],
                       "policies": [{"params": {}, "distribution": distribution}] * count})


def plain_answer(request: dict) -> str:
    return json.dumps({"id": request["id"], "moveInfos": [],
                       "rootInfo": {"visits": 1, "winrate": 0.4, "scoreLead": 1.0}})


def nested_error(request: dict, depth: int = 100_000) -> str:
    """An error reply whose value is nested ``depth`` deep: json.loads takes it, str() cannot."""
    return '{"id": "%s", "error": %s%s}' % (request["id"], "[" * depth, "]" * depth)


def is_human(request: dict) -> bool:
    return "human" in request


# F1: a request in flight when the engine is closed or reconnected is not resent.
async def request_during(scripted, connect, action: str):
    async def answer(request):
        if len(server.requests) == 1:
            await asyncio.sleep(0.5)
        return [human_answer(request)]

    server = await scripted(answer)
    engine = await handol(connect, server)
    task = asyncio.create_task(engine.genmove(EMPTY, "B"))
    await wait_for(lambda: len(server.requests) == 1)
    await asyncio.wait_for(getattr(engine, action)(), HANG)
    error = None
    try:
        await asyncio.wait_for(task, HANG)
    except EngineError as exc:
        error = exc
    await asyncio.sleep(0.8)  # past the held reply
    return server, error


#: After close() no connection is left; after connect() only the new one (one more accepted).
AFTER = {"close": 1, "connect": 2}


@pytest.mark.parametrize("action", AFTER)
async def test_a_request_in_flight_at_close_or_connect_is_an_engine_error(scripted, connect,
                                                                           action):
    _, error = await request_during(scripted, connect, action)
    assert isinstance(error, EngineError)


@pytest.mark.parametrize("action", AFTER)
async def test_a_request_in_flight_at_close_or_connect_is_not_resent(scripted, connect, action):
    server, _ = await request_during(scripted, connect, action)
    assert len(server.requests) == 1


@pytest.mark.parametrize("action", AFTER)
async def test_a_request_in_flight_at_close_or_connect_opens_no_connection(scripted, connect,
                                                                           action):
    server, _ = await request_during(scripted, connect, action)
    assert server.accepted == AFTER[action]


@pytest.mark.parametrize("action", AFTER)
async def test_a_request_in_flight_at_close_or_connect_leaves_nothing_open(scripted, connect,
                                                                           action):
    server, _ = await request_during(scripted, connect, action)
    assert server.open_connections == AFTER[action] - 1


# F2a: asking for a move stops a running analysis (without the caller stopping it).
async def test_genmove_stops_a_running_analysis(fake_engine, connect):
    server = await fake_engine("handol", query_delay=FirstOnly(0.5))
    engine = await handol(connect, server)
    reports = Reports()
    await asyncio.wait_for(engine.start_analysis(EMPTY, reports, max_visits=10), HANG)
    await wait_for(lambda: len(human_requests(server)) == 1)
    await genmove(engine, position_from(game_with(9, "E5")), "W")
    await asyncio.sleep(0.3)
    assert len(reports) == 0


# F2b: an engine move carries only the primary tuple.
async def test_an_engine_move_carries_only_the_primary_tuple(handol_server, connect):
    engine = await handol(connect, handol_server, policy={"min_p": 0.05},
                          compare={"distance_slope": 1})
    await genmove(engine, EMPTY, "B")
    assert human_requests(handol_server)[0]["human"]["policies"] == [{"min_p": 0.05}]


# F2c: with two tuples out and one policy back, the note names the short answer.
async def test_fewer_policies_than_two_tuples_leaves_a_note_naming_it(fake_engine, connect):
    server = await fake_engine("handol", short_policies=True)
    log = Log()
    engine = await handol(connect, server, log=log, compare={"distance_slope": 1},
                          eval_visits=0)
    await asyncio.wait_for(engine.start_analysis(EMPTY, Reports(), max_visits=10), HANG)
    await wait_for(lambda: log.notes)
    assert any("1 policies for 2 tuples" in note for note in log.notes)


# F2d: a non-finite number is never sent.
NAN_KOMI = Position(size=9, komi=float("nan"), rules="japanese", initial_stones=[], moves=[])


async def test_genmove_refuses_a_non_finite_komi_and_sends_nothing(handol_server, connect):
    engine = await handol(connect, handol_server)
    with pytest.raises(EngineError):
        await genmove(engine, NAN_KOMI, "B")
    await asyncio.sleep(0.2)
    assert handol_server.requests == []


async def test_start_analysis_refuses_a_non_finite_komi_and_sends_nothing(handol_server,
                                                                         connect):
    engine = await handol(connect, handol_server)
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.start_analysis(NAN_KOMI, Reports(), max_visits=10), HANG)
    await asyncio.sleep(0.2)
    assert handol_server.requests == []


# F5: a move style that is not a colour map is stored and refused at the move.
@pytest.mark.parametrize("style", ["katago", None, ["human"]])
async def test_a_move_style_that_is_not_a_colour_map_is_refused_and_sends_nothing(
        handol_server, connect, style):
    engine = await handol(connect, handol_server)
    engine.configure(move_style=style)
    with pytest.raises(EngineError):
        await genmove(engine, EMPTY, "B")
    await asyncio.sleep(0.2)
    assert handol_server.requests == []


# M1 + F3: an error value that cannot be rendered, or any other failure while reading an answer,
# is an engine error: a move fails with it, and analysis notes it and goes on.
async def test_a_deeply_nested_error_reply_is_an_engine_error_for_genmove(scripted, connect):
    async def answer(request):
        return [nested_error(request)]

    server = await scripted(answer)
    engine = await handol(connect, server)
    with pytest.raises(EngineError):
        await genmove(engine, EMPTY, "B")


async def first_fails_then_next(server, connect, log):
    """Analyse EMPTY; while its answer is held, ask for the next position (White to move)."""
    engine = await handol(connect, server, log=log, eval_visits=0)
    reports = Reports()
    await asyncio.wait_for(engine.start_analysis(EMPTY, reports, max_visits=10), HANG)
    await wait_for(lambda: len(server.requests) == 1)
    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9, "E5")), reports,
                                                 max_visits=10), HANG)
    await reports.at_least(1)
    return reports


async def test_a_deeply_nested_error_reply_leaves_a_note_and_the_next_position_arrives(
        scripted, connect):
    async def answer(request):
        if len(server.requests) == 1:
            await asyncio.sleep(0.3)
            return [nested_error(request)]
        return [human_answer(request)]

    server = await scripted(answer)
    log = Log()
    reports = await first_fails_then_next(server, connect, log)
    assert [r.current_player for r in reports.items] == ["W"]
    assert any("analysis failed" in note for note in log.notes)


async def test_an_unexpected_failure_reading_an_answer_leaves_a_note_and_the_queue_goes_on(
        scripted, connect, monkeypatch):
    import gowui.engine.handol as handol_module

    real = handol_module.parse_answer
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("unexpected")
        return real(*args, **kwargs)

    monkeypatch.setattr(handol_module, "parse_answer", flaky)

    async def answer(request):
        if len(server.requests) == 1:
            await asyncio.sleep(0.3)
        return [human_answer(request)]

    server = await scripted(answer)
    log = Log()
    reports = await first_fails_then_next(server, connect, log)
    assert [r.current_player for r in reports.items] == ["W"]
    assert any("analysis failed" in note and "RuntimeError" in note for note in log.notes)


# M2: p above 1 becomes 1, so huge finite ps never break the weighted draw.
async def test_huge_finite_ps_never_break_the_draw(scripted, connect):
    async def answer(request):
        return [human_answer(request, (("E5", 1e308), ("D4", 1e308)))]

    server = await scripted(answer)
    engine = await handol(connect, server)
    try:
        move = await genmove(engine, EMPTY, "B")
    except EngineError:
        return
    assert key(move) in {"E5", "D4"}


async def test_a_p_above_one_is_one_in_the_policy(scripted, connect):
    async def answer(request):
        return [human_answer(request, (("E5", 5.0), ("D4", 0.25)))]

    server = await scripted(answer)
    engine = await handol(connect, server, eval_visits=0)
    reports = await analyse(engine, EMPTY)
    policy = reports[0].policy
    assert (policy[point_index("E5", 9)], policy[point_index("D4", 9)]) == (1.0, 0.25)


# L1: a profile name is 1-64 characters of letters, digits, "_", "." and "-".
BAD_PROFILES = ["x" * 65, "rank 5k", "a/b", "prö"]


@pytest.mark.parametrize("profile", BAD_PROFILES)
async def test_start_analysis_refuses_a_bad_profile_and_sends_nothing(handol_server, connect,
                                                                      profile):
    engine = await handol(connect, handol_server, profile=profile)
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.start_analysis(EMPTY, Reports(), max_visits=10), HANG)
    await asyncio.sleep(0.2)
    assert handol_server.requests == []


@pytest.mark.parametrize("profile", BAD_PROFILES)
async def test_genmove_refuses_a_bad_profile_and_sends_nothing(handol_server, connect, profile):
    engine = await handol(connect, handol_server, profile=profile)
    with pytest.raises(EngineError):
        await genmove(engine, EMPTY, "B")
    await asyncio.sleep(0.2)
    assert handol_server.requests == []


# L2: a winrate request whose human request failed is abandoned, not awaited.
async def test_a_failed_human_request_does_not_wait_for_its_winrate(scripted, connect):
    held = asyncio.Event()

    async def answer(request):
        first = sum(1 for r in server.requests if is_human(r) == is_human(request)) == 1
        if first and is_human(request):
            # Refuse only once the winrate request is out, so that it is in flight.
            await wait_for(lambda: any(not is_human(r) for r in server.requests))
            return [json.dumps({"id": request["id"], "error": "refused"})]
        if first:
            await held.wait()  # never set: the first winrate answer never comes
        return [human_answer(request) if is_human(request) else plain_answer(request)]

    server = await scripted(answer)
    engine = await handol(connect, server)
    reports = Reports()
    await asyncio.wait_for(engine.start_analysis(EMPTY, reports, max_visits=10), HANG)
    await wait_for(lambda: len(server.requests) == 2)
    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9, "E5")), reports,
                                                 max_visits=10), HANG)
    await reports.at_least(1, timeout=1.5)
    assert [r.current_player for r in reports.items] == ["W"]


async def test_the_reopen_after_an_abandoned_winrate_names_the_real_cause(scripted, connect):
    async def answer(request):
        first = sum(1 for r in server.requests if is_human(r) == is_human(request)) == 1
        if first and is_human(request):
            await wait_for(lambda: any(not is_human(r) for r in server.requests))
            return [json.dumps({"id": request["id"], "error": "refused"})]
        if first:
            await asyncio.Event().wait()  # the first winrate answer never comes
        return [human_answer(request) if is_human(request) else plain_answer(request)]

    server = await scripted(answer)
    log = Log()
    engine = await handol(connect, server, log=log)
    reports = Reports()
    await asyncio.wait_for(engine.start_analysis(EMPTY, reports, max_visits=10), HANG)
    await wait_for(lambda: len(server.requests) == 2)
    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9, "E5")), reports,
                                                 max_visits=10), HANG)
    await reports.at_least(1, timeout=1.5)
    reopened = [n for n in log.notes if "reopened" in n]
    assert reopened and all("idle" not in n for n in reopened), reopened


# L3: blank lines are skipped only up to a limit.
async def test_endless_blank_lines_are_an_engine_error(scripted, connect):
    async def answer(request):
        return [" "] * 5000

    server = await scripted(answer)
    engine = await handol(connect, server)
    with pytest.raises(EngineError):
        await genmove(engine, EMPTY, "B")
