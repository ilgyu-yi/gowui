"""The game model: new games and handicap (SPEC §1.2), resign and game over (§1.3), the line of
play, cursor and browser-facing state (§1.4)."""

from __future__ import annotations

import pytest

from gowui import coords, rules
from gowui.board import BLACK, EMPTY, WHITE, IllegalMove
from gowui.game import Game


def play_all(game, vertices):
    """Play vertices alternately, starting with the side to move."""
    for vertex in vertices:
        color = BLACK if game.to_dict()["toPlay"] == "black" else WHITE
        game.play(color, coords.from_gtp(vertex, game.size))


def setup_vertices(game):
    return [s["vertex"] for s in game.to_dict()["setupStones"]]


def move_vertices(game):
    return [m["vertex"] for m in game.to_dict()["moves"]]


# -- new games ------------------------------------------------------------------
@pytest.mark.parametrize("size", [0, 1, 26, 100, -3])
def test_size_outside_2_to_25_is_refused(size):
    with pytest.raises(ValueError):
        Game(size)


@pytest.mark.parametrize("size", [2, 25])
def test_size_limits_are_accepted(size):
    assert Game(size).size == size


def test_unknown_rule_set_is_refused():
    with pytest.raises(ValueError):
        Game(9, rules="ing")


@pytest.mark.parametrize("name,komi", [("japanese", 6.5), ("korean", 6.5), ("chinese", 7.5),
                                       ("aga", 7.5), ("new-zealand", 7.0), ("tromp-taylor", 7.5)])
def test_default_komi_follows_the_rule_set(name, komi):
    assert Game(9, rules=name).komi == komi


def test_default_rules_are_japanese():
    assert Game(9).to_dict()["rules"] == "japanese"


def test_explicit_komi_is_kept():
    assert Game(9, komi=5.5).komi == 5.5


def test_handicap_game_defaults_to_half_point_komi():
    assert Game(19, rules="chinese", handicap=4).komi == 0.5


def test_handicap_game_keeps_an_explicit_komi():
    assert Game(19, komi=7.5, handicap=4).komi == 7.5


def test_handicap_game_keeps_an_explicit_zero_komi():
    assert Game(19, komi=0, handicap=2).komi == 0


def test_handicap_game_starts_with_white():
    assert Game(19, handicap=2).to_dict()["toPlay"] == "white"


def test_even_game_starts_with_black():
    assert Game(19).to_dict()["toPlay"] == "black"


@pytest.mark.parametrize("count", [0, 1])
def test_handicap_zero_or_one_places_no_stones(count):
    assert Game(19, handicap=count).to_dict()["setupStones"] == []


@pytest.mark.parametrize("count", [0, 1])
def test_handicap_zero_or_one_is_an_even_game(count):
    game = Game(19, handicap=count)
    assert (game.handicap, game.komi, game.to_dict()["toPlay"]) == (0, 6.5, "black")


GTP_19 = {
    2: ["D4", "Q16"],
    3: ["D4", "Q16", "D16"],
    4: ["D4", "Q16", "D16", "Q4"],
    5: ["D4", "Q16", "D16", "Q4", "K10"],
    6: ["D4", "Q16", "D16", "Q4", "D10", "Q10"],
    7: ["D4", "Q16", "D16", "Q4", "D10", "Q10", "K10"],
    8: ["D4", "Q16", "D16", "Q4", "D10", "Q10", "K4", "K16"],
    9: ["D4", "Q16", "D16", "Q4", "D10", "Q10", "K4", "K16", "K10"],
}


@pytest.mark.parametrize("count", sorted(GTP_19))
def test_19x19_handicap_stones_follow_gtp_fixed_handicap(count):
    assert setup_vertices(Game(19, handicap=count)) == GTP_19[count]


@pytest.mark.parametrize(
    "size,count,expected",
    [
        (7, 4, ["C3", "E5", "C5", "E3"]),               # 3rd line on 7x7
        (8, 2, ["C3", "F6"]),                           # even size, 3rd line
        (9, 9, ["C3", "G7", "C7", "G3", "C5", "G5", "E3", "E7", "E5"]),
        (12, 4, ["C3", "K10", "C10", "K3"]),            # 3rd line up to 12
        (13, 4, ["D4", "K10", "D10", "K4"]),            # 4th line from 13
        (25, 9, ["D4", "W22", "D22", "W4", "D13", "W13", "N4", "N22", "N13"]),
    ],
)
def test_handicap_stones_on_other_sizes(size, count, expected):
    assert setup_vertices(Game(size, handicap=count)) == expected


