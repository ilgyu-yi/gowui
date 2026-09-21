"""Boards of a space (SPEC §3.3, §4.1, §7.6): default names, duplicate / new / select / rename /
move / delete, the 64-board limit, and per-board games and human settings.
"""

from __future__ import annotations

import re

from session_helpers import board_entry, board_ids, move_list


async def duplicate(h, board_id: int | None = None) -> dict:
    """⧉ on a tile: copy the board it names (the active one when none is named)."""
    count = len(h.rec.state()["boards"])
    start = h.rec.mark()
    await h.send({"type": "board_duplicate",
                  "id": h.rec.state()["activeBoard"] if board_id is None else board_id})
    state = await h.rec.wait_state(lambda f: len(f["boards"]) == count + 1, start)
    assert state is not None, f"no board was added; errors: {h.rec.errors(start)}"
    return state


async def new_board(h) -> dict:
    """"+ New board": append a fresh board and switch to it (§3.3)."""
    count = len(h.rec.state()["boards"])
    start = h.rec.mark()
    await h.send({"type": "board_new"})
    state = await h.rec.wait_state(lambda f: len(f["boards"]) == count + 1, start)
    assert state is not None, f"no board was added; errors: {h.rec.errors(start)}"
    return state


async def move(h, board_id: int, after: int | None) -> dict:
    """Put ``board_id`` after the board ``after`` names, or at the head when it is null."""
    start = h.rec.mark()
    await h.send({"type": "board_move", "id": board_id, "after": after})
    state = await h.fresh_state()
    assert h.rec.errors(start) == [], f"the move was refused: {h.rec.errors(start)}"
    return state


async def three_boards(h) -> tuple[int, int, int]:
    """Three boards in order; the last one is active."""
    first = h.rec.state()["activeBoard"]
    second = (await duplicate(h))["activeBoard"]
    third = (await duplicate(h))["activeBoard"]
    assert board_ids(await h.fresh_state()) == [first, second, third]
    return first, second, third


async def refused(h, message: dict) -> tuple[str | None, bool]:
    """Send ``message``; report its error and whether every board came through unchanged."""
    before = (await h.fresh_state())["boards"]
    start = h.rec.mark()
    await h.send(message)
    error = await h.rec.wait_error(start)
    assert error is None or "unknown message type" not in error, \
        f"{message['type']} is not dispatched at all: {error}"
    return error, (await h.fresh_state())["boards"] == before


async def select(h, board_id: int) -> dict:
    start = h.rec.mark()
    await h.send({"type": "board_select", "id": board_id})
    state = await h.rec.wait_state(lambda f: f["activeBoard"] == board_id, start)
    assert state is not None, f"board {board_id} was not selected; errors: {h.rec.errors(start)}"
    return state


async def human(h, **fields) -> dict:
    start = h.rec.mark()
    await h.send({"type": "human_params", **fields})
    state = await h.rec.wait_state(lambda f: True, start)
    assert state is not None, f"no state after human_params; errors: {h.rec.errors(start)}"
    return state


# -- the first board ------------------------------------------------------------------------------
async def test_a_fresh_space_has_one_board_named_board_1(make_session):
    h = make_session()
    state = await h.fresh_state()
    assert [(b["name"], b["id"] == state["activeBoard"]) for b in state["boards"]] == \
        [("Board 1", True)]


# -- duplicate ------------------------------------------------------------------------------------
async def test_duplicate_switches_to_a_copy_of_position_and_cursor(h):
    await h.play("D4", "E5")
    await h.send({"type": "navigate", "index": 1})
    source = h.rec.state()["activeBoard"]
    state = await duplicate(h)
    assert (state["activeBoard"] != source, move_list(state), state["game"]["cursor"]) == \
        (True, ["D4", "E5"], 1)


async def test_duplicate_copies_the_board_it_names_not_the_active_one(h):
    """§4.1 "The board frames": ``board_duplicate`` names the board it copies and has no "the
    active one" meaning."""
    await h.play("D4")
    first = h.rec.state()["activeBoard"]
    await duplicate(h)
    await h.play("E5")            # the active board (the copy) is now two moves long
    assert move_list(await duplicate(h, first)) == ["D4"]


