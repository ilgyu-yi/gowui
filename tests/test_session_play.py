"""Engine play through the session (SPEC §3.2, §4.1): human vs engine, self-play, on-demand
``genmove``, stale moves discarded by the position token and the move epoch, undo/navigate, pass
and resign, and the move limit.

Observed through broadcast frames and the fake engine's request log only.
"""

from __future__ import annotations

import pytest

from helpers import position_queries, wait_for
from session_helpers import gtp_count, move_list, random_sgf, settle, sgf_of

#: How long a slow fake engine thinks about one move (seconds).
SLOW = 1.0


async def slow_gtp(fake_engine, delay: float = SLOW):
    return await fake_engine("gtp", delay={"genmove": delay, "kata-genmove_analyze": delay})


async def engine_plays_white(h, server) -> None:
    await h.connect_to(server)
    await h.send({"type": "players", "whiteIsEngine": True})


# -- human vs engine ---------------------------------------------------------------------------
@pytest.mark.parametrize("protocol", ["gtp", "analysis", "handol"])
async def test_the_engine_answers_a_human_move(h, fake_engine, protocol):
    server = await fake_engine(protocol)
    await engine_plays_white(h, server)
    await h.play("D4")
    state = await h.wait_moves(2)
    assert state["game"]["moves"][1]["color"] == "white"