def _allowed(size, count):
    if count in (0, 1):
        return True
    if size < 7:
        return False
    if size == 7 or size % 2 == 0:
        return 2 <= count <= 4
    return 2 <= count <= 9


CASES = [(size, count) for size in range(2, 26) for count in range(-1, 11)]


@pytest.mark.parametrize("size,count", [c for c in CASES if not _allowed(*c)])
def test_handicap_the_size_does_not_allow_is_refused(size, count):
    with pytest.raises(ValueError):
        Game(size, handicap=count)


@pytest.mark.parametrize("size,count", [c for c in CASES if _allowed(*c) and c[1] >= 2])
def test_handicap_the_size_allows_places_that_many_black_stones(size, count):
    stones = Game(size, handicap=count).to_dict()["setupStones"]
    assert [s["color"] for s in stones] == ["black"] * count


def test_handicap_is_reported():
    assert Game(19, handicap=5).to_dict()["handicap"] == 5


def test_handicap_stones_are_on_the_board():
    game = Game(19, handicap=2)
    assert game.board.get(*coords.from_gtp("D4", 19)) == BLACK


# -- playing, cursor, navigation -------------------------------------------------
def test_move_advances_the_cursor():
    game = Game(9)
    play_all(game, ["E5", "F5"])
    assert (game.cursor, game.move_count) == (2, 2)


def test_side_to_move_alternates():
    game = Game(9)
    play_all(game, ["E5"])
    assert game.to_dict()["toPlay"] == "white"


def test_occupied_point_is_refused_with_a_reason():
    game = Game(9)
    play_all(game, ["E5"])
    assert game.legal_error(WHITE, coords.from_gtp("E5", 9))


def test_occupied_point_raises():
    game = Game(9)
    play_all(game, ["E5"])
    with pytest.raises(IllegalMove):
        game.play(WHITE, coords.from_gtp("E5", 9))


def test_off_board_point_is_refused_with_a_reason():
    assert Game(9).legal_error(BLACK, (9, 0))


def test_pass_is_always_legal():
    assert Game(9).legal_error(BLACK, None) is None


@pytest.mark.parametrize("target,expected", [(-5, 0), (0, 0), (2, 2), (3, 3), (99, 3)])
def test_navigate_clamps_the_cursor(target, expected):
    game = Game(9)
    play_all(game, ["E5", "F5", "D3"])
    game.navigate(target)
    assert game.cursor == expected


def test_navigate_keeps_the_moves():
    game = Game(9)
    play_all(game, ["E5", "F5", "D3"])
    game.navigate(1)
    assert game.move_count == 3


def test_navigate_shows_the_position_at_the_cursor():
    game = Game(9)
    play_all(game, ["E5", "F5", "D3"])
    game.navigate(1)
    assert game.board.get(*coords.from_gtp("F5", 9)) == EMPTY


def test_playing_in_the_past_discards_later_moves():
    game = Game(9)
    play_all(game, ["E5", "F5", "D3"])
    game.navigate(1)
    game.play(WHITE, coords.from_gtp("C3", 9))
    assert move_vertices(game) == ["E5", "C3"]


def test_last_move_after_a_branch():
    game = Game(9)
    play_all(game, ["E5", "F5", "D3"])
    game.navigate(1)
    game.play(WHITE, coords.from_gtp("C3", 9))
    assert game.to_dict()["lastMove"] == "C3"


def test_undo_removes_the_last_move():
    game = Game(9)
    play_all(game, ["E5", "F5"])
    game.undo()
    assert (move_vertices(game), game.cursor) == (["E5"], 1)


def test_undo_at_the_start_changes_nothing():
    game = Game(9)
    play_all(game, ["E5", "F5"])
    game.navigate(0)
    before = game.to_dict()
    game.undo()
    assert game.to_dict() == before