async def test_duplicate_inserts_the_copy_after_the_board_it_names(h):
    """§3.3 "Duplicate": the copy lands after the board it names, not after the active one."""
    first = h.rec.state()["activeBoard"]
    second = (await duplicate(h))["activeBoard"]
    state = await duplicate(h, first)
    assert board_ids(state) == [first, state["activeBoard"], second]


async def test_duplicated_boards_get_distinct_default_names(h):
    await duplicate(h)
    state = await duplicate(h)
    names = [b["name"] for b in state["boards"]]
    assert len(set(names)) == 3 and all(re.fullmatch(r"Board \d+", n) for n in names)


async def test_duplicate_copies_the_human_settings(h):
    await human(h, profile="rank_5k", policy={"min_p": 0.05})
    state = await duplicate(h)
    assert (state["settings"]["humanProfile"], state["settings"]["humanPolicy"]) == \
        ("rank_5k", {"min_p": 0.05})


async def test_duplicate_at_the_board_limit_is_refused_saying_so(h):
    """§4.1: at ``MAX_BOARDS`` the refusal names which action was refused, and changes nothing."""
    board = h.rec.state()["activeBoard"]
    for _ in range(63):
        await duplicate(h)
    error, unchanged = await refused(h, {"type": "board_duplicate", "id": board})
    assert ("duplicate" in (error or "").lower(), unchanged) == (True, True)


async def test_duplicate_of_a_board_that_does_not_exist_is_refused(h):
    """§4.1: an id naming no board is refused with fixed text and changes nothing."""
    assert await refused(h, {"type": "board_duplicate", "id": 9999}) == ("no board 9999", True)


async def test_duplicate_with_an_id_that_is_not_a_whole_number_is_refused(h):
    """§4.1: an ``id`` that is not a whole number gets "id must be a board id"."""
    assert await refused(h, {"type": "board_duplicate", "id": 1.5}) == \
        ("id must be a board id", True)


# -- a new board (§3.3 "New board") ---------------------------------------------------------------
async def test_a_new_board_is_appended_and_becomes_active(h):
    """§3.3: "New board" appends a fresh board and switches to it."""
    first = h.rec.state()["activeBoard"]
    state = await new_board(h)
    assert board_ids(state) == [first, state["activeBoard"]]


async def test_a_new_board_is_an_empty_game(h):
    """§3.3 "A fresh board": an empty game — no moves and no setup stones."""
    await h.play("D4")
    game = (await new_board(h))["game"]
    assert (game["moveCount"], game["setupStones"]) == (0, [])


async def test_a_new_board_takes_the_active_boards_size_and_rules(h):
    """§3.3 "A fresh board": size and rules are the active board's."""
    await h.new_game(13, rules="chinese")
    game = (await new_board(h))["game"]
    assert (game["size"], game["rules"]) == (13, "chinese")


async def test_a_new_board_takes_the_rule_sets_default_komi_not_the_active_boards(h):
    """§3.3 "A fresh board": the rule set's default komi (7.5 for Chinese), never the active
    board's own, which on a handicap board is 0.5; and handicap 0."""
    await h.new_game(9, rules="chinese", handicap=4)
    game = (await new_board(h))["game"]
    assert (game["komi"], game["handicap"]) == (7.5, 0)


async def test_a_new_board_is_named_for_its_id(h):
    """§3.3 "A fresh board": the name is ``Board <id>``."""
    state = await new_board(h)
    entry = board_entry(state, state["activeBoard"])
    assert entry["name"] == f"Board {entry['id']}"


async def test_a_new_board_has_the_default_profile_and_empty_tuples(h):
    """§3.3 "A fresh board": the default profile and empty tuples, whatever the active board
    carries."""
    default = (await h.fresh_state())["settings"]["humanProfile"]
    await human(h, profile="rank_5k", policy={"min_p": 0.05}, compare={"temperature": 1.5})
    settings = (await new_board(h))["settings"]
    assert (settings["humanProfile"], settings["humanPolicy"], settings["humanCompare"]) == \
        (default, {}, None)


async def test_a_new_board_leaves_the_boards_before_it_alone(h):
    """Issue #50 AC: the new-board action makes an empty board without touching any existing
    board's game."""
    await h.play("D4")
    first = h.rec.state()["activeBoard"]
    assert board_entry(await new_board(h), first)["moveCount"] == 1


