"""Coordinates: the strict GTP vertex grammar (SPEC §0), SGF points (§1.5), handicap points (§1.2)."""

from __future__ import annotations

import pytest

from gowui import coords
from gowui.coords import CoordinateError


def _all_points(size):
    return [(x, y) for y in range(size) for x in range(size)]


@pytest.mark.parametrize("size", range(2, 26))
def test_every_point_round_trips_through_gtp(size):
    assert all(coords.from_gtp(coords.to_gtp(p, size), size) == p for p in _all_points(size))


@pytest.mark.parametrize("size", range(2, 26))
def test_every_point_round_trips_through_sgf(size):
    assert all(coords.from_sgf(coords.to_sgf(p, size), size) == p for p in _all_points(size))


@pytest.mark.parametrize("size", range(2, 26))
def test_every_gtp_vertex_is_distinct(size):
    assert len({coords.to_gtp(p, size) for p in _all_points(size)}) == size * size


def test_gtp_rows_count_from_the_bottom():
    assert coords.to_gtp((0, 0), 19) == "A19"


def test_gtp_columns_skip_i():
    assert coords.to_gtp((8, 18), 19) == "J1"


def test_gtp_column_letter_is_case_insensitive():
    assert coords.from_gtp("d4", 19) == coords.from_gtp("D4", 19)


def test_pass_is_a_move():
    assert coords.from_gtp("pass", 19) is None


def test_a_pass_is_written_as_pass():
    assert coords.to_gtp(None, 19) == "pass"


@pytest.mark.parametrize(
    "vertex",
    [
        "",           # empty
        "I5",         # I is not a column
        "i5",
        "A0",         # rows start at 1
        "A05",        # leading zero
        "A+5",        # sign
        "A-5",
        "A 5",        # embedded space
        "A1_0",       # digit separator
        "A5.0",
        "A５",    # fullwidth digit five
        "A٣",    # Arabic-Indic digit three
        "A",          # no row
        "5",          # no column
        "AA5",        # two letters
        "5A",
        "A100",       # three digits
        "T20",        # off a 19x19 board
        "U1",         # column off a 19x19 board
        "resign",     # resign is never a vertex
        "RESIGN",
        "Resign",
    ],
)
def test_malformed_or_off_board_vertex_is_refused(vertex):
    with pytest.raises(CoordinateError):
        coords.from_gtp(vertex, 19)


def test_coordinate_error_is_a_value_error():
    assert issubclass(CoordinateError, ValueError)


def test_largest_vertex_on_a_25x25_board():
    assert coords.from_gtp("Z25", 25) == (24, 0)


def test_off_board_point_cannot_be_written_as_gtp():
    with pytest.raises(CoordinateError):
        coords.to_gtp((19, 0), 19)


# -- SGF points ---------------------------------------------------------------
def test_empty_sgf_value_is_a_pass():
    assert coords.from_sgf("", 19) is None


def test_sgf_pass_is_written_empty():
    assert coords.to_sgf(None, 19) == ""


def test_sgf_tt_is_a_pass_on_19x19():
    assert coords.from_sgf("tt", 19) is None


def test_sgf_tt_is_a_pass_on_small_boards():
    assert coords.from_sgf("tt", 9) is None


def test_sgf_tt_is_a_point_above_19x19():
    assert coords.from_sgf("tt", 20) == (19, 19)


def test_sgf_origin_is_top_left():
    assert coords.from_sgf("ab", 19) == (0, 1)


@pytest.mark.parametrize("text", ["a", "abc", "a1", "zz", "ta"])
def test_malformed_or_off_board_sgf_point_is_refused(text):
    with pytest.raises(CoordinateError):
        coords.from_sgf(text, 9)


# -- handicap points (GTP 2 fixed_handicap) -----------------------------------
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
def test_handicap_points_follow_gtp_order_on_19x19(count):
    assert [coords.to_gtp(p, 19) for p in coords.handicap_points(19, count)] == GTP_19[count]
