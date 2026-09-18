"""SGF writing and reading (SPEC §1.5)."""

from __future__ import annotations

import importlib.metadata

import pytest

from gowui import coords, sgf
from gowui.board import BLACK, WHITE
from gowui.game import Game
from gowui.sgf import SGFError, SGFNode


def play_all(game, vertices):
    for vertex in vertices:
        color = BLACK if game.to_dict()["toPlay"] == "black" else WHITE
        game.play(color, coords.from_gtp(vertex, game.size))


def move_vertices(game):
    return [m["vertex"] for m in game.to_dict()["moves"]]


def setup_of(game):
    return sorted((s["color"], s["vertex"]) for s in game.to_dict()["setupStones"])


def load(body, size=9):
    return Game.from_sgf(f"(;GM[1]FF[4]SZ[{size}]{body})")


TRICKY = "a]b\\c\nd ] \\] end\\"


# -- writing --------------------------------------------------------------------
def test_written_sgf_names_the_application_and_package_version():
    version = importlib.metadata.version("gowui")
    assert f"AP[gowui:{version}]" in Game(9).to_sgf()


@pytest.mark.parametrize("prop", ["GM[1]", "FF[4]", "CA[UTF-8]", "SZ[13]", "KM[7.5]",
                                  "RU[chinese]", "PB[Black]", "PW[White]"])
def test_written_root_properties(prop):
    assert prop in Game(13, rules="chinese").to_sgf()


def test_even_game_writes_no_handicap():
    assert "HA[" not in Game(19).to_sgf()


def test_handicap_game_writes_ha():
    assert "HA[3]" in Game(19, handicap=3).to_sgf()


def test_handicap_game_writes_no_pl():
    assert "PL[" not in Game(19, handicap=3).to_sgf()


def test_no_result_writes_no_re():
    assert "RE[" not in Game(9).to_sgf()


def test_pass_is_written_as_an_empty_value():
    game = Game(9)
    play_all(game, ["E5", "pass"])
    assert ";W[]" in game.to_sgf()


def test_move_comment_is_written_escaped():
    game = Game(9)
    game.play(BLACK, (0, 0), "x]y\\z")
    assert "C[x\\]y\\\\z]" in game.to_sgf()


def test_dump_then_parse_keeps_escaped_values():
    node = SGFNode()
    node.properties["C"] = [TRICKY]
    assert sgf.parse(sgf.dump([node]))[0].properties["C"] == [TRICKY]


# -- round trips ----------------------------------------------------------------
def test_round_trip_keeps_the_whole_game():
    game = Game(19, rules="chinese", handicap=3)
    game.play(WHITE, coords.from_gtp("O3", 19), TRICKY)
    game.play(BLACK, coords.from_gtp("C3", 19))
    game.play(WHITE, None, "pass\r\nwith CRLF")
    game.play(BLACK, coords.from_gtp("R17", 19), "")
    assert Game.from_sgf(game.to_sgf()).to_dict() == game.to_dict()


def test_round_trip_keeps_white_setup_and_first_player():
    game = load("PB[Lee \\] Sedol]PW[A\\\\B]RE[B+3.5]AB[cc][dd]AW[ee]PL[W];W[aa]C[hi];B[bb]")
    assert Game.from_sgf(game.to_sgf()).to_dict() == game.to_dict()


def test_round_trip_keeps_the_handicap_komi_and_side_to_move():
    game = Game(19, handicap=5)
    restored = Game.from_sgf(game.to_sgf())
    assert (restored.komi, restored.handicap, restored.to_dict()["toPlay"]) == (0.5, 5, "white")


def test_round_trip_keeps_an_explicit_komi():
    assert Game.from_sgf(Game(19, komi=-3.5).to_sgf()).komi == -3.5


def test_resigned_game_round_trips_as_over():
    game = Game(9)
    play_all(game, ["E5", "F5"])
    game.resign(BLACK)
    assert Game.from_sgf(game.to_sgf()).to_dict()["gameOver"] is True


def test_resigned_game_round_trips_its_result():
    game = Game(9)
    play_all(game, ["E5", "F5"])
    game.resign(BLACK)
    assert Game.from_sgf(game.to_sgf()).result == "W+R"