def test_undo_behind_the_end_removes_the_move_before_the_cursor_and_everything_after():
    game = Game(9)
    play_all(game, ["E5", "F5", "D3", "C7"])
    game.navigate(2)
    game.undo()
    assert (move_vertices(game), game.cursor) == (["E5"], 1)


def test_undo_behind_the_end_leaves_the_position_that_was_played():
    game = Game(9)
    play_all(game, ["E5", "F5", "D3", "C7"])
    game.navigate(2)
    game.undo()
    replay = Game(9)
    play_all(replay, ["E5"])
    assert game.to_dict()["stones"] == replay.to_dict()["stones"]


def test_pass_is_recorded_as_pass():
    game = Game(9)
    play_all(game, ["pass"])
    assert move_vertices(game) == ["pass"]


# -- game over, resign, result -----------------------------------------------
def test_one_pass_does_not_end_the_game():
    game = Game(9)
    play_all(game, ["pass"])
    assert not game.is_game_over()


def test_two_passes_end_the_game():
    game = Game(9)
    play_all(game, ["pass", "pass"])
    assert game.is_game_over()


def test_two_passes_are_not_over_before_the_cursor_reaches_them():
    game = Game(9)
    play_all(game, ["E5", "pass", "pass"])
    game.navigate(2)
    assert not game.is_game_over()


def test_passes_separated_by_a_move_do_not_end_the_game():
    game = Game(9)
    play_all(game, ["pass", "E5", "pass"])
    assert not game.is_game_over()


def test_black_resigning_gives_w_plus_r():
    game = Game(9)
    game.resign(BLACK)
    assert game.result == "W+R"


def test_white_resigning_gives_b_plus_r():
    game = Game(9)
    play_all(game, ["E5"])
    game.resign(WHITE)
    assert game.result == "B+R"


def test_resigning_ends_the_game():
    game = Game(9)
    play_all(game, ["E5"])
    game.resign(WHITE)
    assert game.is_game_over()


def test_resignation_does_not_end_the_game_at_an_earlier_cursor():
    game = Game(9)
    play_all(game, ["E5", "F5"])
    game.resign(BLACK)
    game.navigate(1)
    assert not game.is_game_over()


def test_resignation_ends_the_game_again_back_at_the_last_move():
    game = Game(9)
    play_all(game, ["E5", "F5"])
    game.resign(BLACK)
    game.navigate(1)
    game.navigate(2)
    assert game.is_game_over()


def test_resignation_is_not_a_move():
    game = Game(9)
    play_all(game, ["E5"])
    game.resign(WHITE)
    assert game.move_count == 1


def test_game_over_is_reported_in_the_state():
    game = Game(9)
    game.resign(BLACK)
    assert game.to_dict()["gameOver"] is True


def test_result_is_reported_in_the_state():
    game = Game(9)
    game.resign(BLACK)
    assert game.to_dict()["result"] == "W+R"


def test_a_move_after_resignation_clears_the_result():
    game = Game(9)
    play_all(game, ["E5"])
    game.resign(WHITE)
    play_all(game, ["F5"])
    assert game.result == ""


def test_a_move_after_resignation_reopens_the_game():
    game = Game(9)
    play_all(game, ["E5"])
    game.resign(WHITE)
    play_all(game, ["F5"])
    assert not game.is_game_over()


def test_a_branch_from_the_past_clears_the_result():
    game = Game(9)
    play_all(game, ["E5", "F5"])
    game.resign(BLACK)
    game.navigate(1)
    game.play(WHITE, coords.from_gtp("C3", 9))
    assert game.result == ""


def test_undo_clears_the_result():
    game = Game(9)
    play_all(game, ["E5", "F5"])
    game.resign(BLACK)
    game.undo()
    assert game.result == ""


def test_navigation_keeps_the_result():
    game = Game(9)
    play_all(game, ["E5", "F5"])
    game.resign(BLACK)
    game.navigate(0)
    assert game.result == "W+R"


def test_a_pass_after_resignation_clears_the_result():
    game = Game(9)
    play_all(game, ["E5"])
    game.resign(WHITE)
    play_all(game, ["pass"])
    assert game.result == ""