async def test_a_new_board_at_the_board_limit_is_refused_saying_so(h):
    """§4.1: at ``MAX_BOARDS`` the refusal names which action was refused, and changes nothing."""
    for _ in range(63):
        await duplicate(h)
    error, unchanged = await refused(h, {"type": "board_new"})
    assert ("new" in (error or "").lower(), unchanged) == (True, True)


# -- select ----------------------------------------------------------------------------------------
async def test_select_shows_the_boards_own_game(h):
    await h.play("D4")
    first = h.rec.state()["activeBoard"]
    await duplicate(h)
    await h.play("E5")
    state = await select(h, first)
    assert move_list(state) == ["D4"]


async def test_each_board_keeps_its_own_cursor(h):
    await h.play("D4", "E5")
    first = h.rec.state()["activeBoard"]
    second = (await duplicate(h))["activeBoard"]
    await h.send({"type": "navigate", "index": 0})
    await select(h, first)
    state = await select(h, second)
    assert state["game"]["cursor"] == 0


async def test_select_of_an_unknown_board_is_an_error(h):
    start = h.rec.mark()
    await h.send({"type": "board_select", "id": 9999})
    assert await h.rec.wait_error(start) is not None


# -- rename ----------------------------------------------------------------------------------------
async def rename(h, name: str) -> dict:
    board = h.rec.state()["activeBoard"]
    await h.send({"type": "board_rename", "id": board, "name": name})
    state = await h.fresh_state()
    return board_entry(state, board)


async def test_rename_trims_the_name(h):
    assert (await rename(h, "  opening study  "))["name"] == "opening study"


async def test_rename_to_an_empty_name_is_ignored(h):
    await rename(h, "kept")
    assert (await rename(h, "   "))["name"] == "kept"


async def test_rename_truncates_to_40_characters(h):
    assert (await rename(h, "x" * 41 + "yz"))["name"] == "x" * 40


# -- move (§3.3 "Move", §4.1 "The board frames") --------------------------------------------------
async def test_a_move_puts_the_board_after_its_anchor(h):
    """§3.3 "Move": a board goes after the one the frame names."""
    first, second, third = await three_boards(h)
    assert board_ids(await move(h, first, third)) == [second, third, first]


async def test_a_move_with_a_null_anchor_puts_the_board_at_the_head(h):
    """§4.1: ``after`` is null to move a board to the head."""
    first, second, third = await three_boards(h)
    assert board_ids(await move(h, third, None)) == [third, first, second]


async def test_a_move_onto_the_boards_own_predecessor_changes_the_order_not_at_all(h):
    """§4.1: an anchor already the board's predecessor is accepted (``move`` fails on a refusal)
    and changes the order not at all — the no-op a drag that lands where it started produces."""
    first, second, third = await three_boards(h)
    assert board_ids(await move(h, second, first)) == [first, second, third]


async def test_a_move_changes_nothing_but_the_order(h):
    """§4.1: a move changes the order and nothing else — no board's game, name, tuples or
    analysis is touched, and no engine move in flight is made stale.

    The thumbnails alone cannot say this. They carry `heat` and `winrate`, and with no engine
    connected both are empty for every board, so an analysis thrown away looks exactly like one
    that was never there. The move epoch and the boards' own versions are what the claim is
    about, so the test reads them."""
    from gowui.engine.types import Analysis

    first, _second, third = await three_boards(h)
    # Seeded, because with no engine connected every slot's analysis is None and comparing
    # {1: None, 2: None, 3: None} to itself proves nothing - the blind spot this assertion exists
    # to close would still be open.
    for slot in h.session.boards:
        slot.last_analysis = Analysis(turn=slot.id, source="seeded")
        slot.analysis_version = slot.version
    before = {b["id"]: b for b in (await h.fresh_state())["boards"]}
    epoch = h.session._epoch
    versions = {slot.id: slot.version for slot in h.session.boards}
    analyses = {slot.id: slot.last_analysis for slot in h.session.boards}

    state = await move(h, first, third)

    assert {b["id"]: b for b in state["boards"]} == before
    assert h.session._epoch == epoch, "a move made an engine move in flight stale"
    assert {slot.id: slot.version for slot in h.session.boards} == versions
    assert {slot.id: slot.last_analysis for slot in h.session.boards} == analyses


