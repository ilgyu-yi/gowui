"""Rule sets (SPEC §1.1) and legality under them (§1.3), including a brute-force reference check."""

from __future__ import annotations

import random

import pytest

from gowui import coords, rules
from gowui.board import BLACK, EMPTY, WHITE, IllegalMove, opponent
from gowui.game import Game

# -- the rule-set table -------------------------------------------------------
TABLE = {
    "japanese": (rules.SIMPLE_KO, False, 6.5, "territory"),
    "korean": (rules.SIMPLE_KO, False, 6.5, "territory"),
    "chinese": (rules.POSITIONAL_SUPERKO, False, 7.5, "area"),
    "aga": (rules.SITUATIONAL_SUPERKO, False, 7.5, "area"),
    "new-zealand": (rules.SITUATIONAL_SUPERKO, True, 7.0, "area"),
    "tromp-taylor": (rules.POSITIONAL_SUPERKO, True, 7.5, "area"),
}


def test_rule_set_names_are_exactly_the_table():
    assert set(rules.RULE_SETS) == set(TABLE)


@pytest.mark.parametrize("name", sorted(TABLE))
def test_rule_set_matches_the_table(name):
    rs = rules.get_rules(name)
    assert (rs.ko, rs.suicide, rs.default_komi, rs.scoring) == TABLE[name]


@pytest.mark.parametrize("name", sorted(TABLE))
def test_rule_set_name_goes_to_the_engine_unchanged(name):
    assert rules.get_rules(name).katago == name


def test_default_rule_set_is_japanese():
    assert rules.DEFAULT_RULES == "japanese"


def test_handicap_komi_is_half_a_point():
    assert rules.HANDICAP_KOMI == 0.5


def test_unknown_rule_set_is_refused():
    with pytest.raises(ValueError):
        rules.get_rules("ing")


# -- helpers --------------------------------------------------------------------
def play_all(game, vertices):
    """Play vertices alternately, starting with the side to move."""
    for vertex in vertices:
        color = BLACK if game.to_dict()["toPlay"] == "black" else WHITE
        game.play(color, coords.from_gtp(vertex, game.size))


def pt(vertex, size=5):
    return coords.from_gtp(vertex, size)


# -- suicide ------------------------------------------------------------------
# Black B5, B4, A3 surround White A5; White A4 then has no liberty and captures nothing.
SUICIDE_SETUP = ["B5", "A5", "B4", "E1", "A3"]


@pytest.mark.parametrize("name", ["japanese", "korean", "chinese", "aga"])
def test_suicide_is_refused_where_the_rules_forbid_it(name):
    game = Game(5, rules=name)
    play_all(game, SUICIDE_SETUP)
    assert game.legal_error(WHITE, pt("A4")) is not None


@pytest.mark.parametrize("name", ["japanese", "korean", "chinese", "aga"])
def test_playing_a_forbidden_suicide_raises(name):
    game = Game(5, rules=name)
    play_all(game, SUICIDE_SETUP)
    with pytest.raises(IllegalMove):
        game.play(WHITE, pt("A4"))


@pytest.mark.parametrize("name", ["japanese", "korean", "chinese", "aga"])
def test_a_refused_suicide_changes_nothing(name):
    game = Game(5, rules=name)
    play_all(game, SUICIDE_SETUP)
    before = game.to_dict()
    with pytest.raises(IllegalMove):
        game.play(WHITE, pt("A4"))
    assert game.to_dict() == before


@pytest.mark.parametrize("name", ["new-zealand", "tromp-taylor"])
def test_suicide_removes_the_whole_own_group_where_legal(name):
    game = Game(5, rules=name)
    play_all(game, SUICIDE_SETUP)
    game.play(WHITE, pt("A4"))
    assert (game.board.get(*pt("A4")), game.board.get(*pt("A5"))) == (EMPTY, EMPTY)


@pytest.mark.parametrize("name", ["new-zealand", "tromp-taylor"])
def test_suicided_stones_are_prisoners_of_the_opponent(name):
    game = Game(5, rules=name)
    play_all(game, SUICIDE_SETUP)
    game.play(WHITE, pt("A4"))
    assert game.to_dict()["captures"] == {"black": 2, "white": 0}


def test_capture_is_resolved_before_the_suicide_check():
    # Black A4 has no liberty of its own, but it takes White A5's last liberty.
    game = Game(5, rules="japanese")
    play_all(game, ["B5", "A5", "E1", "B4", "E2", "A3"])
    assert game.legal_error(BLACK, pt("A4")) is None


def test_a_capturing_move_without_own_liberties_removes_the_captured_stone():
    game = Game(5, rules="japanese")
    play_all(game, ["B5", "A5", "E1", "B4", "E2", "A3", "A4"])
    assert game.board.get(*pt("A5")) == EMPTY


# -- ko -------------------------------------------------------------------------
def ko_shape(name="japanese"):
    """Black takes a ko at C3; White wants to retake at D3 straight away."""
    game = Game(5, rules=name)
    play_all(game, ["E3", "B3", "D4", "C4", "D2", "C2", "A1", "D3", "C3"])
    return game