# -- browser-facing state ------------------------------------------------------
STATE_FIELDS = {
    "size", "komi", "rules", "handicap", "stones", "captures", "toPlay", "cursor",
    "moveCount", "gameOver", "result", "players", "lastMove", "setupStones", "moves",
    "moveNumbers",
}


def test_state_carries_every_field():
    assert STATE_FIELDS <= set(Game(9).to_dict())


def test_state_stones_are_row_major_from_the_top_left():
    game = Game(9)
    game.play(BLACK, coords.from_gtp("B9", 9))
    assert game.to_dict()["stones"][1] == BLACK


def test_state_stone_count_is_the_board_area():
    assert len(Game(13).to_dict()["stones"]) == 169


def test_state_players_default_to_colour_names():
    assert Game(9).to_dict()["players"] == {"black": "Black", "white": "White"}


def test_state_last_move_is_none_at_the_start():
    assert Game(9).to_dict()["lastMove"] is None


def test_state_last_move_follows_the_cursor():
    game = Game(9)
    play_all(game, ["E5", "F5"])
    game.navigate(1)
    assert game.to_dict()["lastMove"] == "E5"


def test_state_moves_are_numbered_from_one():
    game = Game(9)
    play_all(game, ["E5", "F5"])
    assert [m["n"] for m in game.to_dict()["moves"]] == [1, 2]


def test_state_move_colours():
    game = Game(9)
    play_all(game, ["E5", "F5"])
    assert [m["color"] for m in game.to_dict()["moves"]] == ["black", "white"]


def test_state_move_comment():
    game = Game(9)
    game.play(BLACK, coords.from_gtp("E5", 9), "nice")
    assert game.to_dict()["moves"][0]["comment"] == "nice"


# 5x5: White A5 is captured by Black A4.
CAPTURE_LINE = ["B5", "A5", "A4"]


def test_capture_counts_in_the_state():
    game = Game(5)
    play_all(game, CAPTURE_LINE)
    assert game.to_dict()["captures"] == {"black": 1, "white": 0}


def test_capture_counts_follow_the_cursor():
    game = Game(5)
    play_all(game, CAPTURE_LINE)
    game.navigate(2)
    assert game.to_dict()["captures"] == {"black": 0, "white": 0}


def test_move_records_its_capture_count():
    game = Game(5)
    play_all(game, CAPTURE_LINE)
    assert [m["captures"] for m in game.to_dict()["moves"]] == [0, 0, 1]


def test_move_numbers_drop_captured_stones():
    game = Game(5)
    play_all(game, CAPTURE_LINE)
    assert game.move_numbers() == {"B5": 1, "A4": 3}


def test_move_numbers_show_the_latest_stone_on_a_replayed_point():
    game = Game(5)
    play_all(game, CAPTURE_LINE + ["E1", "A5"])
    assert game.move_numbers()["A5"] == 5


def test_move_numbers_follow_the_cursor():
    game = Game(5)
    play_all(game, CAPTURE_LINE)
    game.navigate(2)
    assert game.move_numbers() == {"B5": 1, "A5": 2}


def test_move_numbers_drop_a_suicided_group():
    # New Zealand: White A4 is suicide and takes White A5 (move 2) with it.
    game = Game(5, rules="new-zealand")
    play_all(game, ["B5", "A5", "B4", "E1", "A3", "A4"])
    assert game.move_numbers() == {"B5": 1, "B4": 3, "E1": 4, "A3": 5}


def test_move_numbers_are_in_the_state():
    game = Game(5)
    play_all(game, CAPTURE_LINE)
    assert game.to_dict()["moveNumbers"] == {"B5": 1, "A4": 3}


def test_handicap_stones_have_no_move_number():
    assert Game(19, handicap=2).move_numbers() == {}


def test_legal_moves_default_to_the_side_to_move():
    game = Game(2)
    play_all(game, ["A1"])
    assert sorted(game.legal_moves()) == sorted([(1, 0), (0, 0), (1, 1)])


def test_rules_are_reported_by_name():
    assert Game(9, rules="new-zealand").to_dict()["rules"] == rules.get_rules("new-zealand").name
