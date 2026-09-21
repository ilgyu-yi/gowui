"""Boards of a space (SPEC §3.3, §7.6): default names, duplicate / select / rename / delete, the
64-board limit, and per-board games and human settings.
"""

from __future__ import annotations

import re

from session_helpers import board_entry, board_ids, move_list


async def duplicate(h) -> dict:
    count = len(h.rec.state()["boards"])
    start = h.rec.mark()
    await h.send({"type": "board_duplicate"})
    state = await h.rec.wait_state(lambda f: len(f["boards"]) == count + 1, start)
    assert state is not None, f"no board was added; errors: {h.rec.errors(start)}"
    return state


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


async def test_duplicate_inserts_the_copy_after_the_active_board(h):
    first = h.rec.state()["activeBoard"]
    second = (await duplicate(h))["activeBoard"]
    await select(h, first)
    state = await duplicate(h)
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


async def test_duplicate_at_the_board_limit_is_refused(h):
    for _ in range(63):
        await duplicate(h)
    start = h.rec.mark()
    await h.send({"type": "board_duplicate"})
    assert await h.rec.wait_error(start) is not None


async def test_duplicate_at_the_board_limit_adds_nothing(h):
    for _ in range(63):
        await duplicate(h)
    await h.send({"type": "board_duplicate"})
    assert len((await h.fresh_state())["boards"]) == 64


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


async def test_the_last_board_cannot_be_deleted(h):
    board = h.rec.state()["activeBoard"]
    start = h.rec.mark()
    await h.send({"type": "board_delete", "id": board})
    assert await h.rec.wait_error(start) is not None


async def test_deleting_the_last_board_leaves_it_in_place(h):
    board = h.rec.state()["activeBoard"]
    await h.send({"type": "board_delete", "id": board})
    assert board_ids(await h.fresh_state()) == [board]


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