@pytest.mark.parametrize("name", sorted(TABLE))
def test_immediate_ko_recapture_is_refused(name):
    assert ko_shape(name).legal_error(WHITE, pt("D3")) is not None


def test_ko_capture_counts_a_prisoner():
    assert ko_shape().to_dict()["captures"] == {"black": 1, "white": 0}


def test_ko_error_names_ko():
    assert "ko" in ko_shape().legal_error(WHITE, pt("D3")).lower()


@pytest.mark.parametrize("name", sorted(TABLE))
def test_ko_can_be_retaken_after_a_threat_is_answered(name):
    game = ko_shape(name)
    play_all(game, ["A5", "B1"])
    assert game.legal_error(WHITE, pt("D3")) is None


def ko_after_two_passes(name):
    """The ko is taken, both sides pass, and White retakes: the position after the retake
    already occurred (after White's D3, Black to move)."""
    game = ko_shape(name)
    play_all(game, ["pass", "pass"])
    return game


def test_simple_ko_allows_a_retake_after_passes():
    assert ko_after_two_passes("japanese").legal_error(WHITE, pt("D3")) is None


@pytest.mark.parametrize("name", ["chinese", "tromp-taylor"])
def test_positional_superko_refuses_a_repeat_after_passes(name):
    assert ko_after_two_passes(name).legal_error(WHITE, pt("D3")) is not None


@pytest.mark.parametrize("name", ["aga", "new-zealand"])
def test_situational_superko_refuses_a_repeat_with_the_same_player_to_move(name):
    assert ko_after_two_passes(name).legal_error(WHITE, pt("D3")) is not None


def test_positional_superko_refuses_a_suicide_that_repeats_the_position():
    # White's A5 suicide leaves exactly the position before it (White to move then,
    # Black to move after): a repeat of the stones only.
    game = Game(5, rules="tromp-taylor")
    play_all(game, ["B5", "E1", "A4"])
    assert game.legal_error(WHITE, pt("A5")) is not None


def test_situational_superko_allows_a_repeat_with_the_other_player_to_move():
    game = Game(5, rules="new-zealand")
    play_all(game, ["B5", "E1", "A4"])
    assert game.legal_error(WHITE, pt("A5")) is None


def test_positions_after_the_cursor_do_not_count_for_superko():
    game = Game(5, rules="chinese")
    play_all(game, ["C3", "D3", "B2", "A1"])
    game.navigate(1)
    assert game.legal_error(WHITE, pt("D3")) is None


# White stones on A4 and B5 make A5 a suicide point for Black on the otherwise empty board.
def test_situational_superko_counts_the_first_player_from_pl():
    # With PL[W] the empty-corner start occurred with White to move, so a Black suicide
    # that recreates it (White to move again) repeats that situation.
    game = Game.from_sgf("(;GM[1]FF[4]SZ[5]RU[NZ]AW[ab][ba]PL[W])")
    assert game.legal_error(BLACK, pt("A5")) is not None


def test_situational_superko_allows_that_suicide_when_black_moved_first():
    game = Game.from_sgf("(;GM[1]FF[4]SZ[5]RU[NZ]AW[ab][ba])")
    assert game.legal_error(BLACK, pt("A5")) is None


# -- brute-force reference ------------------------------------------------------
def _neighbours(size, i):
    x, y = i % size, i // size
    if x > 0:
        yield i - 1
    if x + 1 < size:
        yield i + 1
    if y > 0:
        yield i - size
    if y + 1 < size:
        yield i + size


def _chain(size, stones, i):
    color = stones[i]
    seen, stack, libs = {i}, [i], False
    while stack:
        c = stack.pop()
        for n in _neighbours(size, c):
            if stones[n] == EMPTY:
                libs = True
            elif stones[n] == color and n not in seen:
                seen.add(n)
                stack.append(n)
    return seen, libs


def _apply(size, stones, color, point, suicide_ok):
    """Return the stones after the move, or None if it is occupied or forbidden suicide."""
    if point is None:
        return stones
    i = point[1] * size + point[0]
    if stones[i] != EMPTY:
        return None
    board = list(stones)
    board[i] = color
    for n in _neighbours(size, i):
        if board[n] == opponent(color):
            chain, libs = _chain(size, board, n)
            if not libs:
                for c in chain:
                    board[c] = EMPTY
    chain, libs = _chain(size, board, i)
    if not libs:
        if not suicide_ok:
            return None
        for c in chain:
            board[c] = EMPTY
    return tuple(board)