async def test_a_move_keeps_the_active_board_active(h):
    """§4.1: a move leaves the active board active."""
    first, _second, third = await three_boards(h)
    assert (await move(h, first, third))["activeBoard"] == third


async def test_a_move_without_an_after_key_is_refused(h):
    """§4.1: ``after`` must be present — absent is refused rather than read as null, so a dropped
    field cannot silently mean "move to the top"."""
    first, _second, _third = await three_boards(h)
    assert await refused(h, {"type": "board_move", "id": first}) == \
        ("after must be a board id or null", True)


async def test_a_move_whose_after_is_not_a_whole_number_is_refused(h):
    """§4.1: an ``after`` that is not a whole number gets "after must be a board id or null"."""
    first, _second, _third = await three_boards(h)
    assert await refused(h, {"type": "board_move", "id": first, "after": 1.5}) == \
        ("after must be a board id or null", True)


async def test_a_move_whose_after_names_no_board_is_refused(h):
    """§4.1: an anchor that is gone is simply refused, and the next ``state`` resyncs that tab."""
    first, _second, _third = await three_boards(h)
    assert await refused(h, {"type": "board_move", "id": first, "after": 9999}) == \
        ("no board 9999", True)


async def test_a_move_whose_id_names_no_board_is_refused(h):
    """§4.1: an id naming no board is refused with fixed text and changes nothing."""
    _first, _second, third = await three_boards(h)
    assert await refused(h, {"type": "board_move", "id": 9999, "after": third}) == \
        ("no board 9999", True)


async def test_a_move_whose_id_is_not_a_whole_number_is_refused(h):
    """§4.1: an ``id`` that is not a whole number gets "id must be a board id"."""
    _first, _second, third = await three_boards(h)
    assert await refused(h, {"type": "board_move", "id": 1.5, "after": third}) == \
        ("id must be a board id", True)


async def test_a_move_after_itself_is_refused(h):
    """§4.1: a ``board_move`` whose ``after`` is the board itself is refused and changes
    nothing."""
    first, _second, _third = await three_boards(h)
    error, unchanged = await refused(h, {"type": "board_move", "id": first, "after": first})
    assert (error is not None, unchanged) == (True, True)


# -- delete ----------------------------------------------------------------------------------------
async def test_deleting_the_active_board_switches_to_a_neighbour(h):
    first = h.rec.state()["activeBoard"]
    middle = (await duplicate(h))["activeBoard"]
    last = (await duplicate(h))["activeBoard"]
    await select(h, middle)
    start = h.rec.mark()
    await h.send({"type": "board_delete", "id": middle})
    state = await h.rec.wait_state(lambda f: middle not in board_ids(f), start)
    assert state is not None and state["activeBoard"] in (first, last)


async def test_deleting_another_board_keeps_the_active_one(h):
    first = h.rec.state()["activeBoard"]
    second = (await duplicate(h))["activeBoard"]
    start = h.rec.mark()
    await h.send({"type": "board_delete", "id": first})
    state = await h.rec.wait_state(lambda f: first not in board_ids(f), start)
    assert state is not None and state["activeBoard"] == second


async def delete_last(h) -> dict:
    """× on the only tile: the frame is accepted and the board is reset in place (§3.3)."""
    board = h.rec.state()["activeBoard"]
    assert board_ids(await h.fresh_state()) == [board], "the space must hold one board"
    start = h.rec.mark()
    await h.send({"type": "board_delete", "id": board})
    state = await h.fresh_state()
    assert h.rec.errors(start) == [], "deleting the last board is accepted (§3.3)"
    return state


async def test_deleting_the_last_board_keeps_its_id_and_its_place(h):
    """§3.3 "Delete": the board keeps its id, its place and its active status, so a tab holding
    that id does not lose its tile."""
    board = h.rec.state()["activeBoard"]
    state = await delete_last(h)
    assert (board_ids(state), state["activeBoard"]) == ([board], board)


async def test_deleting_the_last_board_clears_its_game(h):
    """§3.3 "Delete": the board becomes a fresh board, so its game is cleared."""
    await h.play("D4", "E5")
    assert (await delete_last(h))["game"]["moveCount"] == 0


async def test_deleting_the_last_board_clears_its_name(h):
    """§3.3 "Delete": the name is part of the record the reset clears, back to ``Board <id>``."""
    board = h.rec.state()["activeBoard"]
    await rename(h, "opening study")
    assert board_entry(await delete_last(h), board)["name"] == f"Board {board}"