def test_pl_is_written_when_white_moves_first_in_an_even_game():
    assert "PL[W]" in load("AB[cc]AW[dd]PL[W]").to_sgf()


# -- reading: basics --------------------------------------------------------------
def test_loaded_game_puts_the_cursor_at_the_end():
    game = load(";B[aa];W[bb]")
    assert game.cursor == game.move_count == 2


def test_missing_sz_means_19():
    assert Game.from_sgf("(;GM[1]FF[4])").size == 19


def test_sz_n_colon_n_is_accepted():
    assert Game.from_sgf("(;GM[1]FF[4]SZ[9:9])").size == 9


@pytest.mark.parametrize("sz", ["19:13", "9:19", "1", "26", "0", "-9", "abc", "", "9.0"])
def test_bad_sz_is_an_sgf_error(sz):
    with pytest.raises(SGFError):
        Game.from_sgf(f"(;GM[1]FF[4]SZ[{sz}])")


def test_komi_is_read():
    assert load("KM[5.5]").komi == 5.5


def test_missing_komi_is_the_rule_default():
    assert load("RU[Chinese]").komi == 7.5


@pytest.mark.parametrize("km", ["nan", "NaN", "inf", "-inf", "abc", "1e999"])
def test_non_finite_or_non_numeric_komi_is_an_sgf_error(km):
    with pytest.raises(SGFError):
        load(f"KM[{km}]")


@pytest.mark.parametrize(
    "ru,expected",
    [
        ("Japanese", "japanese"), ("jp", "japanese"), ("JP", "japanese"),
        ("korean", "korean"), ("Chinese", "chinese"), ("cn", "chinese"), ("AGA", "aga"),
        ("NZ", "new-zealand"), ("New Zealand", "new-zealand"), ("new_zealand", "new-zealand"),
        ("trompTaylor", "tromp-taylor"), ("Tromp Taylor", "tromp-taylor"),
        ("tromp_taylor", "tromp-taylor"), ("Ing", "japanese"), ("", "japanese"),
    ],
)
def test_rule_set_aliases(ru, expected):
    assert load(f"RU[{ru}]").to_dict()["rules"] == expected


def test_missing_rule_set_is_japanese():
    assert load("").to_dict()["rules"] == "japanese"


def test_player_names_are_read():
    assert load("PB[Shin]PW[Park]").to_dict()["players"] == {"black": "Shin", "white": "Park"}


def test_pl_w_sets_the_first_player():
    assert load("AB[cc]AW[dd]PL[W]").to_dict()["toPlay"] == "white"


def test_setup_stones_alone_do_not_make_it_whites_turn():
    assert load("AW[cc]").to_dict()["toPlay"] == "black"


def test_moves_are_read():
    assert move_vertices(load(";B[aa];W[ii];B[]")) == ["A9", "J1", "pass"]


def test_move_comments_are_read():
    assert load(";B[aa]C[hello]").to_dict()["moves"][0]["comment"] == "hello"


def test_root_comment_is_dropped():
    game = load("C[root];B[aa]")
    assert game.to_dict()["moves"][0]["comment"] == ""


def test_result_is_read_and_kept_as_text():
    assert load("RE[B+3.5];B[aa]").result == "B+3.5"


def test_scored_result_does_not_end_the_game():
    assert load("RE[B+3.5];B[aa];W[bb]").to_dict()["gameOver"] is False


@pytest.mark.parametrize("re_", ["W+T", "Void", "?", "B+F", "0"])
def test_other_results_do_not_end_the_game(re_):
    assert load(f"RE[{re_}];B[aa]").is_game_over() is False


def test_scored_result_round_trips():
    game = load("RE[B+3.5];B[aa]")
    assert "RE[B+3.5]" in game.to_sgf()


def test_scored_result_is_over_after_two_passes():
    assert load("RE[B+3.5];B[aa];W[];B[]").is_game_over()


@pytest.mark.parametrize("re_", ["B+R", "W+R", "B+Resign", "W+Resign"])
def test_loaded_resignation_ends_the_game_at_the_last_move(re_):
    assert load(f"RE[{re_}];B[aa];W[bb]").is_game_over()