class Shadow:
    """An independent record of the line of play, replayed from scratch every time."""

    def __init__(self, size, rule_name, setup, first):
        self.size, self.rules = size, rules.get_rules(rule_name)
        self.setup, self.first = setup, first
        self.moves, self.cursor = [], 0

    def history(self):
        stones = [EMPTY] * (self.size * self.size)
        for color, (x, y) in self.setup:
            stones[y * self.size + x] = color
        stones = tuple(stones)
        positions, players = [stones], [self.first]
        for color, point in self.moves[: self.cursor]:
            stones = _apply(self.size, stones, color, point, self.rules.suicide)
            assert stones is not None, "shadow replayed an illegal move"
            positions.append(stones)
            players.append(opponent(color))
        return positions, players

    def to_play(self):
        return self.first if self.cursor == 0 else opponent(self.moves[self.cursor - 1][0])

    def legal(self, positions, players, color, point):
        new = _apply(self.size, positions[-1], color, point, self.rules.suicide)
        if new is None:
            return False
        if point is None:
            return True
        if self.rules.ko == rules.SIMPLE_KO:
            return not (len(positions) >= 2 and new == positions[-2])
        if self.rules.ko == rules.POSITIONAL_SUPERKO:
            return new not in positions
        return (new, opponent(color)) not in set(zip(positions, players))


def _check(game, shadow, step, problems):
    positions, players = shadow.history()
    size = shadow.size
    state = game.to_dict()
    if state["cursor"] != shadow.cursor:
        problems.append(f"step {step}: cursor {state['cursor']} != {shadow.cursor}")
    expected_moves = [coords.to_gtp(p, size) for _, p in shadow.moves]
    if [m["vertex"] for m in state["moves"]] != expected_moves:
        problems.append(f"step {step}: moves {state['moves']} != {expected_moves}")
    if state["toPlay"] != ("black" if shadow.to_play() == BLACK else "white"):
        problems.append(f"step {step}: toPlay {state['toPlay']}")
    current = positions[-1]
    for y in range(size):
        for x in range(size):
            if game.board.get(x, y) != current[y * size + x]:
                problems.append(f"step {step}: stone at {(x, y)} differs")
    for color in (BLACK, WHITE):
        for y in range(size):
            for x in range(size):
                want = shadow.legal(positions, players, color, (x, y))
                got = game.legal_error(color, (x, y)) is None
                if want != got:
                    problems.append(
                        f"step {step}: {'B' if color == BLACK else 'W'} {coords.to_gtp((x, y), size)} "
                        f"legal={got}, reference says {want}; moves={expected_moves[: shadow.cursor]}"
                    )
    to_play = shadow.to_play()
    want_set = {(x, y) for y in range(size) for x in range(size)
                if shadow.legal(positions, players, to_play, (x, y))}
    if set(game.legal_moves(to_play)) != want_set:
        problems.append(f"step {step}: legal_moves differs")


def _drive(game, shadow, seed, steps):
    rng = random.Random(seed)
    problems = []
    _check(game, shadow, -1, problems)
    for step in range(steps):
        roll = rng.random()
        if roll < 0.04 and shadow.cursor:
            target = rng.randrange(0, shadow.cursor)
            game.navigate(target)
            shadow.cursor = target
        elif roll < 0.06:
            game.navigate(len(shadow.moves))
            shadow.cursor = len(shadow.moves)
        elif roll < 0.10:
            game.undo()
            if shadow.cursor:
                shadow.cursor -= 1
                del shadow.moves[shadow.cursor:]
            if game.to_dict()["result"] != "":
                problems.append(f"step {step}: undo kept the result")
        elif roll < 0.12:
            game.resign(shadow.to_play())
        else:
            positions, players = shadow.history()
            color = shadow.to_play()
            options = [(x, y) for y in range(shadow.size) for x in range(shadow.size)
                       if shadow.legal(positions, players, color, (x, y))]
            point = None if (roll < 0.17 or not options) else rng.choice(options)
            try:
                game.play(color, point)
            except IllegalMove as exc:
                problems.append(f"step {step}: refused a legal move {point}: {exc}")
                break
            del shadow.moves[shadow.cursor:]
            shadow.moves.append((color, point))
            shadow.cursor += 1
            if game.to_dict()["result"] != "":
                problems.append(f"step {step}: a move kept the result")
        _check(game, shadow, step, problems)
        if len(problems) > 5:
            break
    return problems


@pytest.mark.parametrize("name", sorted(TABLE))
@pytest.mark.parametrize("size,seed", [(3, 5), (3, 6), (4, 1), (4, 2), (5, 3)])
def test_legality_matches_a_brute_force_replay(name, size, seed):
    game = Game(size, rules=name)
    shadow = Shadow(size, name, [], BLACK)
    assert _drive(game, shadow, seed, 300) == []


@pytest.mark.parametrize("name", sorted(TABLE))
def test_legality_matches_a_brute_force_replay_with_white_first(name):
    ru = {"new-zealand": "NZ", "tromp-taylor": "tromp-taylor"}.get(name, name)
    game = Game.from_sgf(f"(;GM[1]FF[4]SZ[4]RU[{ru}]AB[aa]AW[dd]PL[W])")
    shadow = Shadow(4, name, [(BLACK, (0, 0)), (WHITE, (3, 3))], WHITE)
    assert _drive(game, shadow, 11, 300) == []