async def test_the_engine_does_not_move_for_a_human_side(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.play("D4")
    await settle()
    assert gtp_count(gtp_server, "genmove") == 0


async def test_the_engine_plays_on_a_loaded_game_with_a_scored_result(h, gtp_server):
    await engine_plays_white(h, gtp_server)
    start = h.rec.mark()
    await h.send({"type": "load_sgf", "sgf": sgf_of(9, "D4", result="B+3.5")})
    await h.wait_moves(2, start)


async def test_the_engine_does_not_move_in_a_resigned_game(h, gtp_server):
    await engine_plays_white(h, gtp_server)
    await h.send({"type": "load_sgf", "sgf": sgf_of(9, "D4", result="W+R")})
    await settle()
    assert gtp_count(gtp_server, "genmove") == 0


async def test_the_engine_does_not_move_after_two_passes(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.send({"type": "pass"})
    await h.send({"type": "pass"})
    await h.send({"type": "players", "blackIsEngine": True, "whiteIsEngine": True})
    await settle()
    assert gtp_count(gtp_server, "genmove") == 0


# -- self-play -----------------------------------------------------------------------------------
async def test_self_play_keeps_moving_while_both_sides_are_engines(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.send({"type": "players", "blackIsEngine": True, "whiteIsEngine": True})
    await h.wait_moves(4)


async def test_self_play_stops_when_both_sides_are_set_to_human(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.send({"type": "players", "blackIsEngine": True, "whiteIsEngine": True})
    await h.wait_moves(2)
    start = h.rec.mark()
    await h.send({"type": "players", "blackIsEngine": False, "whiteIsEngine": False})
    assert await h.rec.wait_state(lambda f: not f["thinking"], start)
    await settle(0.5)
    count = gtp_count(gtp_server, "genmove")
    await settle(0.8)
    assert gtp_count(gtp_server, "genmove") == count


async def test_self_play_stops_at_the_move_limit(h, fake_engine):
    """§3.2 / §7.6: at 2,000 moves nothing can be played, not even a pass, although the game is
    not over; engine play stops there instead of asking again and again."""
    server = await fake_engine("analysis")
    await h.connect_to(server)
    start = h.rec.mark()
    await h.send({"type": "load_sgf", "sgf": random_sgf(1998)})
    await h.wait_moves(1998, start, timeout=15)
    await h.send({"type": "players", "blackIsEngine": True, "whiteIsEngine": True})
    await h.wait_moves(2000, start, timeout=15)
    await settle(1.0)
    asked = len(position_queries(server))
    await settle(1.0)
    assert (len(position_queries(server)), h.rec.state()["thinking"]) == (asked, False)


# -- genmove on demand ---------------------------------------------------------------------------
async def test_genmove_plays_one_move_for_the_side_to_move(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.send({"type": "genmove"})
    state = await h.wait_moves(1)
    assert state["game"]["moves"][0]["color"] == "black"


async def test_genmove_does_not_start_automatic_play(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.send({"type": "genmove"})
    await h.wait_moves(1)
    await settle()
    assert gtp_count(gtp_server, "genmove") == 1


async def test_explicit_genmove_on_a_human_side_still_plays(h, gtp_server):
    """§3.2: a move asked for with genmove is unaffected by the players setting."""
    await h.connect_to(gtp_server)
    await h.play("D4")
    await h.send({"type": "genmove", "color": "white"})
    state = await h.wait_moves(2)
    assert state["game"]["moves"][1]["color"] == "white"


async def test_genmove_for_the_wrong_colour_is_an_error(h, gtp_server):
    await h.connect_to(gtp_server)
    start = h.rec.mark()
    await h.send({"type": "genmove", "color": "white"})
    assert await h.rec.wait_error(start) is not None


async def test_genmove_for_the_wrong_colour_asks_the_engine_nothing(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.send({"type": "genmove", "color": "white"})
    await settle()
    assert gtp_count(gtp_server, "genmove") == 0


async def test_genmove_without_an_engine_is_an_error(h):
    start = h.rec.mark()
    await h.send({"type": "genmove"})
    assert await h.rec.wait_error(start) is not None


# -- stale engine moves (§3.2 "Stale work is discarded") -----------------------------------------
STALE_TRIGGERS = {
    "play": ({"type": "play", "color": "white", "vertex": "E5"}, ["D4", "E5"]),
    "undo": ({"type": "undo"}, []),
    "navigate": ({"type": "navigate", "index": 0}, ["D4"]),
    "new_game": ({"type": "new_game", "size": 9, "komi": None, "rules": "japanese",
                  "handicap": 0}, []),
    "load_sgf": ({"type": "load_sgf", "sgf": sgf_of(9, "C7", "G3")}, ["C7", "G3"]),
    "resign": ({"type": "resign", "color": "white"}, ["D4"]),
}


@pytest.mark.parametrize("trigger", sorted(STALE_TRIGGERS))
async def test_a_move_searched_for_an_old_position_is_discarded(h, fake_engine, trigger):
    message, expected = STALE_TRIGGERS[trigger]
    server = await slow_gtp(fake_engine)
    await engine_plays_white(h, server)
    await h.play("D4")
    assert await wait_for(lambda: gtp_count(server, "genmove") == 1)
    await h.send(message)
    await settle(SLOW + 0.8)
    assert move_list(await h.fresh_state()) == expected


async def test_undo_and_a_different_move_at_the_same_cursor_discards_the_search(h, fake_engine):
    """Staleness is never judged by the cursor number (§3.2)."""
    server = await slow_gtp(fake_engine)
    await h.connect_to(server)
    await h.play("D4")
    await h.send({"type": "genmove"})
    assert await wait_for(lambda: gtp_count(server, "genmove") == 1)
    await h.send({"type": "undo"})
    await h.play("A1")  # a corner: the fake's genmove never picks it, so only staleness can discard
    await settle(SLOW + 0.8)
    assert move_list(await h.fresh_state()) == ["A1"]


async def test_switching_boards_and_back_discards_the_move(h, fake_engine):
    """B1: A -> B -> A during a search changes the move epoch, so the move is not played."""
    server = await slow_gtp(fake_engine)
    await h.connect_to(server)
    await h.play("D4")
    board_a = h.rec.state()["activeBoard"]
    await h.send({"type": "genmove"})
    assert await wait_for(lambda: gtp_count(server, "genmove") == 1)
    start = h.rec.mark()
    await h.send({"type": "board_duplicate"})
    assert await h.rec.wait_state(lambda f: f["activeBoard"] != board_a, start)
    await h.send({"type": "board_select", "id": board_a})
    await settle(SLOW + 0.8)
    state = await h.fresh_state()
    assert [b["moveCount"] for b in state["boards"]] == [1, 1]


async def test_setting_white_to_human_mid_search_drops_whites_move(h, fake_engine):
    server = await slow_gtp(fake_engine)
    await engine_plays_white(h, server)
    await h.play("D4")
    assert await wait_for(lambda: gtp_count(server, "genmove") == 1)
    await h.send({"type": "players", "whiteIsEngine": False})
    await settle(SLOW + 0.8)
    assert move_list(await h.fresh_state()) == ["D4"]


async def test_setting_white_to_human_mid_search_ends_automatic_play(h, fake_engine):
    server = await slow_gtp(fake_engine)
    await engine_plays_white(h, server)
    await h.play("D4")
    assert await wait_for(lambda: gtp_count(server, "genmove") == 1)
    await h.send({"type": "players", "whiteIsEngine": False})
    await settle(2 * SLOW + 0.5)
    assert gtp_count(server, "genmove") == 1


# -- undo, navigation --------------------------------------------------------------------------
async def test_undo_does_not_rearm_the_engine(h, gtp_server):
    await engine_plays_white(h, gtp_server)
    await h.play("D4")
    await h.wait_moves(2)
    start = h.rec.mark()
    await h.send({"type": "undo"})
    assert await h.rec.wait_state(lambda f: f["game"]["moveCount"] == 1, start)
    await settle()
    assert gtp_count(gtp_server, "genmove") == 1


async def test_navigating_back_to_the_end_resumes_engine_play(h, gtp_server):
    await h.connect_to(gtp_server)
    await h.play("D4")
    await h.send({"type": "navigate", "index": 0})
    await h.send({"type": "players", "whiteIsEngine": True})
    await settle(0.4)
    assert gtp_count(gtp_server, "genmove") == 0
    start = h.rec.mark()
    await h.send({"type": "navigate", "index": 1})
    await h.wait_moves(2, start)


# -- pass and resign -------------------------------------------------------------------------------
async def test_pass_plays_a_pass_for_the_side_to_move(h):
    start = h.rec.mark()
    await h.send({"type": "pass"})
    state = await h.wait_moves(1, start)
    move = state["game"]["moves"][0]
    assert (move["color"], move["vertex"]) == ("black", "pass")


async def test_resign_without_a_colour_resigns_the_side_to_move(h):
    start = h.rec.mark()
    await h.send({"type": "resign"})
    state = await h.rec.wait_state(lambda f: f["game"]["result"] != "", start)
    assert state is not None and (state["game"]["result"], state["game"]["gameOver"]) == \
        ("W+R", True)


async def test_resign_for_white_records_a_black_win(h):
    start = h.rec.mark()
    await h.send({"type": "resign", "color": "white"})
    state = await h.rec.wait_state(lambda f: f["game"]["result"] != "", start)
    assert state and state["game"]["result"] == "B+R"


async def test_resign_is_not_a_vertex_of_play(h):
    start = h.rec.mark()
    await h.send({"type": "play", "color": "black", "vertex": "resign"})
    assert await h.rec.wait_error(start) is not None


async def test_play_resign_changes_nothing(h):
    before = h.session.snapshot()
    await h.send({"type": "play", "color": "black", "vertex": "resign"})
    assert h.session.snapshot() == before


async def test_an_illegal_move_is_an_error(h):
    await h.play("D4")
    start = h.rec.mark()
    await h.send({"type": "play", "color": "white", "vertex": "D4"})
    assert await h.rec.wait_error(start) is not None


async def test_playing_in_the_past_says_where_it_branched(h):
    """§1.4: the browser is told "branched at move N"."""
    await h.play("D4", "E5", "F6")
    await h.send({"type": "navigate", "index": 1})
    state = await h.play("C3")
    assert "branched at move 1" in state["status"].lower()