def test_loaded_resignation_does_not_end_the_game_earlier():
    game = load("RE[W+R];B[aa];W[bb]")
    game.navigate(1)
    assert not game.is_game_over()


def test_a_move_after_a_loaded_scored_result_clears_it():
    game = load("RE[B+3.5];B[aa]")
    play_all(game, ["E5"])
    assert game.result == ""


# -- reading: values ------------------------------------------------------------
@pytest.mark.parametrize("brk", ["\n", "\r", "\r\n", "\n\r"])
def test_backslash_line_break_is_a_soft_break(brk):
    nodes = sgf.parse(f"(;C[ab\\{brk}cd])")
    assert nodes[0].properties["C"] == ["abcd"]


def test_escaped_bracket_and_backslash_are_read():
    nodes = sgf.parse("(;C[a\\]b\\\\c])")
    assert nodes[0].properties["C"] == ["a]b\\c"]


def test_backslash_escapes_any_character():
    nodes = sgf.parse("(;C[\\a\\:b])")
    assert nodes[0].properties["C"] == ["a:b"]


def test_plain_newline_in_a_comment_is_kept():
    assert load(";B[aa]C[line1\nline2]").to_dict()["moves"][0]["comment"] == "line1\nline2"


def test_tt_is_a_pass_on_19x19():
    assert move_vertices(load(";B[dd];W[tt]", size=19)) == ["D16", "pass"]


def test_tt_is_a_point_above_19x19():
    assert move_vertices(load(";B[tt]", size=21)) == ["U2"]


# -- reading: setup -------------------------------------------------------------
def test_compressed_point_list_is_expanded():
    assert len(load("AB[aa:cc]").to_dict()["setupStones"]) == 9


def test_compressed_point_list_covers_the_rectangle():
    assert setup_of(load("AW[ab:bc]")) == sorted(
        [("white", "A8"), ("white", "B8"), ("white", "A7"), ("white", "B7")])


def test_a_point_listed_twice_in_one_colour_counts_once():
    assert len(load("AB[aa][aa][aa:bb]").to_dict()["setupStones"]) == 4


def test_ae_in_the_root_is_ignored():
    assert setup_of(load("AB[aa]AE[aa]")) == [("black", "A9")]


def test_ae_in_the_root_does_not_stop_reading():
    assert move_vertices(load("AE[bb];B[cc]")) == ["C7"]


def test_handicap_in_sgf_is_free_placement():
    assert setup_of(load("HA[2]AB[aa][ii]", size=19)) == [("black", "A19"), ("black", "J11")]


def test_handicap_in_sgf_does_not_follow_the_size_table():
    assert load("HA[2]AB[aa][bb]", size=5).handicap == 2


def test_handicap_in_sgf_takes_the_half_point_komi():
    assert load("HA[4]AB[pd][dp][pp][dd]", size=19).komi == 0.5


def test_handicap_in_sgf_starts_with_white():
    assert load("HA[4]AB[pd][dp][pp][dd]", size=19).to_dict()["toPlay"] == "white"


def test_handicap_in_sgf_keeps_an_explicit_komi():
    assert load("HA[4]KM[3.5]AB[pd][dp][pp][dd]", size=19).komi == 3.5


def test_handicap_without_setup_stones_has_no_effect():
    game = load("HA[4]", size=19)
    assert (game.handicap, game.komi, game.to_dict()["toPlay"]) == (0, 6.5, "black")


# -- reading: where it stops -----------------------------------------------------
def test_variations_are_dropped():
    game = Game.from_sgf("(;GM[1]FF[4]SZ[9](;B[aa];W[bb](;B[cc])(;B[dd]))(;B[ee]))")
    assert move_vertices(game) == ["A9", "B8", "C7"]


def test_a_moderately_nested_main_line_is_read():
    text = "(;GM[1]FF[4]SZ[9]" + "".join(f"(;{'B' if i % 2 == 0 else 'W'}[{chr(97 + i)}a]"
                                          for i in range(9)) + ")" * 10
    assert Game.from_sgf(text).move_count == 9


def test_an_illegal_move_stops_reading():
    assert move_vertices(load(";B[aa];W[aa];B[bb]")) == ["A9"]


def test_a_suicide_stops_reading_under_rules_that_forbid_it():
    assert load("AB[ab][ba];B[ee];W[aa];B[cc]").move_count == 1


