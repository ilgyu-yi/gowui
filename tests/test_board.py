"""The board: stones, groups, captures and prisoner counts (SPEC §1.3, §1.4)."""

from __future__ import annotations

import pytest

from gowui.board import BLACK, EMPTY, WHITE, Board, IllegalMove, color_from_name, opponent


def test_opponent_of_black_is_white():
    assert opponent(BLACK) == WHITE


def test_opponent_of_white_is_black():
    assert opponent(WHITE) == BLACK


@pytest.mark.parametrize("name,color", [("b", BLACK), ("B", BLACK), ("black", BLACK),
                                        ("w", WHITE), ("W", WHITE), ("white", WHITE)])
def test_color_names(name, color):
    assert color_from_name(name) == color


def test_unknown_color_name_is_refused():
    with pytest.raises(ValueError):
        color_from_name("red")


def test_new_board_is_empty():
    board = Board(9)
    assert all(board.get(x, y) == EMPTY for y in range(9) for x in range(9))


def test_played_stone_is_on_the_board():
    board = Board(9)
    board.play(BLACK, 2, 3)
    assert board.get(2, 3) == BLACK


def test_occupied_point_is_refused():
    board = Board(9)
    board.play(BLACK, 2, 3)
    with pytest.raises(IllegalMove):
        board.play(WHITE, 2, 3)


@pytest.mark.parametrize("x,y", [(-1, 0), (0, -1), (9, 0), (0, 9)])
def test_off_board_point_is_refused(x, y):
    with pytest.raises(IllegalMove):
        Board(9).play(BLACK, x, y)


def _corner_capture():
    """White (0,0) surrounded by Black (1,0) and then (0,1)."""
    board = Board(5)
    board.play(WHITE, 0, 0)
    board.play(BLACK, 1, 0)
    return board, board.play(BLACK, 0, 1)


def test_capture_reports_the_captured_points():
    assert sorted(_corner_capture()[1]) == [(0, 0)]


def test_captured_stone_leaves_the_board():
    assert _corner_capture()[0].get(0, 0) == EMPTY


def test_capture_credits_the_capturer():
    assert _corner_capture()[0].captures == {BLACK: 1, WHITE: 0}


def test_a_group_is_captured_as_a_whole():
    board = Board(5)
    for x, y in [(0, 0), (1, 0)]:
        board.play(WHITE, x, y)
    for x, y in [(2, 0), (0, 1)]:
        board.play(BLACK, x, y)
    board.play(BLACK, 1, 1)
    assert board.captures[BLACK] == 2


def test_multiple_groups_are_captured_by_one_move():
    # White (1,0) and (0,1) are separate stones whose last shared liberty is (0,0).
    board = Board(5)
    board.play(WHITE, 1, 0)
    board.play(WHITE, 0, 1)
    for x, y in [(2, 0), (1, 1), (0, 2)]:
        board.play(BLACK, x, y)
    board.play(BLACK, 0, 0)
    assert board.captures[BLACK] == 2


def _suicide_board():
    board = Board(5)
    board.play(BLACK, 1, 0)
    board.play(BLACK, 0, 1)
    return board


def test_suicide_is_refused_by_default():
    with pytest.raises(IllegalMove):
        _suicide_board().play(WHITE, 0, 0)


def test_refused_suicide_leaves_the_point_empty():
    board = _suicide_board()
    with pytest.raises(IllegalMove):
        board.play(WHITE, 0, 0)
    assert board.get(0, 0) == EMPTY


def test_allowed_suicide_removes_the_stone():
    board = _suicide_board()
    board.play(WHITE, 0, 0, allow_suicide=True)
    assert board.get(0, 0) == EMPTY


def test_allowed_suicide_credits_the_opponent():
    board = _suicide_board()
    board.play(WHITE, 0, 0, allow_suicide=True)
    assert board.captures == {BLACK: 1, WHITE: 0}


def test_allowed_suicide_removes_the_whole_own_group():
    board = Board(5)
    board.play(WHITE, 0, 0)
    for x, y in [(2, 0), (1, 1), (0, 1)]:
        board.play(BLACK, x, y)
    board.play(WHITE, 1, 0, allow_suicide=True)
    assert (board.get(0, 0), board.get(1, 0)) == (EMPTY, EMPTY)


def test_capture_comes_before_the_suicide_check():
    # Black (0,0) has no liberty of its own but takes White (1,0)'s last liberty.
    board = Board(5)
    board.play(WHITE, 1, 0)
    board.play(BLACK, 2, 0)
    board.play(BLACK, 1, 1)
    board.play(WHITE, 0, 1)
    board.play(BLACK, 0, 0)
    assert board.get(1, 0) == EMPTY


def test_copy_is_independent():
    board = Board(5)
    clone = board.copy()
    clone.play(BLACK, 2, 2)
    assert board.get(2, 2) == EMPTY