async def test_deleting_the_last_board_clears_its_profile_and_tuples(h):
    """§3.3 "Delete": the profile and the tuples are cleared with the rest of the record."""
    default = (await h.fresh_state())["settings"]["humanProfile"]
    await human(h, profile="rank_5k", policy={"min_p": 0.05}, compare={"temperature": 1.5})
    settings = (await delete_last(h))["settings"]
    assert (settings["humanProfile"], settings["humanPolicy"], settings["humanCompare"]) == \
        (default, {}, None)


async def test_deleting_the_last_board_keeps_its_size_and_rules(h):
    """§3.3: the reset makes a *fresh* board, which takes the size and rules it had."""
    await h.new_game(13, rules="chinese")
    game = (await delete_last(h))["game"]
    assert (game["size"], game["rules"]) == (13, "chinese")


async def test_deleting_the_last_board_takes_the_rule_sets_default_komi(h):
    """§3.3 "A fresh board": the rule set's default komi, never the board's own 0.5 handicap
    komi, and handicap 0."""
    await h.new_game(9, rules="chinese", handicap=4)
    game = (await delete_last(h))["game"]
    assert (game["komi"], game["handicap"]) == (7.5, 0)


async def test_deleting_a_board_that_does_not_exist_is_refused(h):
    """§4.1: an id naming no board is refused with fixed text and changes nothing."""
    assert await refused(h, {"type": "board_delete", "id": 9999}) == ("no board 9999", True)


async def test_a_deleted_boards_id_is_not_given_to_a_later_board(h):
    """The page matches a tile by its id (§3.8), so an id a delete freed must not come back to
    stand for another board while the space is live."""
    seen = [h.rec.state()["activeBoard"]]
    for _ in range(3):
        seen.append((await duplicate(h))["activeBoard"])
    for board in (seen[3], seen[1]):
        await h.send({"type": "board_delete", "id": board})
    await h.fresh_state()
    for _ in range(3):
        fresh = (await duplicate(h))["activeBoard"]
        assert fresh not in seen, f"board id {fresh} was given out a second time"
        seen.append(fresh)


# -- human settings belong to a board --------------------------------------------------------------
async def test_human_settings_travel_with_their_board(h):
    await human(h, profile="rank_5k")
    first = h.rec.state()["activeBoard"]
    second = (await duplicate(h))["activeBoard"]
    await human(h, profile="rank_1d")
    back = await select(h, first)
    again = await select(h, second)
    assert (back["settings"]["humanProfile"], again["settings"]["humanProfile"]) == \
        ("rank_5k", "rank_1d")


async def test_a_thumbnail_shows_its_boards_profile(h):
    await human(h, profile="rank_5k")
    first = h.rec.state()["activeBoard"]
    await duplicate(h)
    state = await human(h, profile="rank_1d")
    assert board_entry(state, first)["profile"] == "rank_5k"


# -- a board entry's human tuple (§4.2 "each boards entry") ---------------------------------------
async def test_a_board_entry_carries_its_boards_policy_tuple(h):
    state = await human(h, policy={"min_p": 0.05})
    assert board_entry(state, state["activeBoard"])["policy"] == {"min_p": 0.05}


async def test_a_board_entry_keeps_its_own_policy_tuple_after_another_board_changes(h):
    await human(h, policy={"min_p": 0.05})
    first = h.rec.state()["activeBoard"]
    await duplicate(h)
    state = await human(h, policy={"temperature": 1.5})
    assert board_entry(state, first)["policy"] == {"min_p": 0.05}


async def test_a_board_entry_says_compare_while_its_board_has_a_compare_tuple(h):
    state = await human(h, policy={}, compare={"temperature": 1.5})
    assert board_entry(state, state["activeBoard"])["compare"] is True


async def test_a_board_entry_says_no_compare_without_a_compare_tuple(h):
    state = await human(h, policy={"min_p": 0.05})
    assert board_entry(state, state["activeBoard"])["compare"] is False


async def test_a_board_entry_says_no_compare_once_the_compare_tuple_is_cleared(h):
    await human(h, policy={}, compare={"temperature": 1.5})
    state = await human(h, compare=None)
    assert board_entry(state, state["activeBoard"])["compare"] is False