def test_a_suicide_is_read_under_rules_that_allow_it():
    assert load("RU[NZ]AB[ab][ba];B[ee];W[aa];B[cc]").move_count == 3


def test_an_off_board_move_stops_reading():
    assert move_vertices(load(";B[aa];W[zz];B[bb]")) == ["A9"]


def test_an_off_board_move_on_19x19_stops_reading():
    assert move_vertices(load(";B[aa];W[ua];B[bb]", size=19)) == ["A19"]


@pytest.mark.parametrize("value", ["a", "zzz", "a1", "1a", " ", "aa:bb"])
def test_a_move_that_is_not_a_valid_point_stops_reading(value):
    assert move_vertices(load(f";B[aa];W[{value}];B[bb]")) == ["A9"]


@pytest.mark.parametrize("prop", ["AB[cc]", "AW[cc]", "AE[aa]"])
def test_setup_after_the_root_stops_reading(prop):
    assert move_vertices(load(f";B[aa];{prop};W[bb]")) == ["A9"]


def test_setup_in_a_move_node_stops_reading_at_that_node():
    assert move_vertices(load(";B[aa];W[bb]AB[cc];B[dd]")) == ["A9"]


def test_a_long_capture_recapture_game_loads():
    # Two kos on a 9x9 board, fought for thousands of plies under simple ko:
    # B takes ko A, W takes ko B, B passes, W retakes A, B retakes B, W passes, repeat.
    setup = {
        "B": [(4, 2), (3, 1), (3, 3), (4, 6), (3, 5), (3, 7), (2, 6)],
        "W": [(1, 2), (2, 1), (2, 3), (3, 2), (1, 6), (2, 5), (2, 7)],
    }
    cycle = [("B", (2, 2)), ("W", (3, 6)), ("B", None),
             ("W", (3, 2)), ("B", (2, 6)), ("W", None)]
    cycles = 700
    root = "".join(f"A{c}" + "".join(f"[{coords.to_sgf(p, 9)}]" for p in pts)
                   for c, pts in setup.items())
    body = "".join(f";{c}[{coords.to_sgf(p, 9)}]" for c, p in cycle) * cycles
    game = Game.from_sgf(f"(;GM[1]FF[4]SZ[9]{root}{body})")
    assert game.move_count == len(cycle) * cycles


# -- reading: errors ---------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n",
        "not sgf at all",
        ";SZ[9]",
        "(",
        "()",
        "(;SZ[9]",
        "(;SZ[9];B[aa",
        "(;SZ[9]C[unterminated)",
        "(;SZ[9]5)",
        ")(;SZ[9])",
    ],
)
def test_unreadable_text_is_an_sgf_error(text):
    with pytest.raises(SGFError):
        Game.from_sgf(text)


@pytest.mark.parametrize("text", ["", "garbage", "(", "(;C[x"])
def test_parse_raises_sgf_error(text):
    with pytest.raises(SGFError):
        sgf.parse(text)


def test_deep_nesting_is_an_sgf_error():
    with pytest.raises(SGFError):
        Game.from_sgf("(" * 5000 + ";SZ[9]" + ")" * 5000)


def test_deep_unbalanced_nesting_is_an_sgf_error():
    with pytest.raises(SGFError):
        Game.from_sgf("(" * 5000)


@pytest.mark.parametrize("ha", ["x", "", "2.5", "two"])
def test_non_numeric_ha_is_an_sgf_error(ha):
    with pytest.raises(SGFError):
        load(f"HA[{ha}]AB[aa][bb]")


@pytest.mark.parametrize("prop", ["AB[zz]", "AW[jj]", "AB[a]", "AB[abc]", "AB[aa:zz]"])
def test_malformed_or_off_board_setup_point_is_an_sgf_error(prop):
    with pytest.raises(SGFError):
        load(prop)


def test_a_point_in_both_ab_and_aw_is_an_sgf_error():
    with pytest.raises(SGFError):
        load("AB[aa][bb]AW[bb]")


def test_a_compressed_overlap_between_ab_and_aw_is_an_sgf_error():
    with pytest.raises(SGFError):
        load("AB[aa:cc]AW[bb]")
