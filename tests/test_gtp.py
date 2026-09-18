"""The GTP client (SPEC §2.3) against the fake engine over real TCP, with the transport rules of
§2.1 and the sanitising of §2.2."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

import pytest

from gowui import coords
from gowui.engine import ConnectionClosed, EngineError, create_engine
from helpers import (HANG, Disconnects, Log, Reports, color_of, game_with, gtp_commands,
                     gtp_names, position_from, strip_ids, wait_for)

HOST = "127.0.0.1"
#: A command list without kata-set-rules / kata-set-param, for "when supported" checks.
PLAIN_COMMANDS = ["name", "version", "list_commands", "quit", "boardsize", "clear_board", "komi",
                  "play", "genmove", "undo", "final_score", "kata-analyze"]


class Since:
    """The GTP commands a fake received after this point."""

    def __init__(self, server) -> None:
        self.server = server
        self.mark = len(server.requests)

    def commands(self) -> list[str]:
        return strip_ids(self.server.requests[self.mark:])

    def names(self) -> list[str]:
        return [c.split()[0] for c in self.commands()]

    def without_scoring(self) -> list[str]:
        return [c for c in self.commands() if c != "final_score"]


def kata_analyze_flags(commands: list[str], name: str = "kata-analyze") -> list[tuple[bool, bool]]:
    """(carries ownership, carries rootInfo) for each analyze command sent."""
    return [("ownership" in c.split(), "rootInfo" in c.split())
            for c in commands if c.split()[0] == name]


async def sync(engine, game) -> None:
    """Bring the engine to ``game`` through a public operation that syncs first."""
    await asyncio.wait_for(engine.final_score(position_from(game)), HANG)


async def analyse(engine, game, count: int = 1, **kwargs) -> Reports:
    reports = Reports()
    kwargs.setdefault("interval", 0.1)
    await asyncio.wait_for(engine.start_analysis(position_from(game), reports, **kwargs), HANG)
    await reports.at_least(count)
    await asyncio.wait_for(engine.stop_analysis(), HANG)
    return reports


def legal(game, color_letter: str, vertex: str) -> bool:
    """A stone the side may play (the fake never passes on a board with room)."""
    point = coords.from_gtp(vertex, game.size)
    return point is not None and game.legal_error(color_of(color_letter), point) is None


# -- connecting -------------------------------------------------------------------------
async def test_connect_asks_name_version_and_list_commands(gtp_server, connect):
    await connect(gtp_server)
    assert {"name", "version", "list_commands"} <= set(gtp_names(gtp_server))


async def test_connect_learns_the_engine_name(gtp_server, connect):
    engine = await connect(gtp_server)
    assert engine.name and engine.name == await asyncio.wait_for(engine.raw("name"), HANG)


async def test_an_engine_without_a_command_list_is_refused(fake_engine, connect):
    server = await fake_engine("gtp", list_commands=[])
    with pytest.raises(EngineError):
        await connect(server)


async def test_a_json_reply_is_refused_with_a_hint_at_the_analysis_protocol(analysis_server,
                                                                            connect):
    with pytest.raises(EngineError, match="(?i)analysis"):
        await connect(analysis_server, protocol="gtp")


async def test_connecting_to_a_closed_port_is_an_engine_error_with_the_address():
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        port = sock.getsockname()[1]
    engine = create_engine("gtp", HOST, port)
    try:
        await asyncio.wait_for(engine.connect(), HANG)
    except EngineError as error:
        assert (error.address, HOST in str(error), str(port) in str(error)) == (
            (HOST, port), False, False)
    else:
        pytest.fail("connecting to a closed port did not raise")


# -- board sync ---------------------------------------------------------------------------
async def test_first_sync_sets_up_the_board(gtp_server, connect):
    engine = await connect(gtp_server)
    since = Since(gtp_server)
    await sync(engine, game_with(9, "E5"))
    assert since.without_scoring() == ["boardsize 9", "clear_board", "komi 6.5",
                                       "kata-set-rules japanese", "play B E5"]


async def test_setup_stones_are_played_after_clearing(gtp_server, connect):
    engine = await connect(gtp_server)
    since = Since(gtp_server)
    await sync(engine, game_with(9, handicap=2))
    commands = since.without_scoring()
    assert commands[commands.index("clear_board") + 1:][-2:] == ["play B C3", "play B G7"]


async def test_kata_set_rules_is_only_sent_when_supported(fake_engine, connect):
    server = await fake_engine("gtp", list_commands=PLAIN_COMMANDS)
    engine = await connect(server)
    await sync(engine, game_with(9, "E5"))
    assert "kata-set-rules" not in gtp_names(server)


async def test_one_more_move_plays_only_that_move(gtp_server, connect):
    engine = await connect(gtp_server)
    await sync(engine, game_with(9, "E5"))
    since = Since(gtp_server)
    await sync(engine, game_with(9, "E5", "F5"))
    assert since.without_scoring() == ["play W F5"]


async def test_the_same_position_sends_nothing(gtp_server, connect):
    engine = await connect(gtp_server)
    await sync(engine, game_with(9, "E5", "F5"))
    since = Since(gtp_server)
    await sync(engine, game_with(9, "E5", "F5"))
    assert since.without_scoring() == []


async def test_taking_back_three_moves_undoes_them(gtp_server, connect):
    engine = await connect(gtp_server)
    await sync(engine, game_with(9, "E5", "F5", "D4", "D5"))
    since = Since(gtp_server)
    await sync(engine, game_with(9, "E5"))
    assert since.without_scoring() == ["undo", "undo", "undo"]


async def test_taking_back_four_moves_resets(gtp_server, connect):
    engine = await connect(gtp_server)
    await sync(engine, game_with(9, "E5", "F5", "D4", "D5", "C3"))
    since = Since(gtp_server)
    await sync(engine, game_with(9, "E5"))
    assert ("clear_board" in since.names(), "undo" in since.names()) == (True, False)


async def test_a_branch_undoes_to_the_common_prefix_then_plays(gtp_server, connect):
    engine = await connect(gtp_server)
    await sync(engine, game_with(9, "E5", "F5", "D4"))
    since = Since(gtp_server)
    await sync(engine, game_with(9, "E5", "F5", "C3"))
    assert since.without_scoring() == ["undo", "play B C3"]


@pytest.mark.parametrize("before, after", [
    ({"size": 9}, {"size": 13}),
    ({"komi": 6.5}, {"komi": 7.5}),
    ({"rules": "japanese"}, {"rules": "chinese"}),
    ({"komi": 6.5}, {"komi": 6.5, "handicap": 2}),
])
async def test_a_changed_size_komi_rules_or_setup_resets(gtp_server, connect, before, after):
    engine = await connect(gtp_server)
    before, after = dict(before), dict(after)
    await sync(engine, game_with(before.pop("size", 9), "E5", **before))
    since = Since(gtp_server)
    await sync(engine, game_with(after.pop("size", 9), **after))
    assert "clear_board" in since.names()


async def test_a_changed_rule_set_is_sent_to_the_engine(gtp_server, connect):
    engine = await connect(gtp_server)
    await sync(engine, game_with(9, "E5"))
    since = Since(gtp_server)
    await sync(engine, game_with(9, "E5", rules="chinese"))
    assert "kata-set-rules chinese" in since.commands()


async def test_a_failure_mid_sync_is_an_engine_error(gtp_server, connect):
    engine = await connect(gtp_server)
    broken = game_with(9, "E5")
    position = position_from(broken)
    position.moves.append(["W", "E5"])  # the fake refuses a play on an occupied point
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.final_score(position), HANG)


async def test_a_failure_mid_sync_drops_the_mirror(gtp_server, connect):
    engine = await connect(gtp_server)
    position = position_from(game_with(9, "E5"))
    position.moves.append(["W", "E5"])
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.final_score(position), HANG)
    since = Since(gtp_server)
    await sync(engine, game_with(9, "E5", "D5"))
    assert "clear_board" in since.names()


# -- moves ------------------------------------------------------------------------------
async def test_genmove_returns_a_legal_move(gtp_server, connect):
    engine = await connect(gtp_server)
    game = game_with(9, "E5")
    vertex = await asyncio.wait_for(engine.genmove(position_from(game), "W"), HANG)
    assert legal(game, "W", vertex)


async def test_the_engines_move_is_on_its_board_afterwards(gtp_server, connect):
    engine = await connect(gtp_server)
    game = game_with(9, "E5")
    vertex = await asyncio.wait_for(engine.genmove(position_from(game), "W"), HANG)
    since = Since(gtp_server)
    await sync(engine, game_with(9, "E5", vertex))
    assert since.without_scoring() == []


@pytest.mark.parametrize("reply", ["Z99", "J10", "", "resign-ish", "0"])
async def test_a_genmove_answer_that_is_not_a_move_is_an_engine_error(fake_engine, connect,
                                                                       reply):
    engine = await connect(await fake_engine("gtp", replies={"genmove": reply}))
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.genmove(position_from(game_with(9)), "B"), HANG)


@pytest.mark.parametrize("reply", ["pass", "resign"])
async def test_pass_and_resign_are_move_answers(fake_engine, connect, reply):
    engine = await connect(await fake_engine("gtp", replies={"genmove": reply}))
    move = await asyncio.wait_for(engine.genmove(position_from(game_with(9)), "B"), HANG)
    assert move.lower() == reply


async def test_genmove_analyze_streams_the_search(gtp_server, connect):
    engine = await connect(gtp_server)
    reports = Reports()
    await asyncio.wait_for(engine.genmove_analyze(position_from(game_with(9)), "B", reports,
                                                  interval=0.1), HANG)
    assert len(reports) >= 1


async def test_genmove_analyze_plays_a_legal_move(gtp_server, connect):
    engine = await connect(gtp_server)
    game = game_with(9, "E5")
    vertex = await asyncio.wait_for(
        engine.genmove_analyze(position_from(game), "W", Reports(), interval=0.1), HANG)
    assert legal(game, "W", vertex)


async def test_genmove_analyze_uses_kata_genmove_analyze(gtp_server, connect):
    engine = await connect(gtp_server)
    await asyncio.wait_for(
        engine.genmove_analyze(position_from(game_with(9)), "B", Reports(), interval=0.1), HANG)
    assert "kata-genmove_analyze" in gtp_names(gtp_server)


async def test_genmove_analyze_falls_back_to_plain_genmove(fake_engine, connect):
    server = await fake_engine("gtp", kata=False)
    engine = await connect(server)
    game = game_with(9)
    vertex = await asyncio.wait_for(
        engine.genmove_analyze(position_from(game), "B", Reports(), interval=0.1), HANG)
    assert (legal(game, "B", vertex), "genmove" in gtp_names(server),
            "kata-genmove_analyze" in gtp_names(server)) == (True, True, False)


async def test_a_raising_callback_does_not_lose_the_engine_move(gtp_server, connect):
    engine = await connect(gtp_server)
    game = game_with(9)

    def explode(analysis):
        raise RuntimeError("browser went away")

    vertex = await asyncio.wait_for(
        engine.genmove_analyze(position_from(game), "B", explode, interval=0.1), HANG)
    assert legal(game, "B", vertex)


async def test_final_score_returns_the_engines_answer(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", replies={"final_score": "W+3.5"}))
    assert await asyncio.wait_for(engine.final_score(position_from(game_with(9))), HANG) == "W+3.5"


# -- analysis ------------------------------------------------------------------------------
async def test_analysis_uses_kata_analyze(gtp_server, connect):
    engine = await connect(gtp_server)
    await analyse(engine, game_with(9))
    assert "kata-analyze" in gtp_names(gtp_server)


async def test_analysis_reports_arrive(gtp_server, connect):
    engine = await connect(gtp_server)
    reports = await analyse(engine, game_with(9), count=2)
    assert len(reports) >= 2


async def test_analysis_asks_for_root_info(gtp_server, connect):
    engine = await connect(gtp_server)
    await analyse(engine, game_with(9))
    assert kata_analyze_flags(gtp_commands(gtp_server)) == [(False, True)]


async def test_analysis_reports_candidates_with_a_pv(gtp_server, connect):
    engine = await connect(gtp_server)
    report = (await analyse(engine, game_with(9)))[0]
    assert report.move_infos and report.move_infos[0].pv


async def test_an_async_callback_receives_reports(gtp_server, connect):
    engine = await connect(gtp_server)
    seen = []

    async def sink(analysis):
        await asyncio.sleep(0)
        seen.append(analysis)

    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), sink,
                                                 interval=0.1), HANG)
    await asyncio.wait_for(_until(lambda: seen), HANG)
    await asyncio.wait_for(engine.stop_analysis(), HANG)
    assert seen


async def _until(predicate):
    while not predicate():
        await asyncio.sleep(0.02)


async def test_stop_is_bounded_and_the_engine_answers_afterwards(gtp_server, connect):
    engine = await connect(gtp_server)
    await analyse(engine, game_with(9))
    assert await asyncio.wait_for(engine.raw("name"), HANG) == engine.name


async def test_a_command_during_analysis_stops_it_first(gtp_server, connect):
    engine = await connect(gtp_server)
    reports = Reports()
    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), reports,
                                                 interval=0.1), HANG)
    await reports.at_least(1)
    assert await asyncio.wait_for(engine.raw("name"), HANG) == engine.name


async def test_max_visits_is_applied_with_kata_set_param(gtp_server, connect):
    engine = await connect(gtp_server)
    await analyse(engine, game_with(9), max_visits=50)
    assert "kata-set-param maxVisits 50" in gtp_commands(gtp_server)


async def test_max_visits_is_skipped_without_kata_set_param(fake_engine, connect):
    server = await fake_engine("gtp", list_commands=PLAIN_COMMANDS)
    engine = await connect(server)
    await analyse(engine, game_with(9), max_visits=50)
    assert "kata-set-param" not in gtp_names(server)


async def test_a_raising_callback_does_not_kill_the_reader(gtp_server, connect):
    engine = await connect(gtp_server)
    calls = []

    def explode(analysis):
        calls.append(analysis)
        raise RuntimeError("browser went away")

    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), explode,
                                                 interval=0.1), HANG)
    await asyncio.wait_for(_until(lambda: len(calls) >= 3), HANG)
    await asyncio.wait_for(engine.stop_analysis(), HANG)
    assert len(calls) >= 3


# -- perspective -------------------------------------------------------------------------------
OWNERSHIP_81 = "ownership 0.5" + " 0" * 80
KATA_LINE = ("info move E5 visits 100 winrate 0.7 scoreLead 3 scoreMean 2 prior 0.2 order 0 "
             "pv E5 D4 rootInfo visits 100 winrate 0.7 scoreLead 3 scoreMean 2 " + OWNERSHIP_81)


async def white_to_move_report(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", emit_line=KATA_LINE))
    return (await analyse(engine, game_with(9, "C3"), include_ownership=True))[0]


async def black_to_move_report(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", emit_line=KATA_LINE))
    return (await analyse(engine, game_with(9), include_ownership=True))[0]


async def test_white_to_move_winrate_is_flipped_to_black(fake_engine, connect):
    report = await white_to_move_report(fake_engine, connect)
    assert report.move_infos[0].winrate == pytest.approx(0.3)


async def test_white_to_move_score_lead_is_flipped_to_black(fake_engine, connect):
    report = await white_to_move_report(fake_engine, connect)
    assert report.move_infos[0].score_lead == pytest.approx(-3.0)


async def test_white_to_move_score_mean_is_flipped_to_black(fake_engine, connect):
    report = await white_to_move_report(fake_engine, connect)
    assert report.move_infos[0].score_mean == pytest.approx(-2.0)


async def test_white_to_move_root_is_flipped_to_black(fake_engine, connect):
    report = await white_to_move_report(fake_engine, connect)
    assert (report.root.winrate, report.root.score_lead) == (pytest.approx(0.3),
                                                             pytest.approx(-3.0))


async def test_white_to_move_ownership_is_flipped_to_black(fake_engine, connect):
    report = await white_to_move_report(fake_engine, connect)
    assert report.ownership[0] == pytest.approx(-0.5)


async def test_white_to_move_report_names_the_current_player(fake_engine, connect):
    report = await white_to_move_report(fake_engine, connect)
    assert report.current_player == "W"


async def test_black_to_move_numbers_are_unchanged(fake_engine, connect):
    report = await black_to_move_report(fake_engine, connect)
    assert (report.move_infos[0].winrate, report.move_infos[0].score_lead,
            report.ownership[0]) == (pytest.approx(0.7), pytest.approx(3.0), pytest.approx(0.5))


async def test_prior_is_not_flipped(fake_engine, connect):
    report = await white_to_move_report(fake_engine, connect)
    assert report.move_infos[0].prior == pytest.approx(0.2)


# -- option fallback -------------------------------------------------------------------------
async def test_without_ownership_asked_the_ownership_key_is_never_sent(fake_engine, connect):
    server = await fake_engine("gtp", ownership=False)
    engine = await connect(server)
    await analyse(engine, game_with(9), include_ownership=False)
    assert kata_analyze_flags(gtp_commands(server)) == [(False, True)]


async def test_rejected_ownership_falls_back_in_spec_order(fake_engine, connect):
    server = await fake_engine("gtp", ownership=False)
    engine = await connect(server)
    await analyse(engine, game_with(9), include_ownership=True)
    assert kata_analyze_flags(gtp_commands(server)) == [(True, True), (True, False), (False, True)]


async def test_rejected_ownership_arrives_as_an_empty_array(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", ownership=False))
    reports = await analyse(engine, game_with(9), include_ownership=True)
    assert reports[0].ownership == []


async def test_rejected_ownership_keeps_root_info(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", ownership=False))
    reports = await analyse(engine, game_with(9), include_ownership=True)
    assert reports[0].root.visits and reports[0].root.visits > 0


async def test_missing_ownership_is_remembered(fake_engine, connect):
    server = await fake_engine("gtp", ownership=False)
    engine = await connect(server)
    await analyse(engine, game_with(9), include_ownership=True)
    since = Since(server)
    await analyse(engine, game_with(9, "E5"), include_ownership=True)
    assert kata_analyze_flags(since.commands()) == [(False, True)]


async def test_each_fallback_step_writes_one_note(fake_engine, connect):
    log = Log()
    engine = await connect(await fake_engine("gtp", ownership=False), log=log)
    before = len(log.notes)
    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), Reports(),
                                                 interval=0.1, include_ownership=True), HANG)
    notes = len(log.notes) - before
    await asyncio.wait_for(engine.stop_analysis(), HANG)
    assert notes == 2


async def test_rejected_root_info_falls_back_without_it(fake_engine, connect):
    server = await fake_engine("gtp", root_info=False)
    engine = await connect(server)
    await analyse(engine, game_with(9), include_ownership=True)
    assert kata_analyze_flags(gtp_commands(server)) == [(True, True), (True, False)]


async def test_rejected_root_info_keeps_ownership(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", root_info=False))
    reports = await analyse(engine, game_with(9), include_ownership=True)
    assert len(reports[0].ownership) == 81


async def test_missing_root_info_is_remembered(fake_engine, connect):
    server = await fake_engine("gtp", root_info=False)
    engine = await connect(server)
    await analyse(engine, game_with(9), include_ownership=True)
    since = Since(server)
    await analyse(engine, game_with(9, "E5"), include_ownership=True)
    assert kata_analyze_flags(since.commands()) == [(True, False)]


async def test_both_rejected_falls_back_to_neither(fake_engine, connect):
    server = await fake_engine("gtp", root_info=False, ownership=False)
    engine = await connect(server)
    await analyse(engine, game_with(9), include_ownership=True)
    assert kata_analyze_flags(gtp_commands(server)) == [
        (True, True), (True, False), (False, True), (False, False)]


async def test_every_attempt_rejected_is_an_engine_error(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", reject=["kata-analyze"]))
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), Reports(),
                                                     interval=0.1, include_ownership=True), HANG)


async def test_every_attempt_rejected_tries_all_four(fake_engine, connect):
    server = await fake_engine("gtp", reject=["kata-analyze"])
    engine = await connect(server)
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), Reports(),
                                                     interval=0.1, include_ownership=True), HANG)
    assert len(kata_analyze_flags(gtp_commands(server))) == 4


async def test_genmove_analyze_falls_back_on_rejected_ownership(fake_engine, connect):
    server = await fake_engine("gtp", ownership=False)
    engine = await connect(server)
    game = game_with(9)
    vertex = await asyncio.wait_for(
        engine.genmove_analyze(position_from(game), "B", Reports(), interval=0.1,
                               include_ownership=True), HANG)
    assert (legal(game, "B", vertex),
            kata_analyze_flags(gtp_commands(server), "kata-genmove_analyze")[-1]) == (
        True, (False, True))


# -- lz-analyze --------------------------------------------------------------------------------
LZ_LINE = "info move E5 visits 100 winrate 6000 prior 1000 lcb 5800 order 0 pv E5 D4"


async def test_without_kata_analyze_lz_analyze_is_used(fake_engine, connect):
    server = await fake_engine("gtp", kata=False)
    engine = await connect(server)
    await analyse(engine, game_with(9))
    assert ("lz-analyze" in gtp_names(server), "kata-analyze" in gtp_names(server)) == (True,
                                                                                      False)


async def test_lz_analyze_reports_arrive(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", kata=False))
    reports = await analyse(engine, game_with(9), count=2)
    assert reports[0].move_infos


async def test_lz_analyze_is_sent_without_kata_options(fake_engine, connect):
    server = await fake_engine("gtp", kata=False)
    engine = await connect(server)
    await analyse(engine, game_with(9), include_ownership=True)
    assert kata_analyze_flags(gtp_commands(server), "lz-analyze") == [(False, False)]


async def test_lz_analyze_has_no_ownership(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", kata=False))
    reports = await analyse(engine, game_with(9), include_ownership=True)
    assert reports[0].ownership == []


async def test_lz_winrate_is_scaled_to_one_for_black_to_move(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", kata=False, emit_line=LZ_LINE))
    reports = await analyse(engine, game_with(9))
    assert reports[0].move_infos[0].winrate == pytest.approx(0.6)


async def test_lz_winrate_is_scaled_and_flipped_for_white_to_move(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", kata=False, emit_line=LZ_LINE))
    reports = await analyse(engine, game_with(9, "C3"))
    assert reports[0].move_infos[0].winrate == pytest.approx(0.4)


async def test_lz_winrates_from_the_fake_are_within_zero_and_one(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", kata=False))
    reports = await analyse(engine, game_with(9), count=2)
    assert all(0.0 <= m.winrate <= 1.0 for r in reports for m in r.move_infos)


# -- sanitising (§2.2) --------------------------------------------------------------------------
async def nonfinite_report(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", nonfinite=True))
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


OFF_BOARD_LINE = ("info move J10 visits 10 winrate 0.5 order 0 pv J10 E5 "
                  "info move E5 visits 5 winrate 0.5 order 1 pv E5 J10 pass")


async def test_an_off_board_candidate_is_dropped(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", emit_line=OFF_BOARD_LINE))
    report = (await analyse(engine, game_with(9)))[0]
    assert [m.move for m in report.move_infos] == ["E5"]


async def test_an_off_board_pv_vertex_is_dropped(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", emit_line=OFF_BOARD_LINE))
    report = (await analyse(engine, game_with(9)))[0]
    assert report.move_infos[0].pv == ["E5", "pass"]


MALFORMED_LINE = "info move E5 visits 12abc winrate zz order x pv E5"


async def test_a_malformed_report_does_not_end_the_stream(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", emit_line=MALFORMED_LINE))
    reports = await analyse(engine, game_with(9), count=3)
    assert any(r.move_infos and isinstance(r.move_infos[0].visits, int)
               and r.move_infos[0].visits > 0 for r in reports)


async def test_every_report_after_a_malformed_one_is_valid_json(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", emit_line=MALFORMED_LINE))
    reports = await analyse(engine, game_with(9), count=3)
    for report in reports:
        json.dumps(report.to_dict(), allow_nan=False)


async def test_an_unreadable_report_is_skipped_with_a_note(fake_engine, connect):
    log = Log()
    engine = await connect(await fake_engine("gtp", emit_line="info move"), log=log)
    reports = await analyse(engine, game_with(9), count=2)
    assert (len(reports) >= 2, any(n.startswith("#") for n in log.notes)) == (True, True)


# -- one line out ---------------------------------------------------------------------------
@pytest.mark.parametrize("command", ["name\nquit", "name\rquit", "name\x00quit"])
async def test_raw_refuses_more_than_one_line(gtp_server, connect, command):
    engine = await connect(gtp_server)
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.raw(command), HANG)


async def test_a_refused_raw_command_sends_nothing(gtp_server, connect):
    engine = await connect(gtp_server)
    since = Since(gtp_server)
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.raw("name\nquit"), HANG)
    await asyncio.sleep(0.1)
    assert since.commands() == []


async def test_the_connection_is_usable_after_a_refused_raw_command(gtp_server, connect):
    engine = await connect(gtp_server)
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.raw("name\nquit"), HANG)
    assert await asyncio.wait_for(engine.raw("name"), HANG) == engine.name


# -- raw commands --------------------------------------------------------------------------------
async def test_raw_passes_the_command_verbatim(gtp_server, connect):
    engine = await connect(gtp_server)
    since = Since(gtp_server)
    await asyncio.wait_for(engine.raw("known_command   genmove"), HANG)
    assert since.commands() == ["known_command   genmove"]


async def test_raw_returns_the_reply(gtp_server, connect):
    engine = await connect(gtp_server)
    assert await asyncio.wait_for(engine.raw("name"), HANG) == engine.name


async def test_raw_drops_the_mirror(gtp_server, connect):
    engine = await connect(gtp_server)
    await sync(engine, game_with(9, "E5"))
    await asyncio.wait_for(engine.raw("name"), HANG)
    since = Since(gtp_server)
    await sync(engine, game_with(9, "E5"))
    assert "clear_board" in since.names()


# -- unusable connection ---------------------------------------------------------------------------
async def test_a_reply_timeout_is_an_engine_error(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", delay={"final_score": 3.0}))
    engine.command_timeout = 0.3
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.final_score(position_from(game_with(9))), HANG)


async def test_a_reply_timeout_leaves_the_connection_unusable(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", delay={"final_score": 3.0}))
    engine.command_timeout = 0.3
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.final_score(position_from(game_with(9))), HANG)
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.raw("name"), 1.0)


async def test_a_reply_with_the_wrong_id_is_an_engine_error(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", wrong_id_on="final_score"))
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.final_score(position_from(game_with(9))), HANG)


async def test_a_reply_with_the_wrong_id_leaves_the_connection_unusable(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", wrong_id_on="final_score"))
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.final_score(position_from(game_with(9))), HANG)
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.raw("name"), 1.0)


async def test_a_cancelled_command_leaves_the_connection_unusable(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", delay={"final_score": 3.0}))
    task = asyncio.create_task(engine.final_score(position_from(game_with(9))))
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.raw("name"), 1.0)


async def test_a_stop_past_its_bound_returns(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", ignore_interrupt=True))
    engine.stop_timeout = 0.5
    reports = Reports()
    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), reports,
                                                 interval=0.1), HANG)
    await reports.at_least(1)
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.stop_analysis(), HANG)


async def test_a_stop_past_its_bound_leaves_the_connection_unusable(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", ignore_interrupt=True))
    engine.stop_timeout = 0.5
    reports = Reports()
    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), reports,
                                                 interval=0.1), HANG)
    await reports.at_least(1)
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.stop_analysis(), HANG)
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.raw("name"), 1.0)


# -- bounded input --------------------------------------------------------------------------------
async def test_an_overlong_reply_line_is_an_engine_error(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", overlong_line="final_score"))
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.final_score(position_from(game_with(9))), HANG)


async def test_an_overlong_reply_line_leaves_the_connection_unusable(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", overlong_line="final_score"))
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.final_score(position_from(game_with(9))), HANG)
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.raw("name"), 1.0)


async def test_a_reply_under_four_mib_in_total_is_read(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", big_reply=("final_score", 3)))
    reply = await asyncio.wait_for(engine.raw("final_score"), HANG)
    assert len(reply) >= 3_000_000


async def test_a_reply_over_four_mib_in_total_is_an_engine_error(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", big_reply=("final_score", 5)))
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.raw("final_score"), HANG)


# -- lost connection ------------------------------------------------------------------------------
async def hung_up_on_final_score(fake_engine, connect, log=None):
    server = await fake_engine("gtp", hangup_on="final_score")
    engine = await connect(server, log=log)
    disconnects = Disconnects()
    engine.on_disconnect = disconnects
    try:
        await asyncio.wait_for(engine.final_score(position_from(game_with(9))), HANG)
    except EngineError as error:
        return engine, disconnects, error, server
    pytest.fail("final_score survived a hang-up")


async def test_hangup_mid_command_is_connection_closed(fake_engine, connect):
    _, _, error, _ = await hung_up_on_final_score(fake_engine, connect)
    assert isinstance(error, ConnectionClosed)


async def test_hangup_mid_command_carries_the_address_separately(fake_engine, connect):
    _, _, error, server = await hung_up_on_final_score(fake_engine, connect)
    assert (error.address, str(server.port) in str(error), HOST in str(error)) == (
        (HOST, server.port), False, False)


async def test_hangup_mid_command_is_reported_once(fake_engine, connect):
    engine, disconnects, _, _ = await hung_up_on_final_score(fake_engine, connect)
    await disconnects.fired()
    await asyncio.sleep(0.3)
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.raw("name"), HANG)
    await asyncio.wait_for(engine.close(), HANG)
    assert len(disconnects.errors) == 1


async def test_the_disconnect_callback_gets_an_engine_error(fake_engine, connect):
    _, disconnects, _, _ = await hung_up_on_final_score(fake_engine, connect)
    await disconnects.fired()
    assert isinstance(disconnects.errors[0], EngineError)


async def test_later_calls_fail_with_connection_closed_at_once(fake_engine, connect):
    engine, _, _, _ = await hung_up_on_final_score(fake_engine, connect)
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(engine.raw("name"), 1.0)


async def test_notes_never_contain_the_address(fake_engine, connect):
    log = Log()
    _, _, _, server = await hung_up_on_final_score(fake_engine, connect, log=log)
    assert not [n for n in log.notes if str(server.port) in n or HOST in n]


async def test_hangup_mid_genmove_is_connection_closed(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", hangup_on="genmove"))
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(engine.genmove(position_from(game_with(9)), "B"), HANG)


async def test_hangup_mid_genmove_analyze_is_connection_closed(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", hangup_after_reports=1))
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(engine.genmove_analyze(position_from(game_with(9)), "B",
                                                      Reports(), interval=0.1), HANG)


async def analysing_until_hangup(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", hangup_after_reports=2))
    disconnects = Disconnects()
    engine.on_disconnect = disconnects
    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), Reports(),
                                                 interval=0.1), HANG)
    await disconnects.fired()
    return engine, disconnects


async def test_hangup_mid_stream_is_reported(fake_engine, connect):
    _, disconnects = await analysing_until_hangup(fake_engine, connect)
    assert len(disconnects.errors) == 1


async def test_hangup_mid_stream_is_reported_only_once(fake_engine, connect):
    engine, disconnects = await analysing_until_hangup(fake_engine, connect)
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.stop_analysis(), HANG)
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.raw("name"), HANG)
    await asyncio.wait_for(engine.close(), HANG)
    await asyncio.sleep(0.2)
    assert len(disconnects.errors) == 1


async def test_after_a_hangup_mid_stream_calls_fail_at_once(fake_engine, connect):
    engine, _ = await analysing_until_hangup(fake_engine, connect)
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(engine.raw("name"), 1.0)


async def test_a_hangup_while_idle_is_reported(fake_engine, connect):
    server = await fake_engine("gtp")
    engine = await connect(server)
    disconnects = Disconnects()
    engine.on_disconnect = disconnects
    await asyncio.wait_for(server.stop(), HANG)
    assert await disconnects.fired()


async def test_the_disconnect_callback_may_close_the_engine(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", hangup_after_reports=1))
    closed = asyncio.Event()

    async def on_disconnect(error):
        await engine.close()
        closed.set()

    engine.on_disconnect = on_disconnect
    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), Reports(),
                                                 interval=0.1), HANG)
    await asyncio.wait_for(closed.wait(), HANG)


async def test_close_after_a_hangup_does_not_hang(fake_engine, connect):
    engine, _ = await analysing_until_hangup(fake_engine, connect)
    await asyncio.wait_for(engine.close(), HANG)


# -- lz-analyze scaling and the derived root (§2.3) ----------------------------------------------
async def test_lz_prior_is_scaled_to_one(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", kata=False, emit_line=LZ_LINE))
    reports = await analyse(engine, game_with(9))
    assert reports[0].move_infos[0].prior == pytest.approx(0.1)


async def test_lz_lcb_is_scaled_to_one_for_black_to_move(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", kata=False, emit_line=LZ_LINE))
    reports = await analyse(engine, game_with(9))
    assert reports[0].move_infos[0].lcb == pytest.approx(0.58)


async def test_lz_lcb_is_scaled_and_flipped_for_white_to_move(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", kata=False, emit_line=LZ_LINE))
    reports = await analyse(engine, game_with(9, "C3"))
    assert reports[0].move_infos[0].lcb == pytest.approx(0.42)


LZ_TWO = ("info move E5 visits 100 winrate 6000 prior 1000 lcb 5800 order 0 pv E5 D4 "
          "info move D4 visits 50 winrate 5500 prior 900 lcb 5000 order 1 pv D4")
KATA_TWO = ("info move E5 visits 100 winrate 0.7 scoreLead 3 scoreMean 2 order 0 pv E5 "
            "info move D4 visits 50 winrate 0.6 scoreLead 1 scoreMean 1 order 1 pv D4")


def root_of(report) -> tuple:
    root = report.root
    return (root.visits, root.winrate, root.score_lead, root.score_mean)


async def test_lz_root_is_derived_from_the_candidates(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", kata=False, emit_line=LZ_TWO))
    reports = await analyse(engine, game_with(9))
    assert root_of(reports[0]) == (150, pytest.approx(0.6), None, None)


async def test_lz_derived_root_is_in_blacks_view_for_white_to_move(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", kata=False, emit_line=LZ_TWO))
    reports = await analyse(engine, game_with(9, "C3"))
    assert root_of(reports[0]) == (150, pytest.approx(0.4), None, None)


async def test_without_root_info_the_root_is_derived_from_the_candidates(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", root_info=False, emit_line=KATA_TWO))
    reports = await analyse(engine, game_with(9))
    assert root_of(reports[0]) == (150, pytest.approx(0.7), pytest.approx(3.0),
                                   pytest.approx(2.0))


async def test_without_root_info_the_derived_root_is_in_blacks_view(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", root_info=False, emit_line=KATA_TWO))
    reports = await analyse(engine, game_with(9, "C3"))
    assert root_of(reports[0]) == (150, pytest.approx(0.3), pytest.approx(-3.0),
                                   pytest.approx(-2.0))


# -- id-less replies (§2.1) ---------------------------------------------------------------------
async def test_an_engine_that_echoes_no_id_connects(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", no_reply_id=True))
    assert engine.name == "FakeKataGo"


async def test_an_id_less_reply_is_the_answer_to_the_command_in_flight(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", no_reply_id=True))
    await sync(engine, game_with(9, "E5"))
    assert await asyncio.wait_for(engine.raw("final_score"), HANG) == "B+0.5"


async def test_an_id_less_analysis_stream_is_read(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", no_reply_id=True))
    reports = await analyse(engine, game_with(9))
    assert reports[0].move_infos


# -- reply ids ----------------------------------------------------------------------------------
@pytest.mark.parametrize("reply_id", ["9" * 5000, "1" + "0" * 18, "0" * 19 + "1"],
                         ids=["5000-digits", "19-digits", "19-digits-leading-zeros"])
async def test_an_overlong_reply_id_is_an_engine_error(fake_engine, connect, reply_id):
    engine = await connect(await fake_engine("gtp", reply_id={"final_score": reply_id}))
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.raw("final_score"), HANG)


async def test_an_overlong_reply_id_leaves_the_connection_unusable(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", reply_id={"final_score": "9" * 5000}))
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.raw("final_score"), HANG)
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.raw("name"), 1.0)


async def test_an_overlong_reply_id_during_connect_is_an_engine_error(fake_engine, connect):
    server = await fake_engine("gtp", reply_id={"name": "9" * 5000})
    with pytest.raises(EngineError):
        await connect(server)


# -- one deadline per reply, clipped engine text --------------------------------------------------
async def test_stray_output_does_not_extend_the_reply_deadline(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", delay={"final_score": 3.0},
                                             stray=(0.1, "stray output")))
    engine.command_timeout = 0.2
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.raw("final_score"), HANG)
    assert loop.time() - start < 0.6


async def test_stray_lines_are_logged_clipped(fake_engine, connect):
    log = Log()
    engine = await connect(await fake_engine("gtp", delay={"final_score": 0.3},
                                             stray=(0.05, "s" * 20000)), log=log)
    await asyncio.wait_for(engine.raw("final_score"), HANG)
    stray = [text for direction, text in log.entries if direction == "recv" and "sss" in text]
    assert stray and max(len(text) for text in stray) <= 4100


async def test_a_rejection_message_is_clipped_in_the_error(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", reject=["final_score"],
                                             reject_message="r" * 5000))
    with pytest.raises(EngineError) as caught:
        await asyncio.wait_for(engine.raw("final_score"), HANG)
    assert 0 < len(str(caught.value)) <= 600


async def test_an_analyze_rejection_is_clipped_in_notes_and_errors(fake_engine, connect):
    log = Log()
    engine = await connect(await fake_engine("gtp", reject=["kata-analyze", "lz-analyze"],
                                             reject_message="r" * 5000), log=log)
    with pytest.raises(EngineError) as caught:
        await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), Reports()),
                               HANG)
    assert (len(str(caught.value)) <= 700, max(len(n) for n in log.notes) <= 700) == (True, True)


# -- reconnecting (§2.1) ----------------------------------------------------------------------------
async def test_a_reconnect_after_close_replays_the_board(fake_engine, connect):
    first, second = await fake_engine("gtp"), await fake_engine("gtp")
    engine = await connect(first)
    await sync(engine, game_with(9, "E5"))
    await asyncio.wait_for(engine.close(), HANG)
    engine.port = second.port
    await asyncio.wait_for(engine.connect(), HANG)
    await sync(engine, game_with(9, "E5", "C3"))
    commands = gtp_commands(second)
    assert ("play B E5" in commands, "play W C3" in commands) == (True, True)


async def test_a_reconnect_while_connected_replays_the_board(fake_engine, connect):
    first, second = await fake_engine("gtp"), await fake_engine("gtp")
    engine = await connect(first)
    await sync(engine, game_with(9, "E5"))
    engine.port = second.port
    await asyncio.wait_for(engine.connect(), HANG)
    await sync(engine, game_with(9, "E5", "C3"))
    commands = gtp_commands(second)
    assert ("play B E5" in commands, "play W C3" in commands) == (True, True)


async def test_a_reconnect_while_connected_closes_the_old_connection(gtp_server, connect):
    engine = await connect(gtp_server)
    await asyncio.wait_for(engine.connect(), HANG)
    assert await wait_for(lambda: gtp_server.open_connections == 1)


async def test_a_reconnect_while_connected_leaves_a_working_client(gtp_server, connect):
    engine = await connect(gtp_server)
    await asyncio.wait_for(engine.connect(), HANG)
    assert await asyncio.wait_for(engine.raw("name"), HANG) == "FakeKataGo"


async def test_a_reconnect_forgets_a_missing_capability(fake_engine, connect):
    first, second = await fake_engine("gtp", root_info=False), await fake_engine("gtp")
    engine = await connect(first)
    await analyse(engine, game_with(9))
    engine.port = second.port
    await asyncio.wait_for(engine.connect(), HANG)
    await analyse(engine, game_with(9))
    assert kata_analyze_flags(gtp_commands(second)) == [(False, True)]


# -- overlapping calls ----------------------------------------------------------------------------
async def test_a_command_queued_behind_an_analysis_start_runs(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", delay={"kata-analyze": 0.3}))
    starting = asyncio.create_task(engine.start_analysis(position_from(game_with(9)), Reports(),
                                                         interval=0.1))
    await asyncio.sleep(0.05)
    try:
        await asyncio.wait_for(engine.raw("showboard"), HANG)
    finally:
        await asyncio.gather(starting, return_exceptions=True)
    assert await asyncio.wait_for(engine.raw("name"), HANG) == "FakeKataGo"


async def test_an_analysis_restart_queued_behind_another_leaves_one_stream(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", delay={"kata-analyze": 0.3}))
    first, second = Reports(), Reports()
    starting = asyncio.create_task(engine.start_analysis(position_from(game_with(9)), first,
                                                         interval=0.1))
    await asyncio.sleep(0.05)
    await asyncio.wait_for(engine.start_analysis(position_from(game_with(9)), second,
                                                 interval=0.1), HANG)
    await asyncio.gather(starting, return_exceptions=True)
    await second.at_least(1)
    await asyncio.wait_for(engine.stop_analysis(), HANG)
    assert await asyncio.wait_for(engine.raw("name"), HANG) == "FakeKataGo"


# -- bounded sends (§2.1) -------------------------------------------------------------------------
#: More than a peer that never reads can absorb in socket buffers.
FLOOD = "echo " + "x" * (16 * 1024 * 1024)


async def wedged(fake_engine, connect):
    """A client whose engine stopped reading after the handshake, with a send stuck behind it."""
    engine = await connect(await fake_engine("gtp", never_read=True))
    engine.send_timeout = 0.5
    stuck = asyncio.create_task(engine.raw(FLOOD))
    await asyncio.sleep(0.1)
    return engine, stuck


async def test_a_send_the_engine_never_accepts_is_an_engine_error(fake_engine, connect):
    engine = await connect(await fake_engine("gtp", never_read=True))
    engine.send_timeout = 0.5
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.raw(FLOOD), HANG)


async def test_a_send_the_engine_never_accepts_leaves_the_connection_unusable(fake_engine,
                                                                              connect):
    engine = await connect(await fake_engine("gtp", never_read=True))
    engine.send_timeout = 0.5
    with contextlib.suppress(EngineError):
        await asyncio.wait_for(engine.raw(FLOOD), HANG)
    with pytest.raises(EngineError):
        await asyncio.wait_for(engine.raw("name"), 1.0)


@pytest.mark.parametrize("operation", ["genmove", "start_analysis", "stop_analysis", "close"])
async def test_nothing_hangs_behind_a_send_the_engine_never_accepts(fake_engine, connect,
                                                                    operation):
    engine, stuck = await wedged(fake_engine, connect)
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


# -- callback-failure notes (§2.1) ------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_a_failing_disconnect_handler_is_noted_by_type_only(fake_engine, connect, kind):
    log = Log()
    server = await fake_engine("gtp")
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
